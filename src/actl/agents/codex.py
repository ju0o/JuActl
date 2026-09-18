"""Fail-closed Codex Result correlation from the live process-owned rollout."""
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
class CodexResolution:
    session_id: str | None = None
    rollout_path: Path | None = None
    match_method: str = "none"
    confidence: str = "none"
    detail: str = ""
    foreground_pid: int | None = None
    tty: str | None = None


def _same_path(left: str | Path, right: str | Path) -> bool:
    return Path(left).expanduser().resolve(strict=False) == Path(right).expanduser().resolve(strict=False)


def _codex_process(pid: int | None, target: str) -> int | None:
    if pid is not None:
        for process in pane_processes(pid):
            executable = process.args.split(maxsplit=1)[0] if process.args else ""
            if Path(executable).name == "codex":
                return process.pid
        return None
    try:
        pane_pid = int(pane_field(target, "#{pane_pid}"))
    except (TypeError, ValueError, OSError):
        return None
    return _codex_process(pane_pid, target)


def _process_tty(pid: int) -> str | None:
    try:
        tty = Path(f"/proc/{pid}/fd/0").resolve(strict=True)
    except OSError:
        return None
    return str(tty) if str(tty).startswith("/dev/pts/") else None


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


def _open_rollouts(codex_pid: int, sessions_root: Path) -> list[Path]:
    """Read only process FD links under sessions/; never discover by mtime."""
    root = sessions_root.expanduser().resolve(strict=False)
    found: set[Path] = set()
    for path in _open_paths(codex_pid):
        try:
            resolved = path.resolve(strict=False)
            resolved.relative_to(root)
        except (OSError, ValueError):
            continue
        if resolved.suffix == ".jsonl" and resolved.name.startswith("rollout-") and resolved.is_file():
            found.add(resolved)
    return sorted(found)


def _open_thread_locks(codex_pid: int, locks_root: Path) -> list[str]:
    root = locks_root.expanduser().resolve(strict=False)
    found: set[str] = set()
    for path in _open_paths(codex_pid):
        try:
            resolved = path.resolve(strict=False)
            resolved.relative_to(root)
        except (OSError, ValueError):
            continue
        if resolved.suffix == ".lock" and resolved.stem and resolved.stem != ".coordination":
            found.add(resolved.stem)
    return sorted(found)


def _session_meta(path: Path) -> dict[str, Any] | None:
    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if item.get("type") != "session_meta":
                    continue
                payload = item.get("payload")
                return payload if isinstance(payload, dict) else None
    except OSError:
        return None
    return None


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


def _adopt_rollout(root: Path, pid: int, pane_cwd: str) -> tuple[Path | None, str]:
    """Adopt the rollout this process provably owns (written within its lifetime).

    Codex does not keep the rollout file descriptor open between events, so
    process-FD ownership alone is unreliable. Adoption requires exactly one
    rollout whose session_meta cwd equals the pane cwd AND that was written
    within the live process's lifetime — never a global newest-file guess.
    """
    start_ms = _proc_start_ms(pid)
    if start_ms is None:
        return None, "Could not read live Codex process start time"
    sessions_root = root.expanduser() / "sessions"
    candidates: list[Path] = []
    try:
        paths = list(sessions_root.rglob("rollout-*.jsonl"))
    except OSError:
        paths = []
    for path in paths:
        if not path.is_file():
            continue
        meta = _session_meta(path)
        cwd = meta.get("cwd") if isinstance(meta, dict) else None
        if not isinstance(cwd, str) or not _same_path(cwd, pane_cwd):
            continue
        try:
            mtime_ms = int(path.stat().st_mtime * 1000)
        except OSError:
            continue
        if mtime_ms >= start_ms - 1000:
            candidates.append(path)
    if not candidates:
        return None, "No Codex rollout in this pane directory was active during this process"
    if len(candidates) > 1:
        return None, (
            f"Multiple Codex rollouts were active in this pane directory "
            f"({len(candidates)}); cannot adopt safely"
        )
    return candidates[0], ""


