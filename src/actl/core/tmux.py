from __future__ import annotations

import os
import queue
import re
import subprocess
import shlex
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable

from actl.core.models import PaneInfo

SIDE_EFFECT_NONE = "NONE"
SIDE_EFFECT_POSSIBLE = "POSSIBLE_INPUT"
SIDE_EFFECT_OBSERVED = "INPUT_OBSERVED"

# When True, Direct-style calls (no pre_send_hook) consult runtime.guard_tmux_writer.
# Managed callers pass pre_send_hook=make_writer_pre_send_hook(...) and skip this path.
enforce_direct_writer_guard: bool = True


class TmuxError(RuntimeError):
    pass


def _maybe_guard_direct_writer(
    target: str,
    *,
    socket_path: str | None,
    pre_send_hook: Callable[[], Any] | None,
    enforce_writer_guard: bool | None,
) -> None:
    use_guard = enforce_direct_writer_guard if enforce_writer_guard is None else enforce_writer_guard
    if not use_guard or pre_send_hook is not None:
        return
    from actl.core.runtime import guard_tmux_writer

    guard_tmux_writer(target=target, socket_path=socket_path, mode="DIRECT")


REMOTE_SSH_TARGET: str | None = None
_REMOTE_TRANSPORT: "RemoteTransport | None" = None


def _tmux_control_quote(value: str) -> str:
    """Quote one argument for tmux control mode, preserving format bytes."""
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\r", "\\r")
        .replace("\n", "\\n")
        .replace("\t", "\\t")
    )
    return f'"{escaped}"'


