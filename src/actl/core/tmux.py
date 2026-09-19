from __future__ import annotations

import os
import subprocess
import tempfile
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


def set_remote_ssh(target: str | None) -> None:
    """Route all tmux invocations through ``ssh <target> tmux ...``.

    Used by the MainPC remote board: install actl on MainPC once, then run
    ``actl tui --ssh asus`` to drive the asus tmux server without cloning
    or installing anything on the remote side beyond tmux itself.
    """
    global REMOTE_SSH_TARGET
    REMOTE_SSH_TARGET = target


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
    import shlex

    command = [
        "ssh", "-n", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
        REMOTE_SSH_TARGET, " ".join(shlex.quote(part) for part in args),
    ]
    # Windows OpenSSH parses a Python-created single remote-command argument
    # differently from an interactive cmd invocation; let cmd.exe apply the
    # platform's native quoting once at this boundary.
    if os.name == "nt":
        # Do not add double quotes around the remote command: subprocess adds
        # backslashes for cmd.exe /c and they reach ssh literally. The
        # command already contains POSIX single-quote escaping for its shell.
        return ["cmd.exe", "/d", "/c", " ".join(command)]
    return command


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
    args = _remote_args(args)
    try:
        return subprocess.run(
            args, check=check, capture_output=True, text=text,
            encoding="utf-8", errors="replace", timeout=10, **_no_window(),
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
    # NOTE: raw subprocess.run (not _run) so unit tests can stub _run without
    # affecting this inventory read, and check=False so ssh/tmux failures
    # report False instead of raising.
    proc = subprocess.run(
        _remote_args([*_tmux_base(socket_path), "list-panes", "-a", "-F", "#{pane_id}\t#{session_name}:#{window_index}.#{pane_index}"]),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        timeout=10,
        **_no_window(),
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
            import subprocess as _sp

            _sp.run(_remote_args([*base, "delete-buffer", "-b", buffer_name]), capture_output=True)
        except OSError:
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
    fmt = "#{pane_id}\t#{session_name}:#{window_index}.#{pane_index}\t#{pane_current_command}\t#{pane_current_path}\t#{pane_title}"
    out = _run([*_tmux_base(socket_path), "list-panes", "-a", "-F", fmt]).stdout
    rows: list[PaneInfo] = []
    for line in out.splitlines():
        parts = line.split("\t", 4)
        if len(parts) == 5:
            rows.append(PaneInfo(*parts))
    return rows
