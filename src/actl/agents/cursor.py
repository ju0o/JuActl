from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from actl.core.models import CopyResult
from actl.core.tmux import pane_field
from actl.core.validation import pane_processes


@dataclass(frozen=True)
class CursorResolution:
    session_id: str | None = None
    chat_dir: Path | None = None
    store_db: Path | None = None
    match_method: str = "none"
    confidence: str = "none"
    detail: str = ""


def _same_path(left: str | Path, right: str | Path) -> bool:
    return Path(left).expanduser().resolve(strict=False) == Path(right).expanduser().resolve(strict=False)


def _cursor_process(pid: int | None, target: str) -> int | None:
    if pid is not None:
        for process in pane_processes(pid):
            args = process.args.lower()
            if "cursor-agent" in args or "/cursor-agent/" in args:
                return process.pid
        return None
    try:
        pane_pid = int(pane_field(target, "#{pane_pid}"))
    except ValueError:
        return None
    return _cursor_process(pane_pid, target)


def _open_store_dbs(cursor_pid: int, chats_root: Path) -> list[Path]:
    """Read only the Cursor process FD links; never discover by mtime."""
    root = chats_root.expanduser().resolve(strict=False)
    found: set[Path] = set()
    try:
        fds = list(Path(f"/proc/{cursor_pid}/fd").iterdir())
    except OSError:
        return []
    for fd in fds:
        try:
            path = Path(os.readlink(fd)).resolve(strict=False)
        except OSError:
            continue
        if path.name != "store.db":
            continue
        try:
            path.relative_to(root)
        except ValueError:
            continue
        if path.is_file():
            found.add(path)
    return sorted(found)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _store_metadata(db: Path) -> dict[str, Any] | None:
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            rows = con.execute("SELECT value FROM meta").fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return None
    for (value,) in rows:
        raw = value.decode("utf-8", errors="ignore") if isinstance(value, bytes) else str(value)
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            # Cursor 2026.09 stores this JSON as text-encoded hexadecimal.
            try:
                decoded = json.loads(bytes.fromhex(raw).decode("utf-8"))
            except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
                continue
        if isinstance(decoded, dict) and isinstance(decoded.get("agentId"), str):
            return decoded
    return None


def resolve_cursor(chats_root: Path, target: str, cursor_pid: int | None = None) -> CursorResolution:
    """Bind Cursor copy to the selected pane's live process-owned conversation."""
    root = chats_root.expanduser()
    pid = _cursor_process(cursor_pid, target)
    if pid is None:
        return CursorResolution(detail="No live Cursor Agent process belongs to the selected target")
    try:
        pane_cwd = pane_field(target, "#{pane_current_path}")
        pane_title = pane_field(target, "#{pane_title}")
    except Exception as exc:
        return CursorResolution(detail=f"Could not read selected Cursor pane: {exc}")
    stores = _open_store_dbs(pid, root)
    if len(stores) != 1:
        return CursorResolution(detail="Active Cursor process does not identify exactly one open chat store.db")
    db = stores[0]
    chat_dir = db.parent
    chat = _read_json(chat_dir / "meta.json")
    store = _store_metadata(db)
    if not chat or not store:
        return CursorResolution(detail="Active Cursor chat metadata is incomplete")
    session_id = store.get("agentId")
    if not isinstance(session_id, str) or session_id != chat_dir.name:
        return CursorResolution(detail="Cursor store agentId does not match its chat directory")
    if chat.get("hasConversation") is not True:
        return CursorResolution(detail="Cursor chat is not a conversation")
    if not isinstance(chat.get("cwd"), str) or not _same_path(chat["cwd"], pane_cwd):
        return CursorResolution(detail="Cursor chat workspace does not match selected pane cwd")
    if not isinstance(chat.get("title"), str) or chat["title"] != pane_title:
        return CursorResolution(detail="Cursor chat title does not match selected pane title")
    return CursorResolution(
        session_id=session_id,
        chat_dir=chat_dir,
        store_db=db,
        match_method="cursor-process-open-store-db + meta-cwd-title-agent-id",
        confidence="exact",
    )


def _visible_assistant_text(message: Any) -> str | None:
    """Accept only actual Cursor assistant text parts, never nested/tool records."""
    if not isinstance(message, dict) or message.get("role") != "assistant":
        return None
    content = message.get("content")
    if not isinstance(content, list):
        return None
    parts = [
        item["text"].strip()
        for item in content
        if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str) and item["text"].strip()
    ]
    return "\n".join(parts) or None


def _json_blob(value: Any) -> dict[str, Any] | None:
    raw = value.decode("utf-8", errors="ignore") if isinstance(value, bytes) else str(value)
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return decoded if isinstance(decoded, dict) else None


def extract_cursor(resolution: CursorResolution) -> CopyResult | None:
    """Extract the visible assistant response from one already-correlated chat only."""
    if resolution.store_db is None or resolution.confidence != "exact":
        return None
    try:
        con = sqlite3.connect(f"file:{resolution.store_db}?mode=ro", uri=True)
        try:
            rows = con.execute("SELECT rowid, data FROM blobs ORDER BY rowid ASC").fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return None
    latest_user_rowid = -1
    parsed: list[tuple[int, dict[str, Any]]] = []
    for rowid, blob in rows:
        message = _json_blob(blob)
        if message is None:
            continue
        parsed.append((int(rowid), message))
        if message.get("role") == "user":
            latest_user_rowid = int(rowid)
    visible: list[str] = []
    for rowid, message in parsed:
        if rowid <= latest_user_rowid:
            continue
        text = _visible_assistant_text(message)
        if text:
            visible.append(text)
    if not visible:
        return None
    return CopyResult("\n".join(visible), f"cursor-db:{resolution.store_db}", "exact")
