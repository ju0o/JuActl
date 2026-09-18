import json

from actl.agents import claude
from actl.agents.claude import _active_session_id, _project_dir, extract_claude, resolve_claude


def _record(role, content, stop_reason=None, *, session_id=None, cwd=None, sidechain=False):
    row = {"type": "assistant" if role == "assistant" else "user", "message": {"role": role, "content": content}}
    if stop_reason is not None:
        row["message"]["stop_reason"] = stop_reason
    if session_id is not None:
        row["sessionId"] = session_id
    if cwd is not None:
        row["cwd"] = str(cwd)
    if sidechain:
        row["isSidechain"] = True
    return row


def _write_session(root, pane_path, session_id, rows, *, name=None):
    project = _project_dir(root, str(pane_path))
    project.mkdir(parents=True, exist_ok=True)
    path = project / (name or f"{session_id}.jsonl")
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    return path


def _rows(pane, session_id, text, stop_reason="end_turn"):
    return [_record("assistant", [{"type": "text", "text": text}], stop_reason, session_id=session_id, cwd=pane)]


def test_claude_exact_active_session_id_selects_exact_transcript(monkeypatch, tmp_path):
    root = tmp_path / ".claude-pro"; pane = tmp_path / "Agent-Relay"; pane.mkdir()
    _write_session(root, pane, "old", _rows(pane, "old", "OLD"))
    _write_session(root, pane, "wanted", _rows(pane, "wanted", "WANTED"))
    monkeypatch.setattr(claude, "_active_session_binding", lambda *_: ("wanted", 12, "/dev/pts/1", ""))
    assert extract_claude(root, str(pane), 12).text == "WANTED"


def test_claude_active_session_requires_the_pane_tty(monkeypatch, tmp_path):
    root = tmp_path / ".claude-pro"; pane = tmp_path / "Agent-Relay"; pane.mkdir()
    sessions = root / "sessions"; sessions.mkdir(parents=True)
    (sessions / "20.json").write_text(json.dumps({"pid": 20, "sessionId": "foreground", "cwd": str(pane)}))
    (sessions / "21.json").write_text(json.dumps({"pid": 21, "sessionId": "nested", "cwd": str(pane)}))
    monkeypatch.setattr(claude, "_pid_in_pane_tree", lambda *_: True)
    monkeypatch.setattr(claude, "_process_looks_like_claude", lambda *_: True)
    monkeypatch.setattr(claude, "_process_profile_matches", lambda *_: True)
    monkeypatch.setattr(claude, "_process_tty", lambda pid: "/dev/pts/3" if pid in {12, 20} else "/dev/pts/4")
    assert _active_session_id(root, str(pane), 12) == ("foreground", "")


def test_claude_same_project_multiple_sessions_never_choose_wrong_newest(monkeypatch, tmp_path):
    root = tmp_path / ".claude-pro"; pane = tmp_path / "Agent-Relay"; pane.mkdir()
    old = _write_session(root, pane, "old", _rows(pane, "old", "OLD"))
    newer = _write_session(root, pane, "new", _rows(pane, "new", "WRONG_NEWER"))
    newer.touch()
    monkeypatch.setattr(claude, "_active_session_binding", lambda *_: ("old", 12, "/dev/pts/1", ""))
    assert extract_claude(root, str(pane), 12).text == "OLD"


def test_claude_newer_unrelated_project_is_ignored(monkeypatch, tmp_path):
    root = tmp_path / ".claude-pro"; relay = tmp_path / "Agent-Relay"; other = tmp_path / "Other"
    relay.mkdir(); other.mkdir()
    _write_session(root, relay, "wanted", _rows(relay, "wanted", "RELAY"))
    _write_session(root, other, "newer", _rows(other, "newer", "OTHER")).touch()
    monkeypatch.setattr(claude, "_active_session_binding", lambda *_: ("wanted", 12, "/dev/pts/1", ""))
    assert extract_claude(root, str(relay), 12).text == "RELAY"


