from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from actl.core.registry import AGENTS
from actl.core.tmux import pane_field, target_exists


@dataclass(frozen=True)
class ProcessInfo:
    pid: int
    ppid: int
    args: str


@dataclass(frozen=True)
class TargetValidation:
    state: str
    target: str
    pane_id: str | None = None
    pane_pid: int | None = None
    command: str = "-"
    path: str = "-"
    agent_pid: int | None = None
    detail: str = ""

    @property
    def valid(self) -> bool:
        # TRANSPORT_BUSY means the mapping is still considered alive; controls
        # may proceed once the scheduler grants the user-action slot.
        return self.state in {"UP", "WORKING", "IDLE", "TRANSPORT_BUSY"}


_PS_CACHE: tuple[float, str] = (0.0, "")


def clear_ps_cache() -> None:
    global _PS_CACHE
    _PS_CACHE = (0.0, "")


def _ps_output() -> str:
    """ps 출력: 로컬 직접 실행, 원격(--ssh)은 ssh 경유. 원격만 2초 캐시."""
    import time as _time

    from actl.core.tmux import REMOTE_SSH_TARGET, _remote_args

    global _PS_CACHE
    now = _time.monotonic()
    if REMOTE_SSH_TARGET and now - _PS_CACHE[0] < 2.0 and _PS_CACHE[1]:
        return _PS_CACHE[1]
    try:
        from actl.core.tmux import _no_window

        proc = subprocess.run(
            _remote_args(["ps", "-eo", "pid=,ppid=,args="]),
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", check=False, timeout=10, **_no_window(),
        )
    except Exception:
        return _PS_CACHE[1] if REMOTE_SSH_TARGET else ""
    if proc.returncode:
        return _PS_CACHE[1] if REMOTE_SSH_TARGET else ""
    if REMOTE_SSH_TARGET:
        _PS_CACHE = (now, proc.stdout)
    return proc.stdout


def pane_processes(pane_pid: int) -> list[ProcessInfo]:
    """Return the pane process tree using read-only process-table inspection."""
    stdout = _ps_output()
    if not stdout:
        return []
    all_processes: dict[int, ProcessInfo] = {}
    children: dict[int, list[int]] = {}
    for line in stdout.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        try:
            item = ProcessInfo(int(parts[0]), int(parts[1]), parts[2])
        except ValueError:
            continue
        all_processes[item.pid] = item
        children.setdefault(item.ppid, []).append(item.pid)
    found: list[ProcessInfo] = []
    pending = [pane_pid]
    seen: set[int] = set()
    while pending:
        pid = pending.pop()
        if pid in seen:
            continue
        seen.add(pid)
        item = all_processes.get(pid)
        if item:
            found.append(item)
        pending.extend(children.get(pid, []))
    return found


def _remote_file_bytes(path: str) -> bytes | None:
    """원격(--ssh)은 ssh cat, 로컬은 직접 읽기. 실패 시 None."""
    from actl.core.tmux import REMOTE_SSH_TARGET, _remote_args

    if not REMOTE_SSH_TARGET:
        try:
            return Path(path).read_bytes()
        except OSError:
            return None
    import subprocess as _sp

    try:
        from actl.core.tmux import _no_window

        proc = _sp.run(
            _remote_args(["cat", path]),
            capture_output=True, check=False, timeout=10, **_no_window(),
        )
    except Exception:
        return None
    if proc.returncode:
        return None
    return proc.stdout


def _remote_file_text(path: str) -> str | None:
    raw = _remote_file_bytes(path)
    if raw is None:
        return None
    return raw.decode("utf-8", "replace")


def _remote_resolve_link(path: str) -> str | None:
    """readlink -f 원격 지원 (fd/0 tty 확인용)."""
    from actl.core.tmux import REMOTE_SSH_TARGET, _remote_args

    if not REMOTE_SSH_TARGET:
        try:
            return str(Path(path).resolve(strict=True))
        except OSError:
            return None
    import subprocess as _sp

    try:
        from actl.core.tmux import _no_window

        proc = _sp.run(
            _remote_args(["readlink", "-f", path]),
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", check=False, timeout=10, **_no_window(),
        )
    except Exception:
        return None
    if proc.returncode:
        return None
    return proc.stdout.strip() or None


def process_environment(pid: int) -> dict[str, str]:
    raw = _remote_file_bytes(f"/proc/{pid}/environ")
    if raw is None:
        return {}
    values: dict[str, str] = {}
    for entry in raw.split(b"\0"):
        key, sep, value = entry.partition(b"=")
        if sep:
            values[key.decode("utf-8", "ignore")] = value.decode("utf-8", "ignore")
    return values


def _is_cursor(process: ProcessInfo) -> bool:
    args = process.args.lower()
    return "cursor-agent" in args or "/cursor-agent/" in args


def _is_codex(process: ProcessInfo) -> bool:
    executable = process.args.split(maxsplit=1)[0] if process.args else ""
    return Path(executable).name == "codex"


def _is_opencode(process: ProcessInfo) -> bool:
    return is_opencode_tui(process.args)


def is_opencode_tui(args: str) -> bool:
    """Accept the interactive TUI, never its serve/ACP background processes."""
    tokens = args.replace("\\", "/").split()
    if not tokens or Path(tokens[0].strip("'\"")).name.lower().removesuffix(".exe") != "opencode":
        return False
    return not any(token.strip("'\"").lower() in {"serve", "acp", "web"} for token in tokens[1:])


