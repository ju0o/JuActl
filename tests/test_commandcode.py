"""CommandCode /copy correlation and extraction tests."""
import json
from pathlib import Path

from actl.agents import commandcode as cc
from actl.agents.commandcode import extract_commandcode, resolve_commandcode
from actl.core.validation import ProcessInfo

CWD = "/home/user/Projects/MyApp"


def _session(projects_root, slug, session_id, cwd, records, mtime_ms=2_000_000_000_000):
    directory = projects_root / slug
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{session_id}.jsonl"
    header = {"type": "session", "version": 3, "id": session_id, "timestamp": "2026-09-08T00:00:00.000Z", "cwd": cwd}
    rows = [header, *records]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    import os
    os.utime(path, (mtime_ms / 1000, mtime_ms / 1000))
    return path


def _msg(role, parts):
    return {"type": "message", "id": "m", "parentId": None, "timestamp": "2026-09-08T00:00:00.000Z", "message": {"role": role, "content": parts}}


def _text(role, text):
    return _msg(role, [{"type": "text", "text": text}])


def _assistant_streaming(text):
    return _msg("assistant", [{"type": "thinking", "thinking": "NO"}, {"type": "text", "text": text}])


def test_commandcode_adopts_unique_active_session_in_cwd(monkeypatch, tmp_path):
    start = 1_000_000_000_000
    active = _session(tmp_path, "my-app", "s1", CWD, [
        _text("user", "prompt"),
        _assistant_streaming("CC_OK"),
    ], mtime_ms=start + 5_000)
    # Unrelated session in another project must never win.
    _session(tmp_path, "other", "s2", "/elsewhere", [_text("user", "x"), _text("assistant", "WRONG")], mtime_ms=start + 9_000)

    monkeypatch.setattr(cc, "_commandcode_process", lambda *_: 66)
    monkeypatch.setattr(cc, "_open_session_files", lambda *_: [])
    monkeypatch.setattr(cc, "_proc_start_ms", lambda pid: start)
    monkeypatch.setattr(cc, "pane_field", lambda *_: CWD)
    resolution = resolve_commandcode(tmp_path, "%6", 66)
    assert resolution.confidence == "exact"
    assert resolution.session_path == active
    assert extract_commandcode(resolution).text == "CC_OK"


def test_commandcode_fd_owned_session_is_exact(monkeypatch, tmp_path):
    start = 1_000_000_000_000
    active = _session(tmp_path, "my-app", "s1", CWD, [
        _text("user", "prompt"),
        _assistant_streaming("FD_OK"),
    ], mtime_ms=start - 99_000)  # stale mtime, but FD ownership wins
    monkeypatch.setattr(cc, "_commandcode_process", lambda *_: 66)
    monkeypatch.setattr(cc, "_open_session_files", lambda *_: [active])
    monkeypatch.setattr(cc, "pane_field", lambda *_: CWD)
    resolution = resolve_commandcode(tmp_path, "%6", 66)
    assert resolution.confidence == "exact"
    assert extract_commandcode(resolution).text == "FD_OK"


def test_commandcode_stale_session_not_adopted(monkeypatch, tmp_path):
    start = 1_000_000_000_000
    _session(tmp_path, "my-app", "s1", CWD, [_text("user", "x"), _text("assistant", "STALE_TRAP")], mtime_ms=start - 60_000)
    monkeypatch.setattr(cc, "_commandcode_process", lambda *_: 66)
    monkeypatch.setattr(cc, "_open_session_files", lambda *_: [])
    monkeypatch.setattr(cc, "_proc_start_ms", lambda pid: start)
    monkeypatch.setattr(cc, "pane_field", lambda *_: CWD)
    assert resolve_commandcode(tmp_path, "%6", 66).confidence == "none"


def test_commandcode_multiple_active_fails_closed(monkeypatch, tmp_path):
    start = 1_000_000_000_000
    _session(tmp_path, "my-app", "s1", CWD, [_text("user", "x"), _text("assistant", "ONE")], mtime_ms=start + 5_000)
    _session(tmp_path, "my-app", "s2", CWD, [_text("user", "x"), _text("assistant", "TWO")], mtime_ms=start + 4_000)
    monkeypatch.setattr(cc, "_commandcode_process", lambda *_: 66)
    monkeypatch.setattr(cc, "_open_session_files", lambda *_: [])
    monkeypatch.setattr(cc, "_proc_start_ms", lambda pid: start)
    monkeypatch.setattr(cc, "pane_field", lambda *_: CWD)
    resolution = resolve_commandcode(tmp_path, "%6", 66)
    assert resolution.confidence == "none"
    assert "Multiple" in resolution.detail


def test_commandcode_cwd_mismatch_not_adopted(monkeypatch, tmp_path):
    start = 1_000_000_000_000
    _session(tmp_path, "other", "s1", "/elsewhere", [_text("user", "x"), _text("assistant", "WRONG")], mtime_ms=start + 5_000)
    monkeypatch.setattr(cc, "_commandcode_process", lambda *_: 66)
    monkeypatch.setattr(cc, "_open_session_files", lambda *_: [])
    monkeypatch.setattr(cc, "_proc_start_ms", lambda pid: start)
    monkeypatch.setattr(cc, "pane_field", lambda *_: CWD)
    assert resolve_commandcode(tmp_path, "%6", 66).confidence == "none"


def test_commandcode_extract_returns_only_last_response_after_prompt(monkeypatch, tmp_path):
    path = _session(tmp_path, "my-app", "s1", CWD, [
        _text("user", "first"),
        _text("assistant", "OLD"),
        _msg("user", [{"type": "tool_result", "tool_use_id": "x", "content": []}]),
        _assistant_streaming("NEW"),
    ])
    resolution = cc.CommandCodeResolution(session_id="s1", session_path=path, match_method="t", confidence="exact")
    assert extract_commandcode(resolution).text == "NEW"


def test_commandcode_streaming_tail_not_copied(monkeypatch, tmp_path):
    # Last record is a user/tool record -> turn still in progress -> no copy.
    path = _session(tmp_path, "my-app", "s1", CWD, [
        _text("user", "prompt"),
        _assistant_streaming("PARTIAL"),
        _msg("user", [{"type": "tool_result", "tool_use_id": "x", "content": []}]),
    ])
    resolution = cc.CommandCodeResolution(session_id="s1", session_path=path, match_method="t", confidence="exact")
    assert extract_commandcode(resolution) is None


def test_commandcode_extract_rejects_unresolved(tmp_path):
    assert extract_commandcode(cc.CommandCodeResolution()) is None


def test_commandcode_process_detection_matches_cmd_binary(monkeypatch):
    from actl.core.validation import _is_commandcode, ProcessInfo

    assert _is_commandcode(ProcessInfo(1, 1, "/usr/local/bin/cmd --cwd /x"))
    assert _is_commandcode(ProcessInfo(2, 1, "/usr/local/bin/commandcode"))
    assert _is_commandcode(ProcessInfo(3, 1, "node /x/commandcode.js"))
    assert not _is_commandcode(ProcessInfo(4, 1, "/usr/bin/bash"))


def test_commandcode_stale_tmux_socket_fails_closed(monkeypatch):
    monkeypatch.setattr(cc, "pane_field", lambda *_: (_ for _ in ()).throw(RuntimeError("stale tmux socket")))

    assert cc._commandcode_process(None, "%3") is None
