"""Priority remote-operation scheduler for one SSH target.

User actions (P0) must never fail merely because background polling holds the
single RemoteTransport slot. Background work is coalesced and replaceable.
Active replaceable P2 work is cooperatively cancelled so P0 can acquire the
transport within FOREGROUND_ACQUIRE_MAX_S.
"""
from __future__ import annotations

import contextvars
import heapq
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, TypeVar

T = TypeVar("T")

# Priority bands (lower number = higher priority).
P0_USER = 0
P1_RESULT = 1
P2_BACKGROUND = 2

# Founder-facing contract: P0 must obtain executable ownership within this
# window when only replaceable background work holds the transport.
FOREGROUND_ACQUIRE_MAX_S = 2.0

KIND_SEND = "SEND"
KIND_COPY = "COPY"
KIND_FOCUS = "FOCUS"
KIND_MAP = "MAP"
KIND_RESULT = "RESULT"
KIND_PREVIEW = "PREVIEW"
KIND_HYDRATE = "HYDRATE"
KIND_HEALTH = "HEALTH"
KIND_REFRESH = "REFRESH"
KIND_VALIDATE = "VALIDATE"
KIND_GENERIC = "GENERIC"

USER_KINDS = {KIND_SEND, KIND_COPY, KIND_FOCUS, KIND_MAP}
BACKGROUND_KINDS = {KIND_PREVIEW, KIND_HYDRATE, KIND_HEALTH, KIND_REFRESH, KIND_VALIDATE}


class RemoteOpCancelled(RuntimeError):
    """Queued or active replaceable background work was deferred for a user action."""


class TransportBusyError(RuntimeError):
    """Transport contention visible to callers that must not map it as DOWN."""


class ForegroundAcquireTimeout(RuntimeError):
    """P0 could not obtain transport ownership within the latency contract."""


@dataclass(order=True)
class _QueuedOp:
    priority: int
    seq: int
    kind: str = field(compare=False)
    coalesce_key: str | None = field(compare=False)
    replaceable: bool = field(compare=False)
    work: Callable[[], Any] = field(compare=False)
    done: threading.Event = field(compare=False, default_factory=threading.Event)
    result: Any = field(compare=False, default=None)
    error: BaseException | None = field(compare=False, default=None)
    cancelled: bool = field(compare=False, default=False)
    cancel_event: threading.Event = field(compare=False, default_factory=threading.Event)
    started_at: float | None = field(compare=False, default=None)
    waiters: list[threading.Event] = field(compare=False, default_factory=list)


@dataclass(frozen=True)
class OpContext:
    priority: int
    kind: str
    coalesce_key: str | None = None
    replaceable: bool = False
    timeout: float = 60.0


_OP_CTX: contextvars.ContextVar[OpContext | None] = contextvars.ContextVar(
    "actl_remote_op_ctx", default=None
)
_IN_SCHEDULER_WORKER: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "actl_remote_in_worker", default=False
)


def current_op_context() -> OpContext | None:
    return _OP_CTX.get()


def in_scheduler_worker() -> bool:
    return bool(_IN_SCHEDULER_WORKER.get())


@contextmanager
def remote_op(
    priority: int,
    kind: str,
    *,
    coalesce_key: str | None = None,
    replaceable: bool | None = None,
    timeout: float = 60.0,
):
    """Annotate the calling thread's remote work with priority metadata."""
    if replaceable is None:
        replaceable = priority >= P2_BACKGROUND
    token = _OP_CTX.set(
        OpContext(
            priority=priority,
            kind=kind,
            coalesce_key=coalesce_key,
            replaceable=replaceable,
            timeout=timeout,
        )
    )
    try:
        yield
    finally:
        _OP_CTX.reset(token)