def _is_cline(process: ProcessInfo) -> bool:
    args = process.args.lower()
    return Path(args.split(maxsplit=1)[0]).name in {"cline", ".cline"} or "/cline" in args


def _is_commandcode(process: ProcessInfo) -> bool:
    args = process.args.lower()
    executable = Path(args.split(maxsplit=1)[0]).name if process.args else ""
    return executable in {"cmd", "cmd.exe", "commandcode"} or "commandcode" in args or "command code" in args


def _is_claude(process: ProcessInfo, profile: Path) -> bool:
    return claude_profile(process) == profile.expanduser().resolve(strict=False)


def _command_has_claude(args: str) -> bool:
    """Recognize direct and wrapped Claude launchers without trusting names alone."""
    for token in args.replace("\\", "/").split():
        name = Path(token.strip("'\"")).name.lower()
        if name.removesuffix(".exe").removesuffix(".cmd") == "claude":
            return True
    return False


def claude_profile(process: ProcessInfo, *, env_reader=None) -> Path | None:
    """Return the selected Claude profile only for a live Claude command."""
    if not _command_has_claude(process.args):
        return None
    configured = (env_reader or process_environment)(process.pid).get("CLAUDE_CONFIG_DIR")
    if not configured:
        return None
    resolved = Path(configured).expanduser().resolve(strict=False)
    from actl.core.tmux import REMOTE_SSH_TARGET
    for agent in ("claude-team", "claude-pro"):
        expected = AGENTS[agent].data_dirs[0].expanduser().resolve(strict=False)
        if resolved == expected or (REMOTE_SSH_TARGET and resolved.name == expected.name):
            return resolved
    return None


def validate_target(agent: str, target: str) -> TargetValidation:
    """Fail closed when a configured pane does not identify as its agent.

    Temporary remote-transport contention is TRANSPORT_BUSY, never DOWN.
    """
    from actl.core.remote_scheduler import is_transport_contention

    try:
        exists = target_exists(target)
    except Exception as exc:
        if is_transport_contention(exc):
            return TargetValidation(
                "TRANSPORT_BUSY",
                target,
                detail="remote transport busy — mapping remains alive (remap not required)",
            )
        return TargetValidation("UNKNOWN", target, detail=f"target probe failed: {exc}")
    if not exists:
        return TargetValidation("DOWN", target, detail="Configured tmux target does not exist")
    try:
        pane_id = pane_field(target, "#{pane_id}")
        command = pane_field(target, "#{pane_current_command}")
        path = pane_field(target, "#{pane_current_path}")
        raw_pid = pane_field(target, "#{pane_pid}")
    except Exception as exc:
        if is_transport_contention(exc):
            return TargetValidation(
                "TRANSPORT_BUSY",
                target,
                detail="remote transport busy — mapping remains alive (remap not required)",
            )
        detail = str(exc)
        if "degraded" in detail.lower() or "backoff" in detail.lower() or "reconnect" in detail.lower():
            return TargetValidation("DEGRADED", target, detail=detail)
        return TargetValidation("DOWN", target, detail=f"Configured tmux target became unavailable: {exc}")
    try:
        pane_pid = int(raw_pid)
    except ValueError:
        return TargetValidation("MISMATCH", target, pane_id, command=command, path=path, detail="Pane PID unavailable")
    try:
        processes = pane_processes(pane_pid)
    except Exception as exc:
        if is_transport_contention(exc):
            return TargetValidation(
                "TRANSPORT_BUSY",
                target,
                pane_id,
                detail="remote transport busy — mapping remains alive (remap not required)",
            )
        return TargetValidation("DEGRADED", target, pane_id, detail=str(exc))
    spec = AGENTS[agent]
    matched: ProcessInfo | None = None
    if agent == "cursor":
        matched = next((p for p in processes if _is_cursor(p)), None)
    elif agent == "codex":
        matched = next((p for p in processes if _is_codex(p)), None)
    elif agent in {"claude-team", "claude-pro"}:
        matched = next((p for p in processes if _is_claude(p, spec.data_dirs[0])), None)
    elif agent == "opencode":
        matched = next((p for p in processes if _is_opencode(p)), None)
    elif agent == "cline":
        matched = next((p for p in processes if _is_cline(p)), None)
    elif agent == "commandcode":
        matched = next((p for p in processes if _is_commandcode(p)), None)
    else:
        expected = {Path(value).name for value in spec.binary_candidates}
        matched = next(
            (p for p in processes if Path(p.args.split(maxsplit=1)[0]).name in expected), None
        )
    if not matched:
        return TargetValidation(
            "MISMATCH", target, pane_id, pane_pid, command, path,
            detail=f"Configured target does not contain a plausible {spec.display_name} process",
        )
    return TargetValidation("UP", target, pane_id, pane_pid, command, path, matched.pid)


def require_valid_target(config: dict, agent: str) -> str:
    from actl.core.config import get_target

    target = get_target(config, agent).target
    validation = validate_target(agent, target)
    if not validation.valid:
        raise ValueError(
            f"Configured target is {validation.state} for {AGENTS[agent].display_name}; sending blocked: "
            f"{validation.detail}"
        )
    return target
