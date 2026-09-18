"""Session-aware OpenCode Result correlation.

Binding chain (all deterministic, no heuristics):

  stored session_id (ACTL mapping)
  -> live ``opencode --session <ID>`` process cmdline (kernel-provided, read-only)
  -> session row in the shared ``opencode.db`` (explicit ``WHERE id = ?``)
  -> message/part rows of that session only (latest completed assistant text)

A TUI launched as bare ``opencode`` / ``opencode --auto`` (yolo mode) carries
no ``--session`` in its cmdline, so actl auto-maps to the session it most
likely owns instead: among rows whose directory equals the pane cwd AND
whose ``time_updated`` falls within the live process's lifetime, the most
recently updated one is adopted automatically (only one conversation can be
on screen in a pane at a time). Zero candidates in that cwd still fails
closed — there is nothing to adopt. This is pane-bounded (never a global
"newest session across all panes" fallback). Session IDs are obtained
officially via the local ACP server
(``opencode acp`` + ``session/new``), which creates an empty session without
sending any prompt or model call.
"""
from __future__ import annotations

import json
import os
import re
import select
import sqlite3
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from actl.core.models import CopyResult
from actl.core.tmux import TmuxError, pane_field
from actl.core.validation import pane_processes


SESSION_ID_RE = re.compile(r"^ses_[A-Za-z0-9]{8,}$")

#: How ``/copy`` must obtain the expected session: the mapping entry, never a query.
MAPPING_SESSION_KEY = "session_id"


class OpenCodeSessionError(RuntimeError):
    """Raised when an official session operation fails without guessing."""


@dataclass(frozen=True)
class OpenCodeResolution:
    session_id: str | None = None
    storage_path: Path | None = None
    match_method: str = "none"
    confidence: str = "none"
    detail: str = (
        "OpenCode has no read-only live-session binding: process/FD/runtime state "
        "does not uniquely identify the active conversation without newest-session fallback"
    )
    live_session_id: str | None = None
    foreground_pid: int | None = None
    tty: str | None = None


def stored_session_id(config: dict | None) -> str | None:
    """Return the mapped OpenCode session ID, or None when unbound."""
    if not isinstance(config, dict):
        return None
    entry = (config.get("agents") or {}).get("opencode")
    if not isinstance(entry, dict):
        return None
    value = entry.get(MAPPING_SESSION_KEY)
    return value if isinstance(value, str) and value else None


def valid_session_id(value: str | None) -> bool:
    return isinstance(value, str) and SESSION_ID_RE.match(value) is not None


def _proc_args(pid: int) -> list[str] | None:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return None
    return [part for part in raw.decode("utf-8", "ignore").split("\0") if part]


def live_cmdline_session(pid: int) -> str | None:
    """Return the ``--session``/``-s`` value from a live process cmdline.

    Returns None when the process was not launched with an explicit session,
    which is exactly the arbitrary-TUI case that must fail closed.
    """
    args = _proc_args(pid)
    if not args:
        return None
    for index, token in enumerate(args):
        if token == "--session" or token == "-s":
            if index + 1 < len(args) and valid_session_id(args[index + 1]):
                return args[index + 1]
            return None
        if token.startswith("--session="):
            value = token.partition("=")[2]
            return value if valid_session_id(value) else None
    return None


def _opencode_pids(pane_pid: int) -> list[int]:
    found = []
    for process in pane_processes(pane_pid):
        executable = process.args.split(maxsplit=1)[0] if process.args else ""
        if Path(executable).name == "opencode":
            found.append(process.pid)
    return found


def _process_tty(pid: int) -> str | None:
    try:
        tty = Path(f"/proc/{pid}/fd/0").resolve(strict=True)
    except OSError:
        return None
    return str(tty) if str(tty).startswith("/dev/pts/") else None


def _same_path(left: str | Path, right: str | Path) -> bool:
    return Path(left).expanduser().resolve(strict=False) == Path(right).expanduser().resolve(strict=False)


def _db_path(data_root: Path) -> Path:
    return data_root.expanduser().resolve(strict=False) / "opencode.db"