class RemoteOpScheduler:
    """One controlled queue per SSH target; serializes work onto the transport."""

    def __init__(self, target: str) -> None:
        self.target = target
        self._cv = threading.Condition()
        self._heap: list[_QueuedOp] = []
        self._coalesce: dict[str, _QueuedOp] = {}
        self._inflight: _QueuedOp | None = None
        self._seq = 0
        self._worker: threading.Thread | None = None
        self._closed = False
        self._user_inflight = 0
        self._last_p0_acquire_s: float | None = None
        self._stats = {
            "submitted": 0,
            "coalesced": 0,
            "cancelled": 0,
            "completed": 0,
            "user_preempts": 0,
            "active_preempts": 0,
        }

    def stats(self) -> dict[str, int | float | None]:
        with self._cv:
            out: dict[str, int | float | None] = dict(self._stats)
            out["last_p0_acquire_s"] = self._last_p0_acquire_s
            return out

    def user_action_inflight(self) -> bool:
        with self._cv:
            return self._user_inflight > 0 or (
                self._inflight is not None and self._inflight.priority == P0_USER
            )

    def inflight_cancel_event(self) -> threading.Event | None:
        """Cancel event for the active op, if any (read without holding lock long)."""
        with self._cv:
            if self._inflight is None:
                return None
            return self._inflight.cancel_event

    def active_op_is_replaceable_background(self) -> bool:
        with self._cv:
            op = self._inflight
            return (
                op is not None
                and op.replaceable
                and op.priority >= P2_BACKGROUND
                and not op.cancelled
            )

    def pause_background(self) -> None:
        """Mark user action starting; cancel queued + active replaceable P2."""
        with self._cv:
            self._user_inflight += 1
            cancelled = self._cancel_replaceable_locked()
            active = self._request_cancel_active_replaceable_locked()
            self._stats["user_preempts"] += 1
            self._stats["cancelled"] += cancelled
            if active:
                self._stats["active_preempts"] += 1
            self._cv.notify_all()
        if active:
            self._abort_transport_safely()

    def resume_background(self) -> None:
        with self._cv:
            self._user_inflight = max(0, self._user_inflight - 1)
            self._cv.notify_all()

    def submit(
        self,
        work: Callable[[], T],
        *,
        priority: int | None = None,
        kind: str | None = None,
        coalesce_key: str | None = None,
        replaceable: bool | None = None,
        timeout: float | None = None,
    ) -> T:
        ctx = current_op_context()
        priority = P2_BACKGROUND if priority is None else priority
        kind = KIND_GENERIC if kind is None else kind
        if ctx is not None:
            priority = ctx.priority
            kind = ctx.kind
            coalesce_key = coalesce_key if coalesce_key is not None else ctx.coalesce_key
            replaceable = ctx.replaceable if replaceable is None else replaceable
            timeout = ctx.timeout if timeout is None else timeout
        if replaceable is None:
            replaceable = priority >= P2_BACKGROUND and kind in BACKGROUND_KINDS
        wait_timeout = 60.0 if timeout is None else timeout
        requested_at = time.monotonic()

        with self._cv:
            if self._closed:
                raise RuntimeError(f"remote scheduler closed for {self.target}")
            self._stats["submitted"] += 1
            existing: _QueuedOp | None = None
            waiter: threading.Event
            if coalesce_key and coalesce_key in self._coalesce:
                candidate = self._coalesce[coalesce_key]
                if not candidate.done.is_set() and not candidate.cancelled:
                    existing = candidate
                    waiter = threading.Event()
                    existing.waiters.append(waiter)
                    self._stats["coalesced"] += 1
                    self._cv.notify()
            if existing is None:
                if priority == P0_USER:
                    queued = self._cancel_replaceable_locked()
                    active = self._request_cancel_active_replaceable_locked()
                    self._stats["cancelled"] += queued
                    self._stats["user_preempts"] += 1
                    if active:
                        self._stats["active_preempts"] += 1
                        # Transport abort must happen outside the lock.
                        need_abort = True
                    else:
                        need_abort = False
                else:
                    need_abort = False
                self._seq += 1
                existing = _QueuedOp(
                    priority=priority,
                    seq=self._seq,
                    kind=kind,
                    coalesce_key=coalesce_key,
                    replaceable=replaceable,
                    work=work,
                )
                heapq.heappush(self._heap, existing)
                if coalesce_key:
                    self._coalesce[coalesce_key] = existing
                waiter = existing.done
                self._ensure_worker_locked()
                self._cv.notify()
            else:
                need_abort = False

        if need_abort:
            self._abort_transport_safely()

        if not waiter.wait(wait_timeout):
            raise TransportBusyError(
                f"remote transport busy: operation {kind} timed out waiting for slot on {self.target}"
            )
        assert existing is not None
        if priority == P0_USER and existing.started_at is not None:
            acquire = existing.started_at - requested_at
            with self._cv:
                self._last_p0_acquire_s = acquire
        if existing.cancelled:
            raise RemoteOpCancelled(
                f"remote background op {kind} deferred for higher-priority work"
            )
        if existing.error is not None:
            raise existing.error
        return existing.result  # type: ignore[return-value]

    def close(self) -> None:
        with self._cv:
            self._closed = True
            while self._heap:
                op = heapq.heappop(self._heap)
                op.cancelled = True
                op.cancel_event.set()
                op.error = RemoteOpCancelled("scheduler closed")
                op.done.set()
                for waiter in op.waiters:
                    waiter.set()
            if self._inflight is not None and self._inflight.replaceable:
                self._inflight.cancel_event.set()
            self._coalesce.clear()
            self._cv.notify_all()
        self._abort_transport_safely()

    def _abort_transport_safely(self) -> None:
        try:
            from actl.core.tmux import abort_remote_transport_for_preempt

            abort_remote_transport_for_preempt(self.target)
        except Exception:
            # Preemption must not raise into Founder actions; transport reset is best-effort.
            pass

    def _request_cancel_active_replaceable_locked(self) -> bool:
        op = self._inflight
        if op is None:
            return False
        if not (op.replaceable and op.priority >= P2_BACKGROUND):
            return False
        if op.cancelled:
            return False
        op.cancelled = True
        op.cancel_event.set()
        op.error = RemoteOpCancelled(f"active {op.kind} preempted for higher-priority work")
        return True

    def _cancel_replaceable_locked(self) -> int:
        kept: list[_QueuedOp] = []
        cancelled = 0
        while self._heap:
            op = heapq.heappop(self._heap)
            if op.replaceable and op.priority >= P2_BACKGROUND:
                op.cancelled = True
                op.cancel_event.set()
                op.error = RemoteOpCancelled(f"deferred {op.kind}")
                op.done.set()
                for waiter in op.waiters:
                    waiter.set()
                if op.coalesce_key:
                    self._coalesce.pop(op.coalesce_key, None)
                cancelled += 1
            else:
                kept.append(op)
        self._heap = kept
        heapq.heapify(self._heap)
        return cancelled

    def _ensure_worker_locked(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            return
        self._worker = threading.Thread(
            target=self._worker_loop,
            name=f"actl-remote-sched-{self.target}",
            daemon=True,
        )
        self._worker.start()

    def _worker_loop(self) -> None:
        while True:
            with self._cv:
                while not self._closed and not self._heap:
                    self._cv.wait(timeout=1.0)
                if self._closed and not self._heap:
                    return
                if not self._heap:
                    continue
                # Pause starting new background work while a user action is marked.
                if self._user_inflight > 0 and self._heap[0].priority >= P2_BACKGROUND:
                    self._cv.wait(timeout=0.05)
                    continue
                op = heapq.heappop(self._heap)
                if op.cancelled:
                    continue
                op.started_at = time.monotonic()
                self._inflight = op
            try:
                token = _IN_SCHEDULER_WORKER.set(True)
                try:
                    if op.cancel_event.is_set():
                        raise RemoteOpCancelled(f"active {op.kind} preempted before start")
                    result = op.work()
                    if op.cancel_event.is_set():
                        # Work returned after cancel — treat as deferred, drop result.
                        raise RemoteOpCancelled(f"active {op.kind} preempted during work")
                    op.result = result
                finally:
                    _IN_SCHEDULER_WORKER.reset(token)
            except BaseException as exc:  # noqa: BLE001 — delivered to waiter
                if op.error is None:
                    op.error = exc
                if isinstance(exc, RemoteOpCancelled):
                    op.cancelled = True
            finally:
                op.done.set()
                for waiter in op.waiters:
                    waiter.set()
                with self._cv:
                    if op.coalesce_key:
                        self._coalesce.pop(op.coalesce_key, None)
                    self._inflight = None
                    self._stats["completed"] += 1
                    self._cv.notify_all()


_SCHEDULERS: dict[str, RemoteOpScheduler] = {}
_SCHED_LOCK = threading.Lock()


def scheduler_for(target: str) -> RemoteOpScheduler:
    with _SCHED_LOCK:
        sched = _SCHEDULERS.get(target)
        if sched is None:
            sched = RemoteOpScheduler(target)
            _SCHEDULERS[target] = sched
        return sched


def close_scheduler(target: str | None) -> None:
    with _SCHED_LOCK:
        if target is None:
            items = list(_SCHEDULERS.items())
            _SCHEDULERS.clear()
        else:
            sched = _SCHEDULERS.pop(target, None)
            items = [(target, sched)] if sched is not None else []
    for _, sched in items:
        if sched is not None:
            sched.close()


def new_correlation_id() -> str:
    return uuid.uuid4().hex[:12]


def is_transport_contention(exc: BaseException | str) -> bool:
    text = str(exc).lower()
    return (
        "remote transport busy" in text
        or "deferred for higher-priority" in text
        or "preempted" in text
        or "deferred " in text and "remote" in text
        or "timed out waiting for slot" in text
        or isinstance(exc, (TransportBusyError, RemoteOpCancelled, ForegroundAcquireTimeout))
    )
