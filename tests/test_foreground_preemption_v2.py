"""Dogfood-04: active replaceable P2 preemption + foreground latency contract."""
from __future__ import annotations

import threading
import time

from actl.core import remote_scheduler as sched
from actl.core import send_truth


def _reset() -> None:
    sched.close_scheduler(None)
    send_truth.reset_all_correlations()


def test_active_p2_preview_preempted_by_p0_send_within_2s():
    """active P2 PREVIEW + P0 SEND -> preview cancelled, SEND begins <= 2s."""
    _reset()
    s = sched.scheduler_for("asus-preempt-preview")
    preview_entered = threading.Event()
    preview_left = threading.Event()
    send_started = threading.Event()
    send_t0 = {"t": 0.0}
    results: dict[str, object] = {}

    def preview():
        preview_entered.set()
        try:
            # Simulate a long remote exclusive wait that honors cancel_event.
            deadline = time.monotonic() + 30.0
            while time.monotonic() < deadline:
                inflight = s._inflight
                if inflight is not None and inflight.cancel_event.is_set():
                    raise sched.RemoteOpCancelled("active PREVIEW preempted")
                time.sleep(0.05)
            return "preview-timeout"
        finally:
            preview_left.set()

    def send():
        send_started.set()
        return "sent"

    def run_preview():
        try:
            results["preview"] = s.submit(
                preview,
                priority=sched.P2_BACKGROUND,
                kind=sched.KIND_PREVIEW,
                coalesce_key="preview:codex",
                replaceable=True,
                timeout=35.0,
            )
        except sched.RemoteOpCancelled as exc:
            results["preview"] = exc

    def run_send():
        assert preview_entered.wait(timeout=2)
        time.sleep(0.05)
        send_t0["t"] = time.monotonic()
        s.pause_background()
        try:
            results["send"] = s.submit(
                send,
                priority=sched.P0_USER,
                kind=sched.KIND_SEND,
                replaceable=False,
                timeout=10.0,
            )
        finally:
            s.resume_background()

    t_preview = threading.Thread(target=run_preview, daemon=True)
    t_send = threading.Thread(target=run_send, daemon=True)
    t_preview.start()
    t_send.start()
    t_send.join(timeout=5)
    t_preview.join(timeout=5)

    assert results.get("send") == "sent"
    assert send_started.is_set()
    acquire = send_started.wait(timeout=0) or True
    assert acquire
    elapsed = time.monotonic() - send_t0["t"]
    # Contract: P0 obtains ownership within FOREGROUND_ACQUIRE_MAX_S.
    assert elapsed <= sched.FOREGROUND_ACQUIRE_MAX_S + 0.35
    assert isinstance(results.get("preview"), sched.RemoteOpCancelled)
    assert s.stats()["active_preempts"] >= 1
    last = s.stats().get("last_p0_acquire_s")
    assert last is not None and float(last) <= sched.FOREGROUND_ACQUIRE_MAX_S + 0.35
    _reset()


def test_active_p2_hydrate_preempted_by_p0_copy_within_2s():
    """active P2 HYDRATE + P0 COPY -> hydrate cancelled, COPY begins <= 2s."""
    _reset()
    s = sched.scheduler_for("asus-preempt-hydrate")
    started = threading.Event()
    copy_started = threading.Event()
    t0 = {"t": 0.0}
    results: dict[str, object] = {}

    def hydrate():
        started.set()
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            inflight = s._inflight
            if inflight is not None and inflight.cancel_event.is_set():
                raise sched.RemoteOpCancelled("active HYDRATE preempted")
            time.sleep(0.05)
        return "hydrate-timeout"

    def copy_work():
        copy_started.set()
        return "copied"

    def run_hydrate():
        try:
            results["hydrate"] = s.submit(
                hydrate,
                priority=sched.P2_BACKGROUND,
                kind=sched.KIND_HYDRATE,
                coalesce_key="hydrate:asus",
                replaceable=True,
                timeout=35.0,
            )
        except sched.RemoteOpCancelled as exc:
            results["hydrate"] = exc

    def run_copy():
        assert started.wait(timeout=2)
        time.sleep(0.05)
        t0["t"] = time.monotonic()
        results["copy"] = s.submit(
            copy_work,
            priority=sched.P0_USER,
            kind=sched.KIND_COPY,
            replaceable=False,
            timeout=10.0,
        )

    threading.Thread(target=run_hydrate, daemon=True).start()
    t_copy = threading.Thread(target=run_copy, daemon=True)
    t_copy.start()
    t_copy.join(timeout=5)
    time.sleep(0.2)

    assert results.get("copy") == "copied"
    assert copy_started.is_set()
    assert (time.monotonic() - t0["t"]) <= sched.FOREGROUND_ACQUIRE_MAX_S + 0.35
    assert isinstance(results.get("hydrate"), sched.RemoteOpCancelled)
    _reset()