def test_claude_pro_cannot_read_team_profile(monkeypatch, tmp_path):
    pro = tmp_path / ".claude-pro"; team = tmp_path / ".claude-team"; pane = tmp_path / "Agent-Relay"; pane.mkdir()
    _write_session(team, pane, "shared", _rows(pane, "shared", "TEAM"))
    monkeypatch.setattr(claude, "_active_session_binding", lambda *_: ("shared", 12, "/dev/pts/1", ""))
    assert extract_claude(pro, str(pane), 12).text is None


def test_claude_team_cannot_read_pro_profile(monkeypatch, tmp_path):
    pro = tmp_path / ".claude-pro"; team = tmp_path / ".claude-team"; pane = tmp_path / "Agent-Relay"; pane.mkdir()
    _write_session(pro, pane, "shared", _rows(pane, "shared", "PRO"))
    monkeypatch.setattr(claude, "_active_session_binding", lambda *_: ("shared", 12, "/dev/pts/1", ""))
    assert extract_claude(team, str(pane), 12).text is None


def test_claude_project_layout_uses_realpath_and_claude_encoding(tmp_path):
    root = tmp_path / ".claude-pro"; real = tmp_path / "real" / "Agent-Relay"; alias = tmp_path / "alias"
    real.mkdir(parents=True); alias.symlink_to(real, target_is_directory=True)
    project = _project_dir(root, str(alias))
    assert project == root.resolve() / "projects" / str(real.resolve()).replace("/", "-")


def test_claude_stop_sequence_is_valid_visible_final_response(monkeypatch, tmp_path):
    root = tmp_path / ".claude-pro"; pane = tmp_path / "Agent-Relay"; pane.mkdir(); sid = "actual"
    _write_session(root, pane, sid, _rows(pane, sid, "STOP_SEQUENCE_OK", "stop_sequence"))
    monkeypatch.setattr(claude, "_active_session_binding", lambda *_: (sid, 12, "/dev/pts/1", ""))
    assert extract_claude(root, str(pane)).text == "STOP_SEQUENCE_OK"


def test_claude_rejects_user_tool_thinking_and_sidechain(monkeypatch, tmp_path):
    root = tmp_path / ".claude-pro"; pane = tmp_path / "Agent-Relay"; pane.mkdir(); sid = "actual"
    rows = [
        _record("user", "USER", session_id=sid, cwd=pane),
        _record("assistant", [{"type": "thinking", "thinking": "NO"}], "tool_use", session_id=sid, cwd=pane),
        _record("assistant", [{"type": "tool_use", "name": "NO"}], "tool_use", session_id=sid, cwd=pane),
        _record("assistant", [{"type": "text", "text": "SUBAGENT"}], "end_turn", session_id=sid, cwd=pane, sidechain=True),
    ]
    _write_session(root, pane, sid, rows)
    monkeypatch.setattr(claude, "_active_session_binding", lambda *_: (sid, 12, "/dev/pts/1", ""))
    assert extract_claude(root, str(pane)).text is None


def test_claude_reconstructs_visible_fragments_across_tool_use(monkeypatch, tmp_path):
    root = tmp_path / ".claude-pro"; pane = tmp_path / "Agent-Relay"; pane.mkdir(); sid = "actual"
    rows = [
        _record("assistant", [{"type": "text", "text": "FIRST"}], "tool_use", session_id=sid, cwd=pane),
        _record("assistant", [{"type": "thinking", "thinking": "NO"}], "tool_use", session_id=sid, cwd=pane),
        _record("assistant", [{"type": "text", "text": "FINAL"}], "stop_sequence", session_id=sid, cwd=pane),
    ]
    _write_session(root, pane, sid, rows)
    monkeypatch.setattr(claude, "_active_session_binding", lambda *_: (sid, 12, "/dev/pts/1", ""))
    assert extract_claude(root, str(pane)).text == "FIRST\nFINAL"