def session_row(data_root: Path, session_id: str) -> dict[str, Any] | None:
    """Read one explicit session row; never newest/global queries."""
    try:
        con = sqlite3.connect(f"file:{_db_path(data_root)}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        row = con.execute(
            "SELECT id, directory, title FROM session WHERE id = ?", (session_id,)
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        con.close()
    if not row:
        return None
    return {"id": row[0], "directory": row[1], "title": row[2]}


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


def _db_live_session(data_root: Path, pid: int, pane_cwd: str) -> tuple[str | None, str]:
    """Auto-map a bare TUI to the session it most likely owns, no rebind needed.

    Candidates are rows in ``pane_cwd`` created or updated within the live
    process's lifetime (small skew allowance) — this still excludes stale
    sessions left over from a previous process in the same directory. Among
    those candidates, the most recently updated one is adopted automatically:
    only one conversation can be on screen in a given pane at a time, and
    "most recently touched in this pane's directory" is that conversation.
    Zero candidates still fails closed — there is nothing to adopt.
    """
    start_ms = _proc_start_ms(pid)
    if start_ms is None:
        return None, "Could not read live opencode process start time"
    try:
        con = sqlite3.connect(f"file:{_db_path(data_root)}?mode=ro", uri=True)
    except sqlite3.Error:
        return None, "opencode.db is not readable"
    try:
        rows = con.execute(
            "SELECT id, directory, time_updated FROM session WHERE time_updated >= ?",
            (start_ms - 1000,),
        ).fetchall()
    except sqlite3.Error:
        return None, "opencode.db session query failed"
    finally:
        con.close()
    candidates = [
        (row[0], row[2])
        for row in rows
        if isinstance(row[1], str) and isinstance(row[2], int) and _same_path(row[1], pane_cwd)
    ]
    if not candidates:
        return None, (
            "No opencode session in this pane directory was active during this "
            "process; cannot adopt a bare TUI safely. Run: actl bind opencode"
        )
    # ponytail: newest-time_updated wins on ambiguity (auto-map, per user
    # request, over the old fail-closed-on-2+ behavior). Ceiling: a session
    # touched in the background (e.g. async title gen) after the one actually
    # on screen could win the tie-break; add TTY/foreground correlation if
    # that misfires in practice.
    session_id, _ = max(candidates, key=lambda c: c[1])
    return session_id, ""


def resolve_opencode(
    data_root: Path,
    target: str,
    agent_pid: int | None = None,
    expected_session_id: str | None = None,
) -> OpenCodeResolution:
    """Bind /copy to the stored session only when the live TUI proves it.

    A TUI launched without ``--session`` (bare ``opencode`` / ``--auto``) is
    adopted from deterministic DB evidence (pane cwd + active within process
    lifetime + exactly one candidate); otherwise the stored session must match
    the live process cmdline.
    """

    def _unbound_or(alt: str) -> OpenCodeResolution:
        if not valid_session_id(expected_session_id):
            return OpenCodeResolution(
                detail="OpenCode /copy needs a one-time session bind. Run: actl bind opencode"
            )
        return OpenCodeResolution(detail=alt)

    pid = agent_pid
    if pid is None:
        try:
            pid = int(pane_field(target, "#{pane_pid}"))
        except (TypeError, ValueError, OSError, TmuxError):
            return _unbound_or("Could not read selected OpenCode pane PID")
        matches = _opencode_pids(pid)
        if len(matches) != 1:
            return _unbound_or(
                "Selected pane does not contain exactly one live opencode process"
            )
        pid = matches[0]

    live = live_cmdline_session(pid)

    if live is None:
        # Bare TUI (opencode / opencode --auto): adopt the session this
        # process provably owns, read-only. Works bound or unbound.
        try:
            pane_cwd = pane_field(target, "#{pane_current_path}")
        except Exception as exc:
            return OpenCodeResolution(
                detail=f"Could not read selected OpenCode pane: {exc}",
                foreground_pid=pid,
                tty=_process_tty(pid),
            )
        adopted, detail = _db_live_session(data_root, pid, pane_cwd)
        if adopted is None:
            return OpenCodeResolution(
                detail=detail,
                foreground_pid=pid,
                tty=_process_tty(pid),
            )
        row = session_row(data_root, adopted)
        if row is None or not _same_path(row["directory"], pane_cwd):
            return OpenCodeResolution(
                detail="Adopted session lost its opencode.db row",
                foreground_pid=pid,
                tty=_process_tty(pid),
            )
        return OpenCodeResolution(
            session_id=adopted,
            storage_path=_db_path(data_root),
            match_method="db-active-session adoption (bare TUI)",
            confidence="exact",
            detail="",
            live_session_id=adopted,
            foreground_pid=pid,
            tty=_process_tty(pid),
        )

    if not valid_session_id(expected_session_id):
        return OpenCodeResolution(
            detail="OpenCode /copy needs a one-time session bind. Run: actl bind opencode",
            live_session_id=live,
            foreground_pid=pid,
            tty=_process_tty(pid),
        )
    if live != expected_session_id:
        return OpenCodeResolution(
            detail=(
                "Live opencode session does not match the bound session; "
                "refusing to copy from the wrong conversation"
            ),
            live_session_id=live,
            foreground_pid=pid,
            tty=_process_tty(pid),
        )
    try:
        pane_cwd = pane_field(target, "#{pane_current_path}")
    except Exception as exc:
        return OpenCodeResolution(
            detail=f"Could not read selected OpenCode pane: {exc}",
            session_id=expected_session_id,
            live_session_id=live,
            foreground_pid=pid,
            tty=_process_tty(pid),
        )
    row = session_row(data_root, expected_session_id)
    if row is None:
        return OpenCodeResolution(
            detail="Bound OpenCode session_id has no row in opencode.db",
            session_id=expected_session_id,
            live_session_id=live,
            foreground_pid=pid,
            tty=_process_tty(pid),
        )
    if not _same_path(row["directory"], pane_cwd):
        return OpenCodeResolution(
            detail="Bound session directory does not match the selected pane cwd",
            session_id=expected_session_id,
            live_session_id=live,
            foreground_pid=pid,
            tty=_process_tty(pid),
        )
    return OpenCodeResolution(
        session_id=expected_session_id,
        storage_path=_db_path(data_root),
        match_method="stored-session + proc-cmdline-session + db-row + pane-cwd",
        confidence="exact",
        detail="",
        live_session_id=live,
        foreground_pid=pid,
        tty=_process_tty(pid),
    )


def _message_parts(con: sqlite3.Connection, message_id: str) -> list[dict[str, Any]]:
    try:
        rows = con.execute(
            "SELECT data FROM part WHERE message_id = ? ORDER BY time_created, id",
            (message_id,),
        ).fetchall()
    except sqlite3.Error:
        return []
    parts = []
    for (raw,) in rows:
        try:
            item = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(item, dict):
            parts.append(item)
    return parts


def _completed_assistant_text(parts: list[dict[str, Any]]) -> str | None:
    texts = [
        part["text"].strip()
        for part in parts
        if part.get("type") == "text"
        and isinstance(part.get("text"), str)
        and part["text"].strip()
    ]
    return "\n".join(texts) or None


def extract_opencode(resolution: OpenCodeResolution | Path) -> CopyResult | None:
    """Extract the latest completed assistant text for one bound session only.

    Legacy Path callers are rejected: newest-file / bare-root extraction is forbidden.
    Only ``type == "text"`` parts of completed assistant messages qualify; user,
    tool, reasoning, patch, and step parts are never returned, and unfinished
    (streaming) assistant messages without a finish marker are skipped.
    """
    if isinstance(resolution, Path):
        return None
    if (
        resolution.confidence != "exact"
        or resolution.session_id is None
        or resolution.storage_path is None
    ):
        return None
    try:
        con = sqlite3.connect(f"file:{resolution.storage_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        try:
            rows = con.execute(
                "SELECT id, data FROM message WHERE session_id = ? "
                "ORDER BY time_created DESC, id DESC",
                (resolution.session_id,),
            ).fetchall()
        except sqlite3.Error:
            return None
        for message_id, raw in rows:
            try:
                data = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(data, dict) or data.get("role") != "assistant":
                continue
            if data.get("finish") is None:
                continue
            text = _completed_assistant_text(_message_parts(con, message_id))
            if text:
                return CopyResult(
                    text,
                    f"opencode-session:{resolution.session_id}",
                    "exact",
                )
    finally:
        con.close()
    return None


def _acp_readline(proc: subprocess.Popen, deadline: float) -> str:
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise OpenCodeSessionError("Timed out waiting for the local opencode ACP server")
        ready, _, _ = select.select([proc.stdout], [], [], remaining)
        if not ready:
            raise OpenCodeSessionError("Timed out waiting for the local opencode ACP server")
        line = proc.stdout.readline()
        if not line:
            raise OpenCodeSessionError("Local opencode ACP server exited before answering")
        if line.strip():
            return line


def create_session(cwd: str | Path, *, timeout: float = 30.0) -> str:
    """Create an empty session via the official local ACP server.

    Sends ``initialize`` + ``session/new`` over stdio only. No prompt, no model
    call, no network: the server only inserts one empty session row and returns
    its ID, which the user then passes to ``opencode --session <ID>``.
    """
    try:
        proc = subprocess.Popen(
            ["opencode", "acp", "--cwd", str(cwd)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
    except FileNotFoundError as exc:
        raise OpenCodeSessionError("opencode binary not found in PATH") from exc
    try:
        deadline = time.monotonic() + timeout

        def exchange(payload: dict) -> dict:
            assert proc.stdin is not None
            proc.stdin.write(json.dumps(payload) + "\n")
            proc.stdin.flush()
            try:
                reply = json.loads(_acp_readline(proc, deadline))
            except json.JSONDecodeError as exc:
                raise OpenCodeSessionError("Unreadable reply from the local opencode ACP server") from exc
            if not isinstance(reply, dict) or reply.get("id") != payload.get("id"):
                raise OpenCodeSessionError("Mismatched reply from the local opencode ACP server")
            if reply.get("error"):
                raise OpenCodeSessionError(f"opencode ACP error: {reply['error']}")
            return reply

        init = exchange({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": 1, "clientCapabilities": {}}})
        if not isinstance(init.get("result"), dict):
            raise OpenCodeSessionError("opencode ACP initialize failed")
        created = exchange({"jsonrpc": "2.0", "id": 2, "method": "session/new", "params": {"cwd": str(cwd), "mcpServers": []}})
        result = created.get("result")
        session_id = result.get("sessionId") if isinstance(result, dict) else None
        if not valid_session_id(session_id):
            raise OpenCodeSessionError("opencode ACP did not return a valid sessionId")
        return session_id
    finally:
        try:
            proc.terminate()
        except OSError:
            pass
