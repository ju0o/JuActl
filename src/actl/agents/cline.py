"""Fail-closed Cline session correlation from its live session registry."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from actl.core.models import CopyResult
from actl.core.tmux import pane_field
from actl.core.validation import pane_processes


@dataclass(frozen=True)
class ClineResolution:
    session_id: str | None = None
    messages_path: Path | None = None
    match_method: str = "none"
    confidence: str = "none"
    detail: str = ""


def _data_root(default_root: Path, pid: int) -> Path:
    """Respect Cline's ``--data-dir`` in either ``--data-dir <path>`` or
    ``--data-dir=<path>`` form, or fall back to the default data directory."""
    try:
        values = [item.decode("utf-8", "surrogateescape") for item in Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")]
    except OSError:
        return default_root.expanduser() / "data"
    data_dir = None
    for index, token in enumerate(values):
        if token == "--data-dir" and index + 1 < len(values):
            data_dir = values[index + 1]
            break
        if token.startswith("--data-dir="):
            data_dir = token.partition("=")[2]
            break
    if not data_dir:
        return default_root.expanduser() / "data"
    return Path(data_dir).expanduser()


def resolve_cline(default_root: Path, target: str, agent_pid: int | None) -> ClineResolution:
    """Bind the selected pane's live Cline process to exactly one DB session."""
    if agent_pid is None:
        return ClineResolution(detail="No live Cline process belongs to the selected target")
    try:
        cwd = str(Path(pane_field(target, "#{pane_current_path}")).resolve(strict=False))
    except OSError as exc:
        return ClineResolution(detail=f"Could not read selected Cline pane: {exc}")
    db = _data_root(default_root, agent_pid) / "db" / "sessions.db"
    pids = {process.pid for process in pane_processes(agent_pid)}
    if not pids:
        return ClineResolution(detail="Selected Cline process tree is unavailable")
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            rows = con.execute("SELECT session_id, pid, cwd, messages_path FROM sessions WHERE interactive=1 AND status IN ('idle', 'completed')").fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return ClineResolution(detail="Cline session registry is unavailable")
    matches = []
    for session_id, pid, session_cwd, messages_path in rows:
        if not isinstance(session_id, str) or not isinstance(pid, int) or pid not in pids:
            continue
        if not isinstance(session_cwd, str) or str(Path(session_cwd).resolve(strict=False)) != cwd:
            continue
        if isinstance(messages_path, str) and messages_path and Path(messages_path).expanduser().is_file():
            matches.append((session_id, Path(messages_path).expanduser()))
    if len(matches) != 1:
        return ClineResolution(detail="Could not uniquely correlate the live Cline process with one completed interactive session")
    session_id, messages = matches[0]
    return ClineResolution(session_id, messages, "cline-session-db(pid + cwd + messages-path)", "exact")


def extract_cline(resolution: ClineResolution) -> CopyResult | None:
    """Return only persisted assistant text after the latest user/tool record."""
    if resolution.confidence != "exact" or resolution.messages_path is None:
        return None
    try:
        document = json.loads(resolution.messages_path.read_text(encoding="utf-8"))
        messages = document.get("messages") if isinstance(document, dict) else None
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(messages, list):
        return None
    latest_user = max((index for index, message in enumerate(messages) if isinstance(message, dict) and message.get("role") == "user"), default=-1)
    text = []
    for message in messages[latest_user + 1:]:
        if not isinstance(message, dict) or message.get("role") != "assistant" or not isinstance(message.get("content"), list):
            continue
        text.extend(part["text"].strip() for part in message["content"] if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str) and part["text"].strip())
    return CopyResult("\n".join(text), f"cline-session:{resolution.messages_path}", "exact") if text else None