def resolve_codex(root: Path, target: str, codex_pid: int | None = None) -> CodexResolution:
    """Bind Codex copy to the selected pane's live process-owned rollout only."""
    pid = _codex_process(codex_pid, target)
    if pid is None:
        return CodexResolution(detail="No live Codex process belongs to the selected target")
    try:
        pane_cwd = pane_field(target, "#{pane_current_path}")
    except Exception as exc:
        return CodexResolution(detail=f"Could not read selected Codex pane: {exc}", foreground_pid=pid)
    home = root.expanduser()
    owned = _open_rollouts(pid, home / "sessions")
    if len(owned) > 1:
        return CodexResolution(
            detail="Active Codex process does not identify exactly one open sessions rollout",
            foreground_pid=pid,
            tty=_process_tty(pid),
        )
    if len(owned) == 1:
        rollout = owned[0]
        method = "codex-process-open-rollout + session_meta + thread-lock"
    else:
        # Codex closes the rollout FD between events; adopt the one this
        # process provably wrote within its lifetime (cwd + active + unique).
        rollout, detail = _adopt_rollout(home, pid, pane_cwd)
        if rollout is None:
            return CodexResolution(
                detail=detail,
                foreground_pid=pid,
                tty=_process_tty(pid),
            )
        method = "codex adoption: cwd + active-within-lifetime"
    meta = _session_meta(rollout)
    if not meta:
        return CodexResolution(detail="Codex rollout is missing session_meta", foreground_pid=pid, tty=_process_tty(pid))
    session_id = meta.get("session_id") or meta.get("id")
    if not isinstance(session_id, str) or not session_id:
        return CodexResolution(detail="Codex rollout session_meta lacks session_id", foreground_pid=pid, tty=_process_tty(pid))
    if session_id not in rollout.name:
        return CodexResolution(detail="Codex rollout filename does not match session_meta session_id", foreground_pid=pid, tty=_process_tty(pid))
    # FD ownership of the single open rollout proves the active conversation
    # even when the pane's reported cwd differs (Codex may be launched from a
    # parent dir or resume an older session). Adoption (weaker signal) still
    # requires an exact cwd match.
    if not owned:
        cwd = meta.get("cwd")
        if not isinstance(cwd, str) or not _same_path(cwd, pane_cwd):
            return CodexResolution(detail="Codex rollout cwd does not match selected pane cwd", foreground_pid=pid, tty=_process_tty(pid))
    locks = _open_thread_locks(pid, home / "thread-writer-locks")
    # Codex 1.x may keep additional thread-writer locks for subagents; the
    # matched session's own lock must still be present to prove active writing.
    if locks and session_id not in locks:
        return CodexResolution(detail="Codex thread lock does not include the matched rollout session", foreground_pid=pid, tty=_process_tty(pid))
    return CodexResolution(
        session_id=session_id,
        rollout_path=rollout,
        match_method=method,
        confidence="exact",
        foreground_pid=pid,
        tty=_process_tty(pid),
    )


def _agent_message_text(item: dict[str, Any]) -> str | None:
    """Accept only completed AgentMessage text; never reasoning/tools/user."""
    if item.get("type") != "AgentMessage":
        return None
    phase = item.get("phase")
    if phase not in {"commentary", "final_answer"}:
        return None
    content = item.get("content")
    if not isinstance(content, list):
        return None
    parts = [
        part["text"].strip()
        for part in content
        if isinstance(part, dict) and part.get("type") in {"Text", "text"} and isinstance(part.get("text"), str) and part["text"].strip()
    ]
    return "\n".join(parts) or None