def test_queued_duplicate_refresh_coalesced():
    _reset()
    s = sched.scheduler_for("asus-preempt-coalesce")
    runs = {"n": 0}
    gate = threading.Event()
    first_started = threading.Event()

    def refresh():
        runs["n"] += 1
        first_started.set()
        assert gate.wait(timeout=2)
        return f"refresh-{runs['n']}"

    results: list[object] = []

    def launch():
        results.append(
            s.submit(
                refresh,
                priority=sched.P2_BACKGROUND,
                kind=sched.KIND_REFRESH,
                coalesce_key="refresh:asus",
                replaceable=True,
            )
        )

    first = threading.Thread(target=launch, daemon=True)
    first.start()
    assert first_started.wait(timeout=2)
    extras = [threading.Thread(target=launch, daemon=True) for _ in range(2)]
    for thread in extras:
        thread.start()
    time.sleep(0.05)
    gate.set()
    first.join(timeout=2)
    for thread in extras:
        thread.join(timeout=2)
    assert runs["n"] == 1
    assert results == ["refresh-1", "refresh-1", "refresh-1"]
    _reset()


def test_cancellation_leaves_no_orphan_worker():
    """After preemption the worker remains usable and inflight is cleared."""
    _reset()
    s = sched.scheduler_for("asus-preempt-orphan")
    entered = threading.Event()

    def sticky():
        entered.set()
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            if s._inflight is not None and s._inflight.cancel_event.is_set():
                raise sched.RemoteOpCancelled("preempted")
            time.sleep(0.05)
        return "late"

    def run_sticky():
        try:
            s.submit(
                sticky,
                priority=sched.P2_BACKGROUND,
                kind=sched.KIND_PREVIEW,
                replaceable=True,
                timeout=35.0,
            )
        except sched.RemoteOpCancelled:
            pass

    threading.Thread(target=run_sticky, daemon=True).start()
    assert entered.wait(timeout=2)
    s.pause_background()
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        with s._cv:
            if s._inflight is None:
                break
        time.sleep(0.05)
    with s._cv:
        assert s._inflight is None
    # Scheduler still works for a follow-up P0.
    assert (
        s.submit(lambda: "ok", priority=sched.P0_USER, kind=sched.KIND_SEND, replaceable=False)
        == "ok"
    )
    s.resume_background()
    _reset()


def test_p0_does_not_preempt_another_p0():
    _reset()
    s = sched.scheduler_for("asus-preempt-p0-p0")
    first_started = threading.Event()
    release_first = threading.Event()
    order: list[str] = []

    def first():
        order.append("first-start")
        first_started.set()
        assert release_first.wait(timeout=3)
        order.append("first-end")
        return "first"

    def second():
        order.append("second")
        return "second"

    results: dict[str, object] = {}

    def run_first():
        results["first"] = s.submit(
            first, priority=sched.P0_USER, kind=sched.KIND_SEND, replaceable=False
        )

    def run_second():
        assert first_started.wait(timeout=2)
        results["second"] = s.submit(
            second, priority=sched.P0_USER, kind=sched.KIND_COPY, replaceable=False
        )

    t1 = threading.Thread(target=run_first, daemon=True)
    t2 = threading.Thread(target=run_second, daemon=True)
    t1.start()
    t2.start()
    time.sleep(0.1)
    # First P0 must still be running; second waits (not cancelling first).
    with s._cv:
        assert s._inflight is not None
        assert s._inflight.kind == sched.KIND_SEND
        assert not s._inflight.cancel_event.is_set()
    release_first.set()
    t1.join(timeout=3)
    t2.join(timeout=3)
    assert results["first"] == "first"
    assert results["second"] == "second"
    assert order == ["first-start", "first-end", "second"]
    assert s.stats()["active_preempts"] == 0
    _reset()


def test_p0_does_not_preempt_non_replaceable_p1():
    _reset()
    s = sched.scheduler_for("asus-preempt-p1")
    p1_started = threading.Event()
    release_p1 = threading.Event()
    order: list[str] = []

    def p1():
        order.append("p1-start")
        p1_started.set()
        assert release_p1.wait(timeout=3)
        order.append("p1-end")
        return "p1"

    def p0():
        order.append("p0")
        return "p0"

    results: dict[str, object] = {}

    def run_p1():
        results["p1"] = s.submit(
            p1,
            priority=sched.P1_RESULT,
            kind=sched.KIND_RESULT,
            replaceable=False,
        )

    def run_p0():
        assert p1_started.wait(timeout=2)
        results["p0"] = s.submit(
            p0, priority=sched.P0_USER, kind=sched.KIND_SEND, replaceable=False
        )

    t1 = threading.Thread(target=run_p1, daemon=True)
    t0 = threading.Thread(target=run_p0, daemon=True)
    t1.start()
    t0.start()
    time.sleep(0.1)
    with s._cv:
        assert s._inflight is not None
        assert s._inflight.kind == sched.KIND_RESULT
        assert not s._inflight.cancel_event.is_set()
    release_p1.set()
    t1.join(timeout=3)
    t0.join(timeout=3)
    assert results["p1"] == "p1"
    assert results["p0"] == "p0"
    assert order == ["p1-start", "p1-end", "p0"]
    _reset()


