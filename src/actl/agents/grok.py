"""Read only the Grok session owned by the selected live process."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from actl.core.models import CopyResult
from actl.core.tmux import pane_field
from actl.core.validation import pane_processes


@dataclass(frozen=True)
class GrokResolution:
    session_id: str | None = None
    session_dir: Path | None = None
    match_method: str = "none"
    confidence: str = "none"
    detail: str = ""
    foreground_pid: int | None = None


def _grok_pid(target: str) -> int | None:
    try:
        pane_pid = int(pane_field(target, "#{pane_pid}"))
    except (ValueError, OSError):
        return None
    for process in pane_processes(pane_pid):
        if Path(process.args.split(maxsplit=1)[0]).name == "grok":
            return process.pid
    return None


def _grok_process(pid: int | None, target: str) -> int | None:
    """Resolve live grok pid. Prefer explicit pid tree (Managed socket); else pane_field."""
    if pid is not None:
        for process in pane_processes(pid):
            executable = process.args.split(maxsplit=1)[0] if process.args else ""
            if Path(executable).name == "grok":
                return process.pid
        return None
    return _grok_pid(target)


def _fd_targets(pid: int) -> list[str]:
    """fd 심볼릭링크 목적지 목록. 원격(--ssh)은 ssh ls+readlink 경유."""
    from actl.core.tmux import REMOTE_SSH_TARGET, _remote_args

    if not REMOTE_SSH_TARGET:
        try:
            fds = list(Path(f"/proc/{pid}/fd").iterdir())
        except OSError:
            return []
        out = []
        for fd in fds:
            try:
                out.append(os.readlink(fd))
            except OSError:
                continue
        return out
    import subprocess as _sp

    try:
        ls = _sp.run(
            _remote_args(["ls", f"/proc/{pid}/fd"]),
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", check=False, timeout=10,
        )
    except Exception:
        return []
    if ls.returncode:
        return []
    out = []
    for name in ls.stdout.split():
        try:
            rl = _sp.run(
                _remote_args(["readlink", f"/proc/{pid}/fd/{name}"]),
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", check=False, timeout=10,
            )
        except Exception:
            continue
        if rl.returncode:
            continue
        out.append(rl.stdout.strip())
    return out


def _open_event_files(pid: int, root: Path) -> list[Path]:
    found: set[Path] = set()
    for target in _fd_targets(pid):
        try:
            path = Path(target).resolve(strict=False)
            path.relative_to(root)
        except (OSError, ValueError):
            continue
        if path.name == "events.jsonl" and path.parent.name:
            found.add(path)
    return sorted(found)


def _event_session_id(path: Path) -> str | None:
    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("type") == "turn_started" and isinstance(record.get("session_id"), str):
                    return record["session_id"]
    except OSError:
        return None
    return None


def _resolve_cwd(target: str, workspace: str | Path | None) -> Path:
    if workspace is not None:
        return Path(workspace).expanduser().resolve(strict=False)
    return Path(pane_field(target, "#{pane_current_path}")).resolve(strict=False)


def resolve_grok(
    root: Path,
    target: str,
    grok_pid: int | None = None,
    *,
    workspace: str | Path | None = None,
) -> GrokResolution:
    """Require a process-owned event stream, its session ID, and the pane cwd."""
    pid = _grok_process(grok_pid, target)
    if pid is None:
        return GrokResolution(detail="No live Grok process belongs to the selected target")
    try:
        cwd = _resolve_cwd(target, workspace)
    except OSError as exc:
        return GrokResolution(detail=f"Could not read selected Grok pane: {exc}", foreground_pid=pid)
    sessions = root.expanduser().resolve(strict=False) / "sessions"
    files = _open_event_files(pid, sessions)
    if len(files) == 0:
        return GrokResolution(
            detail="Live Grok process has no open session event stream",
            foreground_pid=pid,
        )
    if len(files) > 1:
        return GrokResolution(
            detail="Live Grok process has multiple open session event streams",
            foreground_pid=pid,
        )
    event = files[0]
    session_dir = event.parent
    if unquote(session_dir.parent.name) != str(cwd):
        return GrokResolution(
            detail="Grok session workspace does not match selected pane cwd",
            foreground_pid=pid,
        )
    session_id = _event_session_id(event)
    if not session_id or session_id != session_dir.name:
        return GrokResolution(
            detail="Grok event stream does not identify its session directory",
            foreground_pid=pid,
        )
    return GrokResolution(
        session_id,
        session_dir,
        "grok-process-open-events + cwd + event-session-id",
        "exact",
        foreground_pid=pid,
    )


def extract_grok(resolution: GrokResolution) -> CopyResult | None:
    """Return only visible assistant text after the latest user message."""
    if resolution.confidence != "exact" or resolution.session_dir is None:
        return None
    history = resolution.session_dir / "chat_history.jsonl"
    latest_user = -1
    records: list[tuple[int, dict]] = []
    try:
        with history.open(encoding="utf-8") as fh:
            for index, line in enumerate(fh):
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue
                records.append((index, record))
                if record.get("type") == "user":
                    latest_user = index
    except OSError:
        return None
    text = [
        record["content"].strip()
        for index, record in records
        if index > latest_user
        and record.get("type") == "assistant"
        and isinstance(record.get("content"), str)
        and record["content"].strip()
    ]
    if not text:
        return None
    return CopyResult("\n".join(text), f"grok-session:{history}", "exact")


# --- Managed v1 (Phase 1B) — exact process-owned events.jsonl only; never newest-file ---

MANAGED_PARSER_VERSION = "grok-managed-final-v1"


@dataclass(frozen=True)
class ManagedGrokResolution:
    session_id: str | None = None
    session_dir: Path | None = None
    chat_history_path: Path | None = None
    code: str = "OK"
    detail: str = ""
    match_method: str = "none"
    foreground_pid: int | None = None


@dataclass(frozen=True)
class GrokUserTurnBinding:
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


def resolve_grok_managed(
    root: Path,
    target: str,
    grok_pid: int | None = None,
    *,
    workspace: str | Path | None = None,
) -> ManagedGrokResolution:
    """Managed identity: exact process-owned events.jsonl + cwd + session_id only.

    Never adopts newest-file or ambient session picks.
    """
    resolution = resolve_grok(root, target, grok_pid, workspace=workspace)
    if resolution.confidence != "exact" or resolution.session_dir is None or not resolution.session_id:
        detail = resolution.detail or "No exact process-owned Grok session"
        code = "AMBIGUOUS_SESSION" if "multiple open" in detail else "DOWN"
        return ManagedGrokResolution(
            code=code,
            detail=detail,
            foreground_pid=resolution.foreground_pid,
        )
    history = resolution.session_dir / "chat_history.jsonl"
    if not history.is_file():
        return ManagedGrokResolution(
            session_id=resolution.session_id,
            session_dir=resolution.session_dir,
            code="AMBIGUOUS_SESSION",
            detail="Exact session has no chat_history.jsonl",
            match_method=resolution.match_method,
            foreground_pid=resolution.foreground_pid,
        )
    return ManagedGrokResolution(
        session_id=resolution.session_id,
        session_dir=resolution.session_dir,
        chat_history_path=history,
        code="OK",
        match_method=resolution.match_method,
        foreground_pid=resolution.foreground_pid,
    )


def read_grok_jsonl_forward(path: Path, cursor: dict[str, Any]):
    """Forward-read complete JSONL records from chat_history.jsonl."""
    from actl.agents.codex import read_jsonl_forward

    return read_jsonl_forward(path, cursor)


def _grok_content_text(content: Any) -> str:
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


def _grok_user_text(record: dict[str, Any]) -> str:
    if record.get("type") != "user":
        return ""
    return _grok_content_text(record.get("content"))


def _grok_assistant_text(record: dict[str, Any]) -> str:
    if record.get("type") != "assistant":
        return ""
    return _grok_content_text(record.get("content")).strip()


def _assistant_has_tool_calls(record: dict[str, Any]) -> bool:
    calls = record.get("tool_calls")
    return isinstance(calls, list) and len(calls) > 0


def bind_grok_user_turn(
    cursor: dict[str, Any],
    wire_prompt: str,
    events: list[dict[str, Any]],
    *,
    session_id: str | None = None,
) -> GrokUserTurnBinding:
    """Bind exactly one post-cursor type==user record whose content contains wirePrompt."""
    matches: list[tuple[int, dict[str, Any]]] = []
    for idx, event in enumerate(events):
        if not isinstance(event, dict) or event.get("type") != "user":
            continue
        text = _grok_user_text(event)
        if wire_prompt in text:
            matches.append((idx, event))
    byte_fallback = int(cursor.get("byteOffset") or cursor.get("byte_offset") or 0)
    if not matches:
        return GrokUserTurnBinding(
            "", "", -1, byte_fallback, code="RESULT_NOT_FINAL", detail="no matching user turn after cursor"
        )
    if len(matches) > 1:
        return GrokUserTurnBinding(
            "", "", -1, byte_fallback, code="AMBIGUOUS_SESSION", detail="multiple user turns match wirePrompt"
        )
    idx, event = matches[0]
    sid = session_id if isinstance(session_id, str) and session_id else ""
    if not sid:
        raw_sid = event.get("session_id") or event.get("sessionId")
        sid = raw_sid if isinstance(raw_sid, str) else ""
    user_id = event.get("id") or event.get("uuid")
    if isinstance(user_id, str) and user_id:
        turn_id = user_id
    else:
        turn_id = f"user-{idx}"
    return GrokUserTurnBinding(
        session_id=sid or "",
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


def collect_grok_final(
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
    """Admit FINAL for exactly one post-bind assistant-visible completion.

    Grok chat_history has no Codex task_complete / Claude stop_reason analogue that is
    reliably present on every turn. events.jsonl exposes turn_started but not a stable
    turn_completed paired with the bound user. Provider-specific Managed predicate:

    - Bind exactly one post-cursor user whose content contains wirePrompt.
    - After that user until the next user: require exactly one assistant message with
      non-empty visible text and no tool_calls (incomplete tool/reasoning-only turns
      are RESULT_NOT_FINAL).
    - Multiple matching users or multiple assistant finals → AMBIGUOUS_SESSION.
    """
    import hashlib
    from datetime import datetime, timezone

    forward = read_grok_jsonl_forward(path, cursor)
    if forward.code != "OK":
        return ManagedFinalResult(
            code="AMBIGUOUS_SESSION" if forward.code == "SOURCE_INTEGRITY" else forward.code,
            detail=forward.detail,
        )
    if forward.pending_incomplete and not forward.events:
        return ManagedFinalResult(code="RESULT_NOT_FINAL", detail="incomplete JSONL tail")

    binding = bind_grok_user_turn(cursor, wire_prompt, forward.events, session_id=session_id)
    if binding.code != "OK":
        return ManagedFinalResult(code=binding.code, detail=binding.detail)

    sid = binding.session_id or (session_id if isinstance(session_id, str) else "") or ""

    assistants: list[tuple[int, str, dict[str, Any]]] = []
    for idx, event in enumerate(forward.events):
        if idx <= binding.event_index:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "user":
            break
        if event.get("type") == "assistant":
            text = _grok_assistant_text(event)
            if text:
                assistants.append((idx, text, event))

    if not assistants:
        return ManagedFinalResult(
            code="RESULT_NOT_FINAL",
            detail="no assistant-visible final after bound user turn",
        )
    if len(assistants) != 1:
        return ManagedFinalResult(
            code="AMBIGUOUS_SESSION",
            detail="multiple assistant finals after bound user; refusing latest selection",
        )

    _idx, raw_text, assistant_event = assistants[0]
    if _assistant_has_tool_calls(assistant_event):
        return ManagedFinalResult(
            code="RESULT_NOT_FINAL",
            detail="assistant still has tool_calls; turn incomplete",
        )
    # After a completed assistant, later reasoning/tool_result means the turn is still open.
    for idx, event in enumerate(forward.events):
        if idx <= _idx:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "user":
            break
        if event.get("type") in {"reasoning", "tool_result"}:
            return ManagedFinalResult(
                code="RESULT_NOT_FINAL",
                detail="trailing reasoning/tool_result after assistant; turn incomplete",
            )
        if event.get("type") == "assistant":
            return ManagedFinalResult(
                code="AMBIGUOUS_SESSION",
                detail="multiple assistant finals after bound user; refusing latest selection",
            )

    if cancel_requested:
        return ManagedFinalResult(code="RESULT_NOT_FINAL", detail="CANCEL_REQUESTED blocks automatic FINAL admission")

    item_id = assistant_event.get("id") or assistant_event.get("uuid")
    if not isinstance(item_id, str) or not item_id:
        item_id = f"final-{_idx}"
    turn_id = item_id if item_id.startswith("turn_") else _stable_turn_id(
        sid or "unknown",
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
            "finalItemId": item_id,
            "runtimeId": runtime_id,
            "sessionId": sid,
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
        "sessionId": sid,
        "turnId": turn_id,
        "promptSha256": prompt_sha256,
        "finalItemId": item_id,
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