def extract_codex(resolution: CodexResolution | Path) -> CopyResult | None:
    """Extract completed assistant text from one already-correlated rollout only.

    Legacy Path callers are rejected: newest-file / bare-root extraction is forbidden.
    """
    if isinstance(resolution, Path):
        return None
    if resolution.confidence != "exact" or resolution.rollout_path is None:
        return None
    latest_user = -1
    records: list[tuple[str, str | None]] = []
    try:
        with resolution.rollout_path.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                payload = event.get("payload")
                if event.get("type") != "event_msg" or not isinstance(payload, dict) or payload.get("type") != "item_completed":
                    continue
                item = payload.get("item")
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "UserMessage":
                    latest_user = len(records)
                    records.append(("user", None))
                    continue
                text = _agent_message_text(item)
                if text:
                    records.append(("assistant", text))
    except OSError:
        return None
    visible = [text for kind, text in records[latest_user + 1 :] if kind == "assistant" and text]
    if not visible:
        return None
    return CopyResult("\n".join(visible), f"codex-rollout:{resolution.rollout_path}", "exact")


# --- Managed v1 (Phase 1A) — never uses Direct adoption fallbacks ---

MANAGED_PARSER_VERSION = "codex-managed-final-v1"


@dataclass(frozen=True)
class ManagedCodexResolution:
    session_id: str | None = None
    rollout_path: Path | None = None
    code: str = "OK"
    detail: str = ""
    match_method: str = "none"
    foreground_pid: int | None = None


@dataclass(frozen=True)
class JsonlForwardResult:
    events: list[dict[str, Any]]
    lines: list[str]
    next_byte_offset: int
    pending_incomplete: bool = False
    code: str = "OK"
    detail: str = ""


@dataclass(frozen=True)
class UserTurnBinding:
    session_id: str
    turn_id: str
    thread_id: str | None
    event_index: int
    byte_end: int
    code: str = "OK"
    detail: str = ""


@dataclass(frozen=True)
class ManagedFinalResult:
    code: str
    detail: str = ""
    packet: dict[str, Any] | None = None


class ManagedCodexError(Exception):
    def __init__(self, code: str, detail: str):
        self.code = code
        self.detail = detail
        super().__init__(detail)


def _file_identity(path: Path) -> tuple[str, str]:
    st = path.stat()
    return str(st.st_dev), str(st.st_ino)


def _prefix_sha256(path: Path, byte_offset: int) -> str:
    import hashlib

    with path.open("rb") as fh:
        data = fh.read(byte_offset)
    return hashlib.sha256(data).hexdigest()


def resolve_codex_managed(
    root: Path,
    target: str,
    codex_pid: int | None = None,
    *,
    proven_source: Path | None = None,
) -> ManagedCodexResolution:
    """Managed identity: exact open FD rollout or unique lock + proven source only.

    Never calls `_adopt_rollout`. Zero/multiple/collision → AMBIGUOUS_SESSION.
    """
    pid = _codex_process(codex_pid, target)
    if pid is None:
        return ManagedCodexResolution(code="DOWN", detail="No live Codex process belongs to the selected target")
    home = root.expanduser()
    owned = _open_rollouts(pid, home / "sessions")
    locks = _open_thread_locks(pid, home / "thread-writer-locks")

    rollout: Path | None = None
    method = "none"
    if len(owned) > 1:
        return ManagedCodexResolution(
            code="AMBIGUOUS_SESSION",
            detail="Active Codex process has multiple open sessions rollouts",
            foreground_pid=pid,
        )
    if len(owned) == 1:
        rollout = owned[0]
        method = "managed-process-open-rollout"
    elif proven_source is not None:
        # Already-proven source may continue only with exact unique thread lock.
        if len(locks) != 1:
            return ManagedCodexResolution(
                code="AMBIGUOUS_SESSION",
                detail="Proven source requires exactly one thread-writer lock",
                foreground_pid=pid,
            )
        proven = proven_source.expanduser().resolve(strict=False)
        if not proven.is_file():
            return ManagedCodexResolution(code="DOWN", detail="Proven source path is missing", foreground_pid=pid)
        meta = _session_meta(proven)
        session_id = (meta or {}).get("session_id") or (meta or {}).get("id") if meta else None
        if not isinstance(session_id, str) or session_id != locks[0]:
            return ManagedCodexResolution(
                code="AMBIGUOUS_SESSION",
                detail="Proven source session does not match unique thread lock",
                foreground_pid=pid,
            )
        if session_id not in proven.name:
            return ManagedCodexResolution(
                code="AMBIGUOUS_SESSION",
                detail="Proven source filename does not match session_id",
                foreground_pid=pid,
            )
        rollout = proven
        method = "managed-proven-source+unique-thread-lock"
    else:
        return ManagedCodexResolution(
            code="AMBIGUOUS_SESSION",
            detail="No exact open rollout FD and no proven source; Managed refuses adoption",
            foreground_pid=pid,
        )

    meta = _session_meta(rollout)
    if not meta:
        return ManagedCodexResolution(code="AMBIGUOUS_SESSION", detail="Codex rollout is missing session_meta", foreground_pid=pid)
    session_id = meta.get("session_id") or meta.get("id")
    if not isinstance(session_id, str) or not session_id:
        return ManagedCodexResolution(code="AMBIGUOUS_SESSION", detail="session_meta lacks session_id", foreground_pid=pid)
    if session_id not in rollout.name:
        return ManagedCodexResolution(
            code="AMBIGUOUS_SESSION",
            detail="rollout filename does not match session_meta session_id",
            foreground_pid=pid,
        )
    if locks and session_id not in locks:
        return ManagedCodexResolution(
            code="AMBIGUOUS_SESSION",
            detail="thread lock set does not include matched session",
            foreground_pid=pid,
        )
    return ManagedCodexResolution(
        session_id=session_id,
        rollout_path=rollout,
        code="OK",
        match_method=method,
        foreground_pid=pid,
    )


