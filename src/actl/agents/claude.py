"""Fail-closed Claude Code transcript correlation and extraction."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from actl.core.models import CopyResult


@dataclass(frozen=True)
class ClaudeResolution:
    session_id: str | None
    transcript: Path | None
    match_method: str
    confidence: str
    latest_assistant_timestamp: str | None = None
    detail: str = ""
    foreground_pid: int | None = None
    tty: str | None = None


def _normal_path(path: str | Path) -> Path:
    return Path(path).expanduser().resolve(strict=False)


def _project_dir(profile_root: Path, pane_path: str) -> Path:
    absolute = str(_normal_path(pane_path))
    if not absolute.startswith("/"):
        raise ValueError("pane path is not absolute")
    # Claude Code's project-name transform: leading slash -> leading hyphen.
    return _normal_path(profile_root) / "projects" / absolute.replace("/", "-")


def _same_path(left: str | Path, right: str | Path) -> bool:
    try:
        return _normal_path(left) == _normal_path(right)
    except OSError:
        return False


def _pid_parent(pid: int) -> int | None:
    from actl.core.validation import _remote_file_text

    try:
        # comm can contain spaces/parentheses; ppid follows its final ") ".
        text = _remote_file_text(f"/proc/{pid}/stat")
        if text is None:
            return None
        tail = text.rsplit(") ", 1)[1].split()
        return int(tail[1])
    except (IndexError, OSError, ValueError):
        return None


def _pid_in_pane_tree(pid: int, pane_pid: int) -> bool:
    """Whether pid is pane_pid or a live descendant; never signals a process."""
    seen: set[int] = set()
    while pid > 0 and pid not in seen:
        if pid == pane_pid:
            return True
        seen.add(pid)
        parent = _pid_parent(pid)
        if parent is None:
            return False
        pid = parent
    return False


def _process_profile_matches(pid: int, profile_root: Path) -> bool:
    """Reject an explicit conflicting CLAUDE_CONFIG_DIR without exposing env."""
    from actl.core.validation import _remote_file_bytes

    raw = _remote_file_bytes(f"/proc/{pid}/environ")
    if raw is None:
        return False
    try:
        entries = raw.split(b"\0")
    except OSError:
        return False
    prefix = b"CLAUDE_CONFIG_DIR="
    configured = next((entry[len(prefix):].decode("utf-8", "surrogateescape") for entry in entries if entry.startswith(prefix)), None)
    # A configured profile is the boundary between Team and Pro.  An inherited
    # or missing value is not enough evidence to cross it.
    return configured is not None and _same_path(configured, profile_root)


def _process_looks_like_claude(pid: int) -> bool:
    from actl.core.validation import _remote_file_bytes

    raw = _remote_file_bytes(f"/proc/{pid}/cmdline")
    if raw is None:
        return False
    command = raw.replace(b"\0", b" ").decode("utf-8", "replace").lower()
    return "claude" in command


def _process_tty(pid: int) -> str | None:
    """Return the controlling terminal without reading pane content."""
    from actl.core.validation import _remote_resolve_link

    tty = _remote_resolve_link(f"/proc/{pid}/fd/0")
    if tty is None:
        return None
    return tty if tty.startswith("/dev/pts/") else None


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _active_session_binding(profile_root: Path, pane_path: str, pane_pid: int | None) -> tuple[str | None, int | None, str | None, str]:
    """Return the one selected-profile session proved to belong to this pane."""
    if pane_pid is None or pane_pid <= 0:
        return None, None, None, "Pane PID unavailable"
    pane_tty = _process_tty(pane_pid)
    if not pane_tty:
        return None, None, None, "Pane terminal unavailable"
    candidates: dict[str, int] = {}
    sessions = _normal_path(profile_root) / "sessions"
    try:
        records = list(sessions.glob("*.json"))
    except OSError:
        records = []
    for path in records:
        record = _read_json(path)
        if not record:
            continue
        pid, session_id, cwd = record.get("pid"), record.get("sessionId"), record.get("cwd")
        if not isinstance(pid, int) or not isinstance(session_id, str) or not isinstance(cwd, str):
            continue
        if not _same_path(cwd, pane_path):
            continue
        if not _pid_in_pane_tree(pid, pane_pid) or not _process_looks_like_claude(pid):
            continue
        if _process_tty(pid) != pane_tty:
            continue
        if not _process_profile_matches(pid, profile_root):
            continue
        previous = candidates.setdefault(session_id, pid)
        if previous != pid:
            return None, None, None, "Multiple Claude PIDs claim the same active session"
    if len(candidates) == 1:
        session_id, foreground_pid = next(iter(candidates.items()))
        return session_id, foreground_pid, pane_tty, ""
    if len(candidates) > 1:
        return None, None, None, "Multiple active Claude sessions match the pane"
    return None, None, None, "No selected-profile Claude session is bound to the pane process"


def _active_session_id(profile_root: Path, pane_path: str, pane_pid: int | None) -> tuple[str | None, str]:
    """Compatibility wrapper for callers that need only the active UUID."""
    session_id, _, _, detail = _active_session_binding(profile_root, pane_path, pane_pid)
    return session_id, detail


def _record_session_id(record: object) -> str | None:
    if not isinstance(record, dict):
        return None
    value = record.get("sessionId") or record.get("session_id")
    return value if isinstance(value, str) else None


def _transcript_matches(path: Path, session_id: str, pane_path: str) -> bool:
    """Validate filename-derived transcript identity from non-sensitive fields."""
    saw_session = saw_cwd = False
    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    # A live append can leave one incomplete final line.
                    continue
                if _record_session_id(record) == session_id:
                    saw_session = True
                if isinstance(record, dict) and isinstance(record.get("cwd"), str) and _same_path(record["cwd"], pane_path):
                    saw_cwd = True
    except OSError:
        return False
    return saw_session and saw_cwd


def _exact_transcript(profile_root: Path, pane_path: str, session_id: str) -> Path | None:
    try:
        project = _project_dir(profile_root, pane_path)
    except (OSError, ValueError):
        return None
    path = project / f"{session_id}.jsonl"
    try:
        # Do not follow a symlink outside this exact project's storage.
        if not path.is_file() or not path.resolve().is_relative_to(project.resolve()):
            return None
    except OSError:
        return None
    return path if _transcript_matches(path, session_id, pane_path) else None


def _assistant_text(record: object) -> str | None:
    if not isinstance(record, dict) or record.get("type") != "assistant" or record.get("isSidechain"):
        return None
    message = record.get("message")
    if not isinstance(message, dict) or message.get("role") != "assistant":
        return None
    content = message.get("content")
    if not isinstance(content, list):
        return None
    chunks = [part.get("text") for part in content if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str)]
    text = "\n".join(chunk for chunk in chunks if chunk.strip()).strip()
    return text or None


def _is_terminal_assistant(record: object) -> bool:
    if not isinstance(record, dict) or not isinstance(record.get("message"), dict):
        return False
    # A missing stop reason may be a live streaming fragment, never a completed
    # response. Claude Code uses end_turn and stop_sequence for visible finals.
    return record["message"].get("stop_reason") in {"end_turn", "stop_sequence"}


def _latest_visible_assistant(path: Path) -> tuple[str | None, str | None]:
    """Reconstruct newest completed visible assistant turn without pane capture."""
    current: list[str] = []
    latest: str | None = None
    latest_timestamp: str | None = None
    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                text = _assistant_text(record)
                if text:
                    current.append(text)
                    if _is_terminal_assistant(record):
                        latest = "\n".join(current).strip()
                        timestamp = record.get("timestamp") if isinstance(record, dict) else None
                        latest_timestamp = timestamp if isinstance(timestamp, str) else None
                        current = []
    except OSError:
        return None, None
    return latest, latest_timestamp


def resolve_claude(profile_root: Path, pane_path: str, pane_pid: int | None = None) -> ClaudeResolution:
    """Resolve selected profile + live pane process + active UUID only."""
    active_id, foreground_pid, tty, active_detail = _active_session_binding(profile_root, pane_path, pane_pid)
    if active_id:
        transcript = _exact_transcript(profile_root, pane_path, active_id)
        if not transcript:
            return ClaudeResolution(active_id, None, "active-session-id", "none", detail="Active session has no exact matching transcript", foreground_pid=foreground_pid, tty=tty)
        _, timestamp = _latest_visible_assistant(transcript)
        return ClaudeResolution(active_id, transcript, "active-session-id", "exact", timestamp, foreground_pid=foreground_pid, tty=tty)
    return ClaudeResolution(None, None, "none", "none", detail=active_detail)


def extract_resolved_claude(resolution: ClaudeResolution) -> CopyResult:
    """Extract only the transcript already proved for this live resolution."""
    if not resolution.transcript:
        return CopyResult(None, "claude-unresolved", "none", resolution.detail)
    text, _ = _latest_visible_assistant(resolution.transcript)
    if not text:
        return CopyResult(None, "claude-unresolved", "none", "No completed visible assistant response in matched session")
    return CopyResult(text, f"claude-session:{resolution.transcript}", resolution.confidence, resolution.detail)


def extract_claude(profile_root: Path, pane_path: str, pane_pid: int | None = None) -> CopyResult:
    """Extract only a proved Claude transcript; never choose by global mtime."""
    return extract_resolved_claude(resolve_claude(profile_root, pane_path, pane_pid))


# --- Managed v1 (Phase 1B) — exact active session + transcript only; never newest-file ---

MANAGED_PARSER_VERSION = "claude-managed-final-v1"


@dataclass(frozen=True)
class ManagedClaudeResolution:
    session_id: str | None = None
    transcript_path: Path | None = None
    code: str = "OK"
    detail: str = ""
    match_method: str = "none"
    foreground_pid: int | None = None


@dataclass(frozen=True)
class ClaudeUserTurnBinding:
    session_id: str
    turn_id: str
    event_index: int
    byte_end: int
    code: str = "OK"
    detail: str = ""


@dataclass(frozen=True)
class ManagedFinalResult:
    code: str
    detail: str = ""
    packet: dict[str, Any] | None = None


def resolve_claude_managed(
    profile_root: Path,
    pane_path: str,
    pane_pid: int | None = None,
) -> ManagedClaudeResolution:
    """Managed identity: exact active session binding + exact transcript only.

    Never adopts newest-file or cross-profile transcripts.
    """
    active_id, foreground_pid, _tty, active_detail = _active_session_binding(
        profile_root, pane_path, pane_pid
    )
    if not active_id:
        return ManagedClaudeResolution(
            code="DOWN",
            detail=active_detail or "No selected-profile Claude session is bound to the pane process",
            foreground_pid=foreground_pid,
        )
    transcript = _exact_transcript(profile_root, pane_path, active_id)
    if not transcript:
        return ManagedClaudeResolution(
            session_id=active_id,
            code="AMBIGUOUS_SESSION",
            detail="Active session has no exact matching transcript",
            match_method="active-session-id",
            foreground_pid=foreground_pid,
        )
    return ManagedClaudeResolution(
        session_id=active_id,
        transcript_path=transcript,
        code="OK",
        match_method="active-session-id",
        foreground_pid=foreground_pid,
    )


def read_claude_jsonl_forward(path: Path, cursor: dict[str, Any]):
    """Forward-read complete JSONL records; Claude transcripts are flat objects."""
    from actl.agents.codex import read_jsonl_forward

    return read_jsonl_forward(path, cursor)


def _claude_user_text(record: dict[str, Any]) -> str:
    if record.get("type") != "user":
        return ""
    message = record.get("message")
    if not isinstance(message, dict) or message.get("role") != "user":
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for part in content:
        if isinstance(part, str):
            parts.append(part)
            continue
        if not isinstance(part, dict):
            continue
        text = part.get("text")
        if part.get("type") == "text" and isinstance(text, str):
            parts.append(text)
    return "\n".join(parts)


def bind_claude_user_turn(
    cursor: dict[str, Any],
    wire_prompt: str,
    events: list[dict[str, Any]],
    *,
    session_id: str | None = None,
) -> ClaudeUserTurnBinding:
    """Bind exactly one post-cursor user record whose text contains wirePrompt."""
    matches: list[tuple[int, dict[str, Any]]] = []
    for idx, event in enumerate(events):
        if not isinstance(event, dict) or event.get("type") != "user":
            continue
        if event.get("isSidechain"):
            continue
        text = _claude_user_text(event)
        if wire_prompt in text:
            matches.append((idx, event))
    byte_fallback = int(cursor.get("byteOffset") or cursor.get("byte_offset") or 0)
    if not matches:
        return ClaudeUserTurnBinding(
            "", "", -1, byte_fallback, code="RESULT_NOT_FINAL", detail="no matching user turn after cursor"
        )
    if len(matches) > 1:
        return ClaudeUserTurnBinding(
            "", "", -1, byte_fallback, code="AMBIGUOUS_SESSION", detail="multiple user turns match wirePrompt"
        )
    idx, event = matches[0]
    sid = session_id or _record_session_id(event) or ""
    if not isinstance(sid, str) or not sid:
        return ClaudeUserTurnBinding(
            "", "", idx, byte_fallback, code="RESULT_NOT_FINAL", detail="matched user turn lacks sessionId"
        )
    user_id = event.get("uuid") or event.get("id")
    if isinstance(user_id, str) and user_id:
        turn_id = user_id
    else:
        turn_id = f"user-{idx}"
    return ClaudeUserTurnBinding(
        session_id=sid,
        turn_id=turn_id,
        event_index=idx,
        byte_end=byte_fallback,
        code="OK",
    )


def _stable_turn_id(session_id: str, user_index: int, byte_offset: int) -> str:
    import hashlib

    payload = f"{session_id}:{user_index}:{byte_offset}".encode("utf-8")
    return "turn_" + hashlib.sha256(payload).hexdigest()[:24]


def _canonical_json_bytes(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _sha256_hex(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


def collect_claude_final(
    *,
    path: Path,
    cursor: dict[str, Any],
    wire_prompt: str,
    command_id: str,
    runtime_id: str,
    prompt_sha256: str,
    session_id: str | None = None,
    cancel_requested: bool = False,
) -> ManagedFinalResult:
    """Admit FINAL for exactly one post-bind terminal assistant.

    Claude has no Codex-style task_complete; terminal predicate is provider-specific:
    stop_reason in {end_turn, stop_sequence}, not tool_use, not sidechain.
    """
    import hashlib
    from datetime import datetime, timezone

    forward = read_claude_jsonl_forward(path, cursor)
    if forward.code != "OK":
        return ManagedFinalResult(
            code="AMBIGUOUS_SESSION" if forward.code == "SOURCE_INTEGRITY" else forward.code,
            detail=forward.detail,
        )
    if forward.pending_incomplete and not forward.events:
        return ManagedFinalResult(code="RESULT_NOT_FINAL", detail="incomplete JSONL tail")

    binding = bind_claude_user_turn(cursor, wire_prompt, forward.events, session_id=session_id)
    if binding.code != "OK":
        return ManagedFinalResult(code=binding.code, detail=binding.detail)

    # Exactly one terminal assistant after the bound user turn (no task_complete).
    terminals: list[tuple[int, str, str, dict[str, Any]]] = []
    for idx, event in enumerate(forward.events):
        if idx <= binding.event_index:
            continue
        if not isinstance(event, dict) or event.get("type") != "assistant":
            continue
        if event.get("isSidechain"):
            continue
        if not _is_terminal_assistant(event):
            continue
        text = _assistant_text(event)
        if not text:
            continue
        item_id = event.get("uuid") or event.get("id")
        if not isinstance(item_id, str) or not item_id:
            item_id = f"final-{idx}"
        terminals.append((idx, text, item_id, event))

    if not terminals:
        return ManagedFinalResult(
            code="RESULT_NOT_FINAL",
            detail="no terminal assistant (end_turn/stop_sequence) after bound user turn",
        )
    if len(terminals) != 1:
        return ManagedFinalResult(
            code="AMBIGUOUS_SESSION",
            detail="multiple terminal assistant finals; refusing latest selection",
        )

    _idx, raw_text, final_item_id, assistant_event = terminals[0]
    if cancel_requested:
        return ManagedFinalResult(code="RESULT_NOT_FINAL", detail="CANCEL_REQUESTED blocks automatic FINAL admission")

    assistant_turn = assistant_event.get("uuid") or assistant_event.get("id")
    if isinstance(assistant_turn, str) and assistant_turn:
        turn_id = assistant_turn
    else:
        turn_id = _stable_turn_id(
            binding.session_id,
            binding.event_index,
            int(cursor.get("byteOffset") or cursor.get("byte_offset") or 0),
        )

    try:
        st = path.stat()
        byte_start = int(cursor.get("byteOffset") or cursor.get("byte_offset") or 0)
        byte_end = forward.next_byte_offset
        with path.open("rb") as fh:
            fh.seek(byte_start)
            slice_bytes = fh.read(byte_end - byte_start)
        slice_sha = hashlib.sha256(slice_bytes).hexdigest()
    except OSError as exc:
        return ManagedFinalResult(code="AMBIGUOUS_SESSION", detail=f"source slice read failed: {exc}")

    result_id = "res1_" + _sha256_hex(
        _canonical_json_bytes({
            "commandId": command_id,
            "finalItemId": final_item_id,
            "runtimeId": runtime_id,
            "sessionId": binding.session_id,
            "sourceSliceSha256": slice_sha,
            "turnId": turn_id,
        })
    )
    observed_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    packet = {
        "contractVersion": 1,
        "resultId": result_id,
        "commandId": command_id,
        "runtimeId": runtime_id,
        "sessionId": binding.session_id,
        "turnId": turn_id,
        "promptSha256": prompt_sha256,
        "finalItemId": final_item_id,
        "sourceRef": {
            "path": str(path),
            "dev": str(st.st_dev),
            "inode": str(st.st_ino),
            "byteStart": byte_start,
            "byteEnd": byte_end,
            "sourceSliceSha256": slice_sha,
        },
        "parserVersion": MANAGED_PARSER_VERSION,
        "observedAt": observed_at,
        "completionKind": "RESPONSE_COMPLETE",
        "rawFinalText": raw_text,
        "rawFinalTextSha256": _sha256_hex(raw_text.encode("utf-8")),
    }
    return ManagedFinalResult(code="FINAL", packet=packet)