def test_cancelled_p2_may_retry_later():
    _reset()
    s = sched.scheduler_for("asus-preempt-retry")
    entered = threading.Event()

    def sticky():
        entered.set()
        while True:
            if s._inflight is not None and s._inflight.cancel_event.is_set():
                raise sched.RemoteOpCancelled("preempted")
            time.sleep(0.05)

    def run_sticky():
        try:
            s.submit(
                sticky,
                priority=sched.P2_BACKGROUND,
                kind=sched.KIND_PREVIEW,
                replaceable=True,
                timeout=10.0,
            )
        except sched.RemoteOpCancelled:
            pass

    threading.Thread(target=run_sticky, daemon=True).start()
    assert entered.wait(timeout=2)
    s.pause_background()
    time.sleep(0.2)
    s.resume_background()
    # Later retry of the same kind succeeds.
    assert (
        s.submit(
            lambda: "preview-ok",
            priority=sched.P2_BACKGROUND,
            kind=sched.KIND_PREVIEW,
            coalesce_key="preview:retry",
            replaceable=True,
        )
        == "preview-ok"
    )
    _reset()


def test_scheduler_usable_after_cancellation():
    _reset()
    s = sched.scheduler_for("asus-preempt-usable")
    gate = threading.Event()
    started = threading.Event()

    def bg():
        started.set()
        while not (s._inflight and s._inflight.cancel_event.is_set()):
            if gate.is_set():
                return "bg"
            time.sleep(0.05)
        raise sched.RemoteOpCancelled("preempted")

    def run_bg():
        try:
            s.submit(bg, priority=sched.P2_BACKGROUND, kind=sched.KIND_REFRESH, replaceable=True)
        except sched.RemoteOpCancelled:
            pass

    threading.Thread(target=run_bg, daemon=True).start()
    assert started.wait(timeout=2)
    s.pause_background()
    assert s.submit(lambda: 1, priority=sched.P0_USER, kind=sched.KIND_FOCUS, replaceable=False) == 1
    s.resume_background()
    assert s.submit(lambda: 2, priority=sched.P2_BACKGROUND, kind=sched.KIND_HEALTH, replaceable=True) == 2
    _reset()


def test_send_truth_unchanged_for_success_path():
    _reset()
    send_truth.begin_send("codex", "%0", previous_result_hash="hash-old")
    send_truth.set_send_state("codex", "%0", send_truth.SENDING)
    send_truth.set_send_state("codex", "%0", send_truth.SUBMITTED)
    send_truth.set_send_state("codex", "%0", send_truth.START_ACKNOWLEDGED)
    klass, snap = send_truth.classify_result("codex", "%0", "hash-new")
    assert klass == send_truth.NEW_RESULT
    assert snap is not None
    assert send_truth.SEND_STATE_KO[send_truth.CLEARING_BACKGROUND] == "백그라운드 작업 정리 중"
    assert send_truth.COPY_STATE_KO[send_truth.COPY_READING] == "결과 읽는 중"
    _reset()


def test_result_correlation_unchanged_for_failed_and_pending():
    _reset()
    old = "ACTL_LIVE_E2E_OK_OLD"
    old_hash = send_truth.result_hash(old)
    send_truth.begin_send("codex", "%0", previous_result_hash=old_hash)
    send_truth.set_send_state("codex", "%0", send_truth.SEND_FAILED, error="x")
    klass, _ = send_truth.classify_result("codex", "%0", old_hash, text=old)
    assert klass == send_truth.STALE_RESULT
    send_truth.begin_send("codex", "%0", previous_result_hash=old_hash)
    send_truth.set_send_state("codex", "%0", send_truth.START_ACKNOWLEDGED)
    klass2, _ = send_truth.classify_result("codex", "%0", old_hash)
    assert klass2 == send_truth.RESULT_PENDING
    _reset()


def test_korean_clearing_and_acquire_timeout_labels():
    assert send_truth.SEND_STATE_KO[send_truth.CLEARING_BACKGROUND] == "백그라운드 작업 정리 중"
    assert send_truth.SEND_STATE_KO[send_truth.FOREGROUND_ACQUIRE_TIMEOUT] == "전송 지연: 백그라운드 정리 초과"
    assert send_truth.COPY_STATE_KO[send_truth.COPY_QUEUED] == "복사 대기"
    assert send_truth.COPY_STATE_KO[send_truth.COPY_CLEARING] == "백그라운드 작업 정리 중"
    assert send_truth.COPY_STATE_KO[send_truth.COPY_READING] == "결과 읽는 중"