def read_jsonl_forward(path: Path, cursor: dict[str, Any]) -> JsonlForwardResult:
    """Read complete JSONL lines after cursor; never skip malformed completed lines."""
    path = Path(path)
    try:
        st = path.stat()
    except OSError as exc:
        return JsonlForwardResult([], [], 0, code="SOURCE_INTEGRITY", detail=f"stat failed: {exc}")

    byte_offset = int(cursor.get("byteOffset") or cursor.get("byte_offset") or 0)
    expected_dev = cursor.get("dev")
    expected_inode = cursor.get("inode")
    expected_prefix = cursor.get("prefixSha256") or cursor.get("prefix_sha256")
    if expected_dev is not None and str(st.st_dev) != str(expected_dev):
        return JsonlForwardResult([], [], byte_offset, code="SOURCE_INTEGRITY", detail="dev mismatch")
    if expected_inode is not None and str(st.st_ino) != str(expected_inode):
        return JsonlForwardResult([], [], byte_offset, code="SOURCE_INTEGRITY", detail="inode mismatch")
    if st.st_size < byte_offset:
        return JsonlForwardResult([], [], byte_offset, code="SOURCE_INTEGRITY", detail="source shrunk before cursor")
    if expected_prefix is not None:
        try:
            actual_prefix = _prefix_sha256(path, byte_offset)
        except OSError as exc:
            return JsonlForwardResult([], [], byte_offset, code="SOURCE_INTEGRITY", detail=str(exc))
        if actual_prefix != expected_prefix:
            return JsonlForwardResult([], [], byte_offset, code="SOURCE_INTEGRITY", detail="prefix hash mismatch")

    try:
        with path.open("rb") as fh:
            fh.seek(byte_offset)
            raw = fh.read()
    except OSError as exc:
        return JsonlForwardResult([], [], byte_offset, code="SOURCE_INTEGRITY", detail=str(exc))

    events: list[dict[str, Any]] = []
    lines: list[str] = []
    consumed = 0
    pending = False
    while consumed < len(raw):
        nl = raw.find(b"\n", consumed)
        if nl < 0:
            pending = consumed < len(raw)
            break
        chunk = raw[consumed:nl]
        consumed = nl + 1
        try:
            text = chunk.decode("utf-8")
        except UnicodeDecodeError:
            return JsonlForwardResult(
                events,
                lines,
                byte_offset + consumed,
                code="SOURCE_INTEGRITY",
                detail="malformed UTF-8 on completed line",
            )
        try:
            event = json.loads(text)
        except json.JSONDecodeError:
            return JsonlForwardResult(
                events,
                lines,
                byte_offset + consumed,
                code="SOURCE_INTEGRITY",
                detail="malformed JSON on completed line",
            )
        if not isinstance(event, dict):
            return JsonlForwardResult(
                events,
                lines,
                byte_offset + consumed,
                code="SOURCE_INTEGRITY",
                detail="completed line is not a JSON object",
            )
        lines.append(text)
        events.append(event)
    return JsonlForwardResult(
        events=events,
        lines=lines,
        next_byte_offset=byte_offset + consumed,
        pending_incomplete=pending,
        code="OK",
    )


