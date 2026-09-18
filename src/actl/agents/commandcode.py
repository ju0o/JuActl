"""Fail-closed CommandCode (AWS ``cmd``) Result correlation.

CommandCode stores one session per ``projects/<slug>/<uuid>.jsonl``. The file
opens with a ``session`` record carrying the real ``cwd``, followed by
``message`` records with ``message.role`` and ``content[].type == "text"``
(plus ``thinking``/``tool_use``/``tool_result`` parts that are never copied).

Resolution requires the session to be provably owned by the live pane process:
  * the process holds exactly one matching session file open (FD ownership), or
  * exactly one matching session file was written within the process lifetime
    (same deterministic adoption rule as bare OpenCode TUIs).
Zero or several candidates fail closed; never a global "newest session" guess.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from actl.core.models import CopyResult
from actl.core.tmux import pane_field
from actl.core.validation import pane_processes


@dataclass(frozen=True)
class CommandCodeResolution:
    session_id: str | None = None
    session_path: Path | None = None
    match_method: str = "none"
    confidence: str = "none"
    detail: str = ""
    foreground_pid: int | None = None


def _same_path(left: str | Path, right: str | Path) -> bool:
    return Path(left).expanduser().resolve(strict=False) == Path(right).expanduser().resolve(strict=False)


def _is_commandcode_args(process) -> bool:
    args = (process.args or "").lower()
    executable = args.split(maxsplit=1)[0]
    return Path(executable).name in {"cmd", "cmd.exe", "commandcode"} or "commandcode" in args or "command code" in args


def _commandcode_process(pid: int | None, target: str) -> int | None:
    if pid is not None:
        for process in pane_processes(pid):
            if _is_commandcode_args(process):
                return process.pid
        return None
    try:
        pane_pid = int(pane_field(target, "#{pane_pid}"))
    except Exception:
        # A missing/stale tmux server is an unresolved target, never a copy crash.
        return None
    return _commandcode_process(pane_pid, target)


def _open_paths(pid: int) -> list[Path]:
    try:
        fds = list(Path(f"/proc/{pid}/fd").iterdir())
    except OSError:
        return []
    found: list[Path] = []
    for fd in fds:
        try:
            found.append(Path(os.readlink(fd)))
        except OSError:
            continue
    return found


def _session_cwd(path: Path) -> str | None:
    """Read the session record's cwd from the first few lines of a session file."""
    try:
        with path.open(encoding="utf-8") as fh:
            for _ in range(20):
                line = fh.readline()
                if not line:
                    return None
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("type") == "session" and isinstance(record.get("cwd"), str):
                    return record["cwd"]
    except OSError:
        return None
    return None


def _session_files(projects_root: Path) -> list[Path]:
    root = projects_root.expanduser()
    found: list[Path] = []
    try:
        for slug_dir in root.iterdir():
            if not slug_dir.is_dir():
                continue
            for path in slug_dir.iterdir():
                if not path.is_file() or path.suffix != ".jsonl":
                    continue
                if path.name.endswith(".checkpoints.jsonl") or path.name in {"config.json", ".meta.json"}:
                    continue
                found.append(path)
    except OSError:
        return []
    return sorted(found)


def _cwd_matching_sessions(projects_root: Path, pane_cwd: str) -> list[Path]:
    return [path for path in _session_files(projects_root) if _session_cwd(path) and _same_path(_session_cwd(path), pane_cwd)]


def _open_session_files(pid: int, projects_root: Path, pane_cwd: str) -> list[Path]:
    root = projects_root.expanduser()
    found: set[Path] = set()
    for path in _open_paths(pid):
        try:
            resolved = path.resolve(strict=False)
            resolved.relative_to(root)
        except (OSError, ValueError):
            continue
        if resolved.suffix != ".jsonl" or resolved.name.endswith(".checkpoints.jsonl"):
            continue
        cwd = _session_cwd(resolved)
        if cwd and _same_path(cwd, pane_cwd) and resolved.is_file():
            found.add(resolved)
    return sorted(found)