class RemoteTransport:
    """One bounded tmux control-mode session per SSH target."""

    MAX_IN_FLIGHT = 1

    def __init__(self, target: str) -> None:
        self.target = target
        self.state = "DISCONNECTED"
        self._process: subprocess.Popen[str] | None = None
        self._lock = threading.Lock()
        self._retry_at = 0.0
        self._events: queue.Queue[str] = queue.Queue()
        self._responses: queue.Queue[object] | None = None
        self._reader: threading.Thread | None = None
        # Cooperative preemption: set by scheduler when active replaceable P2
        # must release the exclusive transport without killing Python threads.
        self._preempt = threading.Event()
        self._exclusive_generation = 0
        self._needs_reset = False

    def connect(self) -> None:
        if self._process is not None and self._process.poll() is None:
            self.state = "READY"
            return
        now = time.monotonic()
        if now < self._retry_at:
            self.state = "DEGRADED"
            raise TmuxError(f"remote transport reconnect backoff active for {self.target}")
        self.state = "CONNECTING"
        session_probe = _remote_args(["tmux", "list-sessions", "-F", "#{session_id}\t#{session_name}\t#{session_windows}"])
        try:
            sessions = subprocess.run(
                session_probe,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=10,
                shell=False,
                **_no_window(),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            self.state = "DEGRADED"
            self._retry_at = now + 1.0
            raise TmuxError(f"cannot inspect remote tmux sessions: {exc}") from exc
        session_id = None
        for line in sessions.stdout.splitlines():
            candidate, _, rest = line.partition("\t")
            name, _, windows = rest.partition("\t")
            try:
                valid_session_id = re.fullmatch(r"\$\d+", candidate.strip())
                if valid_session_id and name.strip() and int(windows) > 0:
                    session_id = candidate.strip()
                    break
            except ValueError:
                continue
        if sessions.returncode or session_id is None:
            self.state = "DEGRADED"
            self._retry_at = now + 1.0
            raise TmuxError("remote tmux has no existing session; refusing to create one")
        command = _remote_control_args(session_id)
        command[6] = self.target
        try:
            self._process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                shell=False,
                **_no_window(),
            )
            # tmux control mode emits one server-initialization frame before
            # accepting the first command. Consume only that frame so the
            # first caller receives its own response, not an empty startup.
            assert self._process.stdout is not None
            while True:
                startup_row = self._process.stdout.readline()
                if not startup_row:
                    raise TmuxError("remote tmux control session closed during startup")
                if startup_row.startswith("%end "):
                    break
            self._reader = threading.Thread(target=self._read_loop, name="actl-tmux-events", daemon=True)
            self._reader.start()
            self.state = "READY"
            self._retry_at = 0.0
        except (OSError, subprocess.SubprocessError, TmuxError) as exc:
            process, self._process = self._process, None
            if process is not None and process.poll() is None:
                process.kill()
                process.wait()
            self.state = "DEGRADED"
            self._retry_at = now + 1.0
            raise TmuxError(f"cannot connect remote transport to {self.target}: {exc}") from exc

    def health(self) -> str:
        if self._process is not None and self._process.poll() is None:
            self.state = "READY"
        elif self.state == "READY":
            self.state = "DEGRADED"
        return self.state

    def reconnect(self) -> None:
        self.close()
        self.connect()

    def _read_loop(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        frame: list[str] | None = None
        failed = False
        for raw in iter(process.stdout.readline, ""):
            row = raw.rstrip("\r\n")
            if row.startswith("%begin "):
                frame = []
                failed = False
                continue
            if frame is not None and row.startswith("%error "):
                failed = True
                continue
            if frame is not None and row.startswith("%end "):
                output = "\n".join(frame) + ("\n" if frame else "")
                response = TmuxError(output.strip() or "remote tmux command failed") if failed else subprocess.CompletedProcess(
                    ["ssh", self.target, "tmux", "-C"], 0, output, ""
                )
                pending = self._responses
                if pending is not None:
                    pending.put(response)
                frame = None
                continue
            if frame is not None:
                frame.append(row)
                continue
            if frame is None and row.startswith("%"):
                self._events.put(row)
        self.state = "DEGRADED"
        pending = self._responses
        if pending is not None:
            pending.put(TmuxError("remote transport closed unexpectedly"))

    def executeTmux(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        """Serialize through the per-target priority scheduler, then the lock.

        When already inside a scheduler worker, run exclusively without
        re-queueing (avoids nested-submit deadlock).
        """
        from actl.core.remote_scheduler import (
            KIND_GENERIC,
            P2_BACKGROUND,
            current_op_context,
            in_scheduler_worker,
            scheduler_for,
        )

        if in_scheduler_worker():
            return self._execute_tmux_exclusive(argv)

        ctx = current_op_context()

        def work() -> subprocess.CompletedProcess[str]:
            return self._execute_tmux_exclusive(argv)

        # Individual tmux commands are never coalesced; coalesce applies only to
        # high-level GUI operations submitted explicitly via scheduler.submit.
        return scheduler_for(self.target).submit(
            work,
            priority=ctx.priority if ctx is not None else P2_BACKGROUND,
            kind=ctx.kind if ctx is not None else KIND_GENERIC,
            coalesce_key=None,
            replaceable=ctx.replaceable if ctx is not None else True,
            timeout=ctx.timeout if ctx is not None else 60.0,
        )

    def request_preempt(self) -> None:
        """Signal the active exclusive wait to abort for a P0 user action."""
        self._preempt.set()

    def _execute_tmux_exclusive(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        from actl.core.remote_scheduler import RemoteOpCancelled, TransportBusyError, scheduler_for

        # Safety belt: scheduler already serializes; lock rejects true races.
        if not self._lock.acquire(timeout=0.05):
            raise TransportBusyError(
                "remote transport busy: maximum in-flight operations is 1"
            )
        self._exclusive_generation += 1
        generation = self._exclusive_generation
        cancel_event = None
        try:
            cancel_event = scheduler_for(self.target).inflight_cancel_event()
        except Exception:
            cancel_event = None
        try:
            # Only the active op's cancel_event aborts. Stale preempt from a prior
            # background abort must reset the control session, not cancel P0/P1.
            if cancel_event is not None and cancel_event.is_set():
                self._needs_reset = True
                raise RemoteOpCancelled("remote transport preempted before exclusive start")
            if self._needs_reset or self._preempt.is_set():
                self.close(aggressive=True)
                self._needs_reset = False
                self._preempt.clear()
            self.connect()
            assert self._process is not None and self._process.stdin is not None
            command = argv[1:] if argv and argv[0] == "tmux" else argv
            line = " ".join(_tmux_control_quote(part) for part in command)
            response_queue: queue.Queue[object] = queue.Queue(maxsize=1)
            self._responses = response_queue
            self._process.stdin.write(line + "\n")
            self._process.stdin.flush()
            # Poll so an active replaceable op can release within FOREGROUND_ACQUIRE_MAX_S.
            deadline = time.monotonic() + 10.0
            response: object | None = None
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    self._needs_reset = True
                    self._responses = None
                    # Soft-close the SSH control process so the next op gets a clean session.
                    self.close(aggressive=True)
                    self._preempt.clear()
                    raise RemoteOpCancelled("remote transport preempted during exclusive wait")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.state = "DEGRADED"
                    raise TmuxError("remote tmux command timed out after 10s")
                try:
                    response = response_queue.get(timeout=min(0.05, remaining))
                    break
                except queue.Empty:
                    continue
            if generation != self._exclusive_generation:
                raise RemoteOpCancelled("remote transport generation invalidated")
            if isinstance(response, TmuxError):
                raise response
            return response  # type: ignore[return-value]
        finally:
            self._responses = None
            self._lock.release()

    def drain_events(self) -> list[str]:
        events: list[str] = []
        while True:
            try:
                events.append(self._events.get_nowait())
            except queue.Empty:
                return events

    def close(self, *, aggressive: bool = False) -> None:
        process, self._process = self._process, None
        if process is None:
            self.state = "DISCONNECTED"
            return
        wait_s = 0.25 if aggressive else 2.0
        try:
            if process.stdin is not None and process.poll() is None:
                try:
                    process.stdin.write("exit\n")
                    process.stdin.flush()
                except OSError:
                    pass
            process.wait(timeout=wait_s)
        except (OSError, subprocess.TimeoutExpired):
            try:
                process.kill()
            except OSError:
                pass
            try:
                process.wait(timeout=0.5)
            except (OSError, subprocess.TimeoutExpired):
                pass
        reader = self._reader
        self._reader = None
        if reader is not None and reader is not threading.current_thread():
            reader.join(timeout=0.2 if aggressive else 1.0)
        self.state = "DISCONNECTED"


def remote_events() -> list[str]:
    if not REMOTE_SSH_TARGET or _REMOTE_TRANSPORT is None:
        return []
    return _REMOTE_TRANSPORT.drain_events()


def set_remote_ssh(target: str | None) -> None:
    """Route all tmux invocations through ``ssh <target> tmux ...``.

    Used by the MainPC remote board: install actl on MainPC once, then run
    ``actl tui --ssh asus`` to drive the asus tmux server without cloning
    or installing anything on the remote side beyond tmux itself.
    """
    if target and not re.fullmatch(r"[A-Za-z0-9_.@:-]+", target):
        raise ValueError("SSH target must be a host alias, user@host, or hostname")
    global REMOTE_SSH_TARGET, _REMOTE_TRANSPORT
    from actl.core.remote_scheduler import close_scheduler

    if _REMOTE_TRANSPORT is not None and _REMOTE_TRANSPORT.target != target:
        _REMOTE_TRANSPORT.close()
        close_scheduler(_REMOTE_TRANSPORT.target)
        _REMOTE_TRANSPORT = None
    if target is None and REMOTE_SSH_TARGET:
        close_scheduler(REMOTE_SSH_TARGET)
    REMOTE_SSH_TARGET = target


def abort_remote_transport_for_preempt(target: str) -> None:
    """Cooperatively abort an active replaceable exclusive wait on ``target``.

    Signals the transport's preempt event. If an exclusive SSH control process
    is mid-command, the waiter exits and closes that process cleanly so P0 can
    reconnect. Does not kill Python threads. Does not touch unrelated hosts.
    """
    transport = _REMOTE_TRANSPORT
    if transport is None or transport.target != target:
        return
    transport.request_preempt()


def _remote_transport() -> RemoteTransport:
    global _REMOTE_TRANSPORT
    if not REMOTE_SSH_TARGET:
        raise TmuxError("remote SSH target is not configured")
    if _REMOTE_TRANSPORT is None or _REMOTE_TRANSPORT.target != REMOTE_SSH_TARGET:
        _REMOTE_TRANSPORT = RemoteTransport(REMOTE_SSH_TARGET)
    return _REMOTE_TRANSPORT


def _tmux_base(socket_path: str | None = None) -> list[str]:
    if socket_path:
        return ["tmux", "-S", socket_path]
    return ["tmux"]


def _remote_args(args: list[str]) -> list[str]:
    """Wrap a full tmux argv for ``ssh TARGET`` as one remote shell command.

    ssh joins its arguments with spaces for the remote shell, so every part
    (``-F '#{...}'`` formats, ``#{pane_id}`` targets, socket paths) must be
    shell-quoted, otherwise quotes are stripped and tmux misparses flags.
    Local (non-ssh) calls pass through untouched.
    """
    if not REMOTE_SSH_TARGET:
        return args
    command = ["ssh", "-n", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", REMOTE_SSH_TARGET]
    if os.name == "nt":
        # Windows OpenSSH handles the remote command as one argument. Passing
        # separate argv items loses quoting through CreateProcess, especially
        # for tmux formats beginning with `#`.
        return command + [" ".join(shlex.quote(part) for part in args)]
    return command + [" ".join(shlex.quote(part) for part in args)]


def _remote_control_args(session_id: str) -> list[str]:
    parts = ["tmux", "-C", "attach-session", "-t", session_id]
    command = ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", REMOTE_SSH_TARGET or ""]
    return command + [" ".join(shlex.quote(part) for part in parts)]


def _no_window() -> dict:
    """Windows: ssh/tmux 자식 콘솔창 팝업 방지 (pythonw GUI용)."""
    import sys as _sys

    if _sys.platform != "win32":
        return {}
    try:
        info = subprocess.STARTUPINFO()
        info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        return {"startupinfo": info, "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    except Exception:
        return {}


def _run(args: list[str], *, check: bool = True, text: bool = True) -> subprocess.CompletedProcess:
    if REMOTE_SSH_TARGET:
        try:
            return _remote_transport().executeTmux(args)
        except TmuxError as exc:
            if not check:
                return subprocess.CompletedProcess(args, 1, b"" if not text else "", str(exc))
            raise
    args = _remote_args(args)
    command = args
    try:
        return subprocess.run(
            command, check=check, capture_output=True, text=text,
            encoding="utf-8", errors="replace", timeout=10,
            shell=False, **_no_window(),
        )
    except FileNotFoundError as exc:
        hint = "ssh" if args and args[0] == "ssh" else "tmux"
        raise TmuxError(f"{hint} is not installed or not in PATH") from exc
    except subprocess.CalledProcessError as exc:
        msg = exc.stderr.strip() if isinstance(exc.stderr, str) else str(exc)
        raise TmuxError(msg or f"tmux command failed: {' '.join(args)}") from exc


def target_exists(target: str, socket_path: str | None = None) -> bool:
    # display-message succeeds with an empty expansion for some nonexistent
    # targets on tmux 3.6, so compare against the authoritative pane inventory.
    proc = _run(
        [*_tmux_base(socket_path), "list-panes", "-a", "-F", "#{pane_id}\t#{session_name}:#{window_index}.#{pane_index}"],
        check=False,
    )
    if proc.returncode:
        return False
    for row in proc.stdout.splitlines():
        pane_id, _, address = row.partition("\t")
        if target == pane_id or target == address:
            return True
    return False


def pane_field(target: str, fmt: str, socket_path: str | None = None) -> str:
    return _run([*_tmux_base(socket_path), "display-message", "-p", "-t", target, fmt]).stdout.strip()


def send_keys(
    target: str,
    *keys: str,
    socket_path: str | None = None,
    enforce_writer_guard: bool | None = None,
) -> None:
    """Send raw keys (e.g. "C-c") to a pane; never types prompt text."""
    if not target_exists(target, socket_path=socket_path):
        raise TmuxError(f"tmux target not found: {target}")
    _maybe_guard_direct_writer(
        target,
        socket_path=socket_path,
        pre_send_hook=None,
        enforce_writer_guard=enforce_writer_guard,
    )
    _run([*_tmux_base(socket_path), "send-keys", "-t", target, *keys])


def _stage_result(
    stages: list[dict[str, Any]],
    *,
    ok: bool,
    side_effect: str,
    delivery_disposition: str | None = None,
    error: str | None = None,
    buffer_name: str | None = None,
) -> dict[str, Any]:
    completed = [s["name"] for s in stages if s.get("ok")]
    failed = next((s["name"] for s in stages if not s.get("ok")), None)
    return {
        "ok": ok,
        "stages": stages,
        "completedStages": completed,
        "failedStage": failed,
        "sideEffect": side_effect,
        "deliveryDisposition": delivery_disposition,
        "error": error,
        "bufferName": buffer_name,
    }


def send_prompt_staged(
    target: str,
    prompt: str,
    *,
    socket_path: str | None = None,
    press_enter: bool = True,
    pre_send_hook: Callable[[], Any] | None = None,
    enforce_writer_guard: bool | None = None,
) -> dict[str, Any]:
    """Load → paste → optional Enter with per-stage receipt; cleanup on same socket."""
    stages: list[dict[str, Any]] = []
    base = _tmux_base(socket_path)
    buffer_name = f"actl-buffer-{os.getpid()}-{time.time_ns()}"
    tmp_path: Path | None = None
    side_effect = SIDE_EFFECT_NONE

    def record(name: str, ok: bool, detail: str = "") -> None:
        stages.append({"name": name, "ok": ok, "detail": detail})

    try:
        if not target_exists(target, socket_path=socket_path):
            record("verify_target", False, f"tmux target not found: {target}")
            return _stage_result(
                stages,
                ok=False,
                side_effect=SIDE_EFFECT_NONE,
                error=f"tmux target not found: {target}",
                buffer_name=buffer_name,
            )
        record("verify_target", True)

        _maybe_guard_direct_writer(
            target,
            socket_path=socket_path,
            pre_send_hook=pre_send_hook,
            enforce_writer_guard=enforce_writer_guard,
        )

        if pre_send_hook is not None:
            try:
                pre_send_hook()
            except Exception as exc:
                record("pre_send_hook", False, str(exc))
                return _stage_result(
                    stages,
                    ok=False,
                    side_effect=SIDE_EFFECT_NONE,
                    error=str(exc),
                    buffer_name=buffer_name,
                )
            record("pre_send_hook", True)

        try:
            if REMOTE_SSH_TARGET:
                # The persistent remote process cannot see MainPC temp paths.
                # tmux control mode's double-quoted argument preserves LF/UTF-8.
                _run([*base, "set-buffer", "-b", buffer_name, prompt])
            else:
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", prefix="actl-", suffix=".txt", delete=False) as fh:
                    fh.write(prompt)
                    tmp_path = Path(fh.name)
                _run([*base, "load-buffer", "-b", buffer_name, str(tmp_path)])
            record("load_buffer", True)
            side_effect = SIDE_EFFECT_POSSIBLE
        except Exception as exc:
            record("load_buffer", False, str(exc))
            return _stage_result(
                stages,
                ok=False,
                side_effect=side_effect,
                error=str(exc),
                buffer_name=buffer_name,
            )

        try:
            # tmux 3.6: -p emits bracketed-paste delimiters only when the target
            # application asked for them; -r preserves LF rather than translating
            # every line to CR. This keeps one buffer insertion as one prompt.
            _run([*base, "paste-buffer", "-p", "-r", "-b", buffer_name, "-t", target, "-d"])
            record("paste_buffer", True)
            side_effect = SIDE_EFFECT_OBSERVED
        except Exception as exc:
            record("paste_buffer", False, str(exc))
            return _stage_result(
                stages,
                ok=False,
                side_effect=SIDE_EFFECT_POSSIBLE,
                delivery_disposition="DELIVERY_AMBIGUOUS",
                error=str(exc),
                buffer_name=buffer_name,
            )

        if press_enter:
            try:
                _run([*base, "send-keys", "-t", target, "Enter"])
                record("enter", True)
            except Exception as exc:
                record("enter", False, str(exc))
                return _stage_result(
                    stages,
                    ok=False,
                    side_effect=SIDE_EFFECT_OBSERVED,
                    delivery_disposition="DELIVERY_AMBIGUOUS",
                    error=str(exc),
                    buffer_name=buffer_name,
                )

        return _stage_result(
            stages,
            ok=True,
            side_effect=side_effect,
            delivery_disposition="TRANSPORT_SENT",
            buffer_name=buffer_name,
        )
    finally:
        if tmp_path:
            tmp_path.unlink(missing_ok=True)
        try:
            if REMOTE_SSH_TARGET:
                _run([*base, "delete-buffer", "-b", buffer_name], check=False)
            else:
                subprocess.run(
                    [*base, "delete-buffer", "-b", buffer_name],
                    capture_output=True,
                    check=False,
                )
        except (OSError, TmuxError):
            pass


def send_prompt(
    target: str,
    prompt: str,
    *,
    press_enter: bool = True,
    socket_path: str | None = None,
    pre_send_hook: Callable[[], Any] | None = None,
    enforce_writer_guard: bool | None = None,
) -> None:
    result = send_prompt_staged(
        target,
        prompt,
        socket_path=socket_path,
        press_enter=press_enter,
        pre_send_hook=pre_send_hook,
        enforce_writer_guard=enforce_writer_guard,
    )
    if not result["ok"]:
        raise TmuxError(result.get("error") or "send_prompt failed")


def interrupt_ctrl_c(
    target: str,
    *,
    socket_path: str | None = None,
    pre_send_hook: Callable[[], Any] | None = None,
    enforce_writer_guard: bool | None = None,
) -> dict[str, Any]:
    """Send C-c once after optional writer hook."""
    stages: list[dict[str, Any]] = []
    if not target_exists(target, socket_path=socket_path):
        stages.append({"name": "verify_target", "ok": False, "detail": f"tmux target not found: {target}"})
        return _stage_result(stages, ok=False, side_effect=SIDE_EFFECT_NONE, error=f"tmux target not found: {target}")
    stages.append({"name": "verify_target", "ok": True, "detail": ""})
    _maybe_guard_direct_writer(
        target,
        socket_path=socket_path,
        pre_send_hook=pre_send_hook,
        enforce_writer_guard=enforce_writer_guard,
    )
    if pre_send_hook is not None:
        try:
            pre_send_hook()
        except Exception as exc:
            stages.append({"name": "pre_send_hook", "ok": False, "detail": str(exc)})
            return _stage_result(stages, ok=False, side_effect=SIDE_EFFECT_NONE, error=str(exc))
        stages.append({"name": "pre_send_hook", "ok": True, "detail": ""})
    try:
        _run([*_tmux_base(socket_path), "send-keys", "-t", target, "C-c"])
        stages.append({"name": "interrupt_ctrl_c", "ok": True, "detail": ""})
        return _stage_result(
            stages,
            ok=True,
            side_effect=SIDE_EFFECT_OBSERVED,
            delivery_disposition="INTERRUPT_SENT",
        )
    except Exception as exc:
        stages.append({"name": "interrupt_ctrl_c", "ok": False, "detail": str(exc)})
        return _stage_result(
            stages,
            ok=False,
            side_effect=SIDE_EFFECT_POSSIBLE,
            delivery_disposition="DELIVERY_AMBIGUOUS",
            error=str(exc),
        )


def capture_pane(target: str, history: int = 500, socket_path: str | None = None) -> str:
    return _run(
        [*_tmux_base(socket_path), "capture-pane", "-p", "-J", "-S", f"-{history}", "-t", target]
    ).stdout


def list_panes(socket_path: str | None = None) -> list[PaneInfo]:
    fmt = "#{pane_id}\t#{session_name}:#{window_index}.#{pane_index}\t#{pane_current_command}\t#{pane_current_path}\t#{pane_title}\t#{pane_pid}"
    out = _run([*_tmux_base(socket_path), "list-panes", "-a", "-F", fmt]).stdout
    rows: list[PaneInfo] = []
    for line in out.splitlines():
        parts = line.split("\t", 5)
        if len(parts) == 6:
            try:
                pane_pid = int(parts[5])
            except ValueError:
                pane_pid = None
            rows.append(PaneInfo(*parts[:5], pane_pid=pane_pid))
    return rows


def _valid_tmux_name(name: str) -> str:
    name = name.strip()
    if not name or any(char in name for char in "\r\n\x00"):
        raise ValueError("tmux name must be non-empty and single-line")
    return name


def rename_session(target: str, name: str, socket_path: str | None = None) -> None:
    _run([*_tmux_base(socket_path), "rename-session", "-t", target, _valid_tmux_name(name)])


def rename_window(target: str, name: str, socket_path: str | None = None) -> None:
    _run([*_tmux_base(socket_path), "rename-window", "-t", target, _valid_tmux_name(name)])


def rename_pane(target: str, name: str, socket_path: str | None = None) -> None:
    _run([*_tmux_base(socket_path), "select-pane", "-t", target, "-T", _valid_tmux_name(name)])


def move_pane(source: str, destination: str, socket_path: str | None = None) -> None:
    """Move one existing pane to a window; the pane ID and mapping survive."""
    _run([*_tmux_base(socket_path), "move-pane", "-s", source, "-t", destination])


def create_session(name: str, cwd: str | None = None, socket_path: str | None = None) -> None:
    args = [*_tmux_base(socket_path), "new-session", "-d", "-s", _valid_tmux_name(name)]
    if cwd:
        args.extend(["-c", cwd])
    _run(args)


def create_window(target: str, name: str, socket_path: str | None = None) -> None:
    _run([*_tmux_base(socket_path), "new-window", "-d", "-t", target, "-n", _valid_tmux_name(name)])


def split_pane(target: str, *, horizontal: bool = True, socket_path: str | None = None) -> None:
    direction = "-h" if horizontal else "-v"
    _run([*_tmux_base(socket_path), "split-window", "-d", direction, "-t", target])