def _user_message_text(item: dict[str, Any]) -> str:
    content = item.get("content")
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        text = part.get("text")
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(parts)


def bind_user_turn(
    cursor: dict[str, Any],
    wire_prompt: str,
    events: list[dict[str, Any]],
    *,
    session_id: str | None = None,
) -> UserTurnBinding:
    """Bind exactly one post-cursor UserMessage that contains wirePrompt."""
    matches: list[tuple[int, dict[str, Any], dict[str, Any]]] = []
    for idx, event in enumerate(events):
        payload = event.get("payload")
        if event.get("type") != "event_msg" or not isinstance(payload, dict):
            continue
        if payload.get("type") != "item_completed":
            continue
        item = payload.get("item")
        if not isinstance(item, dict) or item.get("type") != "UserMessage":
            continue
        text = _user_message_text(item)
        if wire_prompt in text:
            matches.append((idx, event, payload))
    if not matches:
        return UserTurnBinding("", "", None, -1, int(cursor.get("byteOffset") or 0), code="RESULT_NOT_FINAL", detail="no matching UserMessage after cursor")
    if len(matches) > 1:
        return UserTurnBinding("", "", None, -1, int(cursor.get("byteOffset") or 0), code="AMBIGUOUS_SESSION", detail="multiple UserMessage turns match wirePrompt")
    idx, event, payload = matches[0]
    item = payload.get("item") if isinstance(payload.get("item"), dict) else {}
    turn_id = payload.get("turn_id") or item.get("turn_id") or event.get("turn_id")
    thread_id = payload.get("thread_id") or item.get("thread_id") or event.get("thread_id")
    sid = session_id or payload.get("session_id") or item.get("session_id") or ""
    if not sid:
        # Codex 0.153.4+: item_completed UserMessage has turn_id/thread_id but no
        # session_id; take it from the same rollout's session_meta event.
        for ev in events:
            if ev.get("type") != "session_meta":
                continue
            meta = ev.get("payload") if isinstance(ev.get("payload"), dict) else {}
            cand = meta.get("session_id") or meta.get("id")
            if isinstance(cand, str) and cand:
                sid = cand
                break
    if not isinstance(turn_id, str) or not turn_id:
        return UserTurnBinding(str(sid), "", thread_id if isinstance(thread_id, str) else None, idx, 0, code="RESULT_NOT_FINAL", detail="matched UserMessage lacks turn_id")
    if not isinstance(sid, str) or not sid:
        return UserTurnBinding("", turn_id, thread_id if isinstance(thread_id, str) else None, idx, 0, code="RESULT_NOT_FINAL", detail="matched UserMessage lacks session_id")
    return UserTurnBinding(
        session_id=sid,
        turn_id=turn_id,
        thread_id=thread_id if isinstance(thread_id, str) else None,
        event_index=idx,
        byte_end=int(cursor.get("byteOffset") or 0),
        code="OK",
    )