def _proc_start_ms(pid: int) -> int | None:
    """Wall-clock start time (epoch ms) of a live process, read-only from /proc."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
        tail = stat.rsplit(") ", 1)[1].split()
        starttime_ticks = int(tail[19])
        clk_tck = os.sysconf("SC_CLK_TCK")
        uptime = float(Path("/proc/uptime").read_text(encoding="utf-8").split()[0])
        return int((time.time() - uptime + starttime_ticks / clk_tck) * 1000)
    except Exception:
        return None


def _adopt_live_session(projects_root: Path, pid: int, pane_cwd: str) -> tuple[Path | None, str]:
    """Adopt the session this process provably owns (written within its lifetime)."""
    start_ms = _proc_start_ms(pid)
    if start_ms is None:
        return None, "Could not read live commandcode process start time"
    candidates: list[Path] = []
    for path in _cwd_matching_sessions(projects_root, pane_cwd):
        try:
            mtime_ms = int(path.stat().st_mtime * 1000)
        except OSError:
            continue
        if mtime_ms >= start_ms - 1000:
            candidates.append(path)
    if not candidates:
        return None, "No commandcode session in this pane directory was active during this process"
    if len(candidates) > 1:
        return None, f"Multiple commandcode sessions were active in this pane directory ({len(candidates)}); cannot adopt safely"
    return candidates[0], ""


def resolve_commandcode(projects_root: Path, target: str, agent_pid: int | None = None) -> CommandCodeResolution:
    """Bind CommandCode copy to the selected pane's live session only."""
    pid = _commandcode_process(agent_pid, target)
    if pid is None:
        return CommandCodeResolution(detail="No live CommandCode process belongs to the selected target")
    try:
        pane_cwd = pane_field(target, "#{pane_current_path}")
    except Exception as exc:
        return CommandCodeResolution(detail=f"Could not read selected CommandCode pane: {exc}", foreground_pid=pid)

    owned = _open_session_files(pid, projects_root, pane_cwd)
    if len(owned) > 1:
        return CommandCodeResolution(detail="Active CommandCode process owns multiple matching session files", foreground_pid=pid)
    session: Path | None
    method: str
    if len(owned) == 1:
        session = owned[0]
        method = "commandcode-process-open-session + cwd"
    else:
        session, detail = _adopt_live_session(projects_root, pid, pane_cwd)
        if session is None:
            return CommandCodeResolution(detail=detail, foreground_pid=pid)
        method = "commandcode cwd + active-within-lifetime adoption"
    session_id = session.name[: -len(".jsonl")] if session.name.endswith(".jsonl") else session.name
    return CommandCodeResolution(
        session_id=session_id,
        session_path=session,
        match_method=method,
        confidence="exact",
        foreground_pid=pid,
    )


def _assistant_text(message: Any) -> str | None:
    if not isinstance(message, dict) or message.get("role") != "assistant":
        return None
    content = message.get("content")
    if not isinstance(content, list):
        return None
    parts = [
        part["text"].strip()
        for part in content
        if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str) and part["text"].strip()
    ]
    return "\n".join(parts) or None


def extract_commandcode(resolution: CommandCodeResolution) -> CopyResult | None:
    """Extract the final visible assistant text for one correlated session only."""
    if resolution.confidence != "exact" or resolution.session_path is None:
        return None
    records: list[dict[str, Any]] = []
    try:
        with resolution.session_path.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict) and record.get("type") == "message":
                    records.append(record)
    except OSError:
        return None
    if not records:
        return None
    # A turn still in progress ends on a user/tool record: never copy a partial.
    if records[-1].get("message", {}).get("role") != "assistant":
        return None
    # Return the final assistant text record (the visible response of the
    # latest turn), even when it is preceded by interleaved tool turns.
    for record in reversed(records):
        text = _assistant_text(record.get("message"))
        if text:
            return CopyResult(text, f"commandcode-session:{resolution.session_path}", "exact")
    return None