def test_claude_unknown_active_session_fails_closed_with_multiple_project_sessions(monkeypatch, tmp_path):
    root = tmp_path / ".claude-pro"; pane = tmp_path / "Agent-Relay"; pane.mkdir()
    _write_session(root, pane, "one", _rows(pane, "one", "ONE"))
    _write_session(root, pane, "two", _rows(pane, "two", "TWO"))
    monkeypatch.setattr(claude, "_active_session_binding", lambda *_: (None, None, None, "No active session"))
    result = resolve_claude(root, str(pane), 12)
    assert result.transcript is None and result.confidence == "none"


def test_claude_newer_opposite_profile_transcript_never_steals(monkeypatch, tmp_path):
    team = tmp_path / ".claude-team"; pro = tmp_path / ".claude-pro"; pane = tmp_path / "project"; pane.mkdir()
    _write_session(team, pane, "team", _rows(pane, "team", "TEAM"))
    _write_session(pro, pane, "pro", _rows(pane, "pro", "NEWER_PRO")).touch()
    monkeypatch.setattr(claude, "_active_session_binding", lambda root, *_: ("team", 12, "/dev/pts/1", "") if root == team else ("pro", 13, "/dev/pts/2", ""))
    assert extract_claude(team, str(pane), 12).text == "TEAM"
    assert extract_claude(pro, str(pane), 13).text == "NEWER_PRO"


def test_claude_requires_live_session_even_with_one_exact_project_transcript(tmp_path):
    root = tmp_path / ".claude-pro"; pane = tmp_path / "project"; pane.mkdir()
    _write_session(root, pane, "old", _rows(pane, "old", "STALE"))
    assert extract_claude(root, str(pane), None).text is None


def test_claude_stale_session_id_is_not_reused(monkeypatch, tmp_path):
    root = tmp_path / ".claude-pro"; pane = tmp_path / "project"; pane.mkdir()
    _write_session(root, pane, "old", _rows(pane, "old", "STALE"))
    monkeypatch.setattr(claude, "_active_session_binding", lambda *_: ("restarted", 99, "/dev/pts/1", ""))
    assert extract_claude(root, str(pane), 12).text is None


def test_claude_restart_recorrelates_instead_of_caching(monkeypatch, tmp_path):
    root = tmp_path / ".claude-pro"; pane = tmp_path / "project"; pane.mkdir()
    _write_session(root, pane, "before", _rows(pane, "before", "BEFORE"))
    _write_session(root, pane, "after", _rows(pane, "after", "AFTER"))
    active = iter(["before", "after"])
    monkeypatch.setattr(claude, "_active_session_binding", lambda *_: (next(active), 12, "/dev/pts/1", ""))
    assert extract_claude(root, str(pane), 12).text == "BEFORE"
    assert extract_claude(root, str(pane), 12).text == "AFTER"


def test_claude_latest_user_record_is_never_returned(monkeypatch, tmp_path):
    root = tmp_path / ".claude-pro"; pane = tmp_path / "project"; pane.mkdir(); sid = "live"
    rows = _rows(pane, sid, "ASSISTANT") + [_record("user", "LATEST_USER", session_id=sid, cwd=pane)]
    _write_session(root, pane, sid, rows)
    monkeypatch.setattr(claude, "_active_session_binding", lambda *_: (sid, 12, "/dev/pts/1", ""))
    assert extract_claude(root, str(pane), 12).text == "ASSISTANT"


def test_claude_missing_stop_reason_is_not_a_completed_result(monkeypatch, tmp_path):
    root = tmp_path / ".claude-pro"; pane = tmp_path / "project"; pane.mkdir(); sid = "live"
    _write_session(root, pane, sid, [_record("assistant", [{"type": "text", "text": "STREAMING"}], session_id=sid, cwd=pane)])
    monkeypatch.setattr(claude, "_active_session_binding", lambda *_: (sid, 12, "/dev/pts/1", ""))
    assert extract_claude(root, str(pane), 12).text is None