def _final_answer_text(item: dict[str, Any]) -> str | None:
    if item.get("type") != "AgentMessage" or item.get("phase") != "final_answer":
        return None
    content = item.get("content")
    if not isinstance(content, list):
        return None
    parts = [
        part["text"]
        for part in content
        if isinstance(part, dict) and part.get("type") in {"Text", "text"} and isinstance(part.get("text"), str)
    ]
    text = "".join(parts)
    return text if text else None


def _canonical_json_bytes(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _sha256_hex(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


def collect_codex_final(
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
    """Admit FINAL only when Target Architecture §7.4 predicates all hold."""
    import hashlib
    from datetime import datetime, timezone

    forward = read_jsonl_forward(path, cursor)
    if forward.code != "OK":
        return ManagedFinalResult(code="AMBIGUOUS_SESSION" if forward.code == "SOURCE_INTEGRITY" else forward.code, detail=forward.detail)
    if forward.pending_incomplete and not forward.events:
        return ManagedFinalResult(code="RESULT_NOT_FINAL", detail="incomplete JSONL tail")

    binding = bind_user_turn(cursor, wire_prompt, forward.events, session_id=session_id)
    if binding.code != "OK":
        return ManagedFinalResult(code=binding.code, detail=binding.detail)

    # Scan only after the bound user turn.
    final_candidates: list[tuple[int, str, str, dict[str, Any]]] = []
    task_complete: dict[str, Any] | None = None
    for idx, event in enumerate(forward.events):
        if idx <= binding.event_index:
            continue
        payload = event.get("payload")
        if event.get("type") != "event_msg" or not isinstance(payload, dict):
            continue
        item = payload.get("item") if isinstance(payload.get("item"), dict) else {}
        turn_id = payload.get("turn_id") or item.get("turn_id")
        if turn_id != binding.turn_id:
            continue
        if payload.get("type") == "item_completed":
            if not isinstance(item, dict):
                continue
            # Commentary must never be promoted.
            if item.get("type") == "AgentMessage" and item.get("phase") == "commentary":
                continue
            text = _final_answer_text(item)
            if text is None:
                continue
            item_id = str(item.get("id") or payload.get("item_id") or f"final-{idx}")
            final_candidates.append((idx, text, item_id, payload))
        elif payload.get("type") == "task_complete":
            task_complete = payload

    if not final_candidates:
        return ManagedFinalResult(code="RESULT_NOT_FINAL", detail="no final_answer AgentMessage for bound turn")
    if len(final_candidates) != 1:
        return ManagedFinalResult(code="AMBIGUOUS_SESSION", detail="multiple final_answer candidates; refusing latest selection")
    if task_complete is None:
        return ManagedFinalResult(code="RESULT_NOT_FINAL", detail="missing task_complete for bound turn")

    _idx, raw_text, final_item_id, _payload = final_candidates[0]
    # task_complete must appear after the final item.
    tc_index = None
    for idx, event in enumerate(forward.events):
        payload = event.get("payload")
        if event.get("type") == "event_msg" and isinstance(payload, dict) and payload.get("type") == "task_complete":
            if payload.get("turn_id") == binding.turn_id:
                tc_index = idx
                task_complete = payload
    if tc_index is None or tc_index <= _idx:
        return ManagedFinalResult(code="RESULT_NOT_FINAL", detail="task_complete must follow final_answer")

    last_msg = task_complete.get("last_agent_message")
    if last_msg != raw_text:
        return ManagedFinalResult(code="RESULT_NOT_FINAL", detail="task_complete.last_agent_message mismatch")

    if cancel_requested:
        return ManagedFinalResult(code="RESULT_NOT_FINAL", detail="CANCEL_REQUESTED blocks automatic FINAL admission")

    try:
        st = path.stat()
        byte_start = int(cursor.get("byteOffset") or 0)
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
            "turnId": binding.turn_id,
        })
    )
    observed_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    packet = {
        "contractVersion": 1,
        "resultId": result_id,
        "commandId": command_id,
        "runtimeId": runtime_id,
        "sessionId": binding.session_id,
        "turnId": binding.turn_id,
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
