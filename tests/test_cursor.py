import json
import sqlite3

from actl.agents import cursor
from actl.agents.cursor import CursorResolution, extract_cursor, resolve_cursor


def _chat(root, workspace, session, title, messages):
    chat = root / "workspace" / session
    chat.mkdir(parents=True)
    (chat / "meta.json").write_text(json.dumps({"hasConversation": True, "cwd": str(workspace), "title": title}))
    db = chat / "store.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE blobs (id TEXT PRIMARY KEY, data BLOB)")
    con.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value BLOB)")
    con.execute("INSERT INTO meta VALUES ('0', ?)", (json.dumps({"agentId": session}),))
    for index, message in enumerate(messages):
        con.execute("INSERT INTO blobs VALUES (?, ?)", (str(index), json.dumps(message)))
    con.commit(); con.close()
    return db


def _active_resolution(monkeypatch, root, db, workspace, title="Active"):
    monkeypatch.setattr(cursor, "_cursor_process", lambda *_: 77)
    monkeypatch.setattr(cursor, "_open_store_dbs", lambda *_: [db])
    monkeypatch.setattr(cursor, "pane_field", lambda _target, field: str(workspace) if field == "#{pane_current_path}" else title)
    return resolve_cursor(root, "live:0.0", 77)


def test_cursor_target_uses_open_active_store_not_newest_unrelated_db(monkeypatch, tmp_path):
    workspace = tmp_path / "JuCeipt"; workspace.mkdir()
    other = tmp_path / "Other"; other.mkdir()
    active = _chat(tmp_path, workspace, "active", "Active", [{"role": "user", "content": []}, {"role": "assistant", "content": [{"type": "text", "text": "ACTIVE"}]}])
    newer = _chat(tmp_path, other, "newer", "Other", [{"role": "user", "content": []}, {"role": "assistant", "content": [{"type": "text", "text": "WRONG"}]}])
    newer.touch()
    resolution = _active_resolution(monkeypatch, tmp_path, active, workspace)
    assert resolution.store_db == active and extract_cursor(resolution).text == "ACTIVE"


def test_cursor_same_workspace_multiple_conversations_selects_open_session(monkeypatch, tmp_path):
    workspace = tmp_path / "JuCeipt"; workspace.mkdir()
    active = _chat(tmp_path, workspace, "active", "Active", [{"role": "user", "content": []}, {"role": "assistant", "content": [{"type": "text", "text": "ACTIVE"}]}])
    _chat(tmp_path, workspace, "other", "Other", [{"role": "user", "content": []}, {"role": "assistant", "content": [{"type": "text", "text": "WRONG"}]}])
    resolution = _active_resolution(monkeypatch, tmp_path, active, workspace)
    assert resolution.session_id == "active" and resolution.confidence == "exact"


def test_cursor_unknown_active_conversation_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setattr(cursor, "_cursor_process", lambda *_: 77)
    monkeypatch.setattr(cursor, "_open_store_dbs", lambda *_: [])
    assert resolve_cursor(tmp_path, "live:0.0", 77).confidence == "none"


def test_cursor_rejects_user_tool_and_internal_blobs(tmp_path):
    db = _chat(tmp_path, tmp_path, "active", "Active", [
        {"role": "user", "content": [{"type": "text", "text": "PROMPT"}]},
        {"role": "assistant", "content": [{"type": "reasoning", "text": "THINK"}, {"type": "tool-call", "text": "TOOL"}]},
        {"role": "tool", "content": [{"type": "text", "text": "RESULT"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "VISIBLE"}, {"type": "tool-call", "text": "NO"}]},
    ])
    result = extract_cursor(CursorResolution(store_db=db, confidence="exact"))
    assert result.text == "VISIBLE"


def test_cursor_current_schema_reconstructs_visible_assistant_fragments(tmp_path):
    db = _chat(tmp_path, tmp_path, "active", "Active", [
        {"role": "user", "content": []},
        {"role": "assistant", "content": [{"type": "text", "text": "FIRST"}, {"type": "tool-call", "name": "x"}]},
        {"role": "assistant", "content": [{"type": "reasoning", "text": "NO"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "FINAL"}]},
    ])
    assert extract_cursor(CursorResolution(store_db=db, confidence="exact")).text == "FIRST\nFINAL"


def test_cursor_metadata_must_match_selected_pane(monkeypatch, tmp_path):
    workspace = tmp_path / "JuCeipt"; workspace.mkdir()
    active = _chat(tmp_path, workspace, "active", "Different", [])
    resolution = _active_resolution(monkeypatch, tmp_path, active, workspace, title="Active")
    assert resolution.confidence == "none" and "title" in resolution.detail


def test_cursor_current_hex_encoded_store_metadata_is_supported(monkeypatch, tmp_path):
    workspace = tmp_path / "JuCeipt"; workspace.mkdir()
    active = _chat(tmp_path, workspace, "active", "Active", [])
    con = sqlite3.connect(active)
    con.execute("UPDATE meta SET value = ?", (json.dumps({"agentId": "active"}).encode().hex(),))
    con.commit(); con.close()
    assert _active_resolution(monkeypatch, tmp_path, active, workspace).confidence == "exact"


def test_cursor_restart_with_new_pid_recorrelates(monkeypatch, tmp_path):
    # Relaunching Cursor Agent yields a new PID; correlation must follow the
    # new live process-owned store.db rather than a cached PID.
    workspace = tmp_path / "JuCeipt"; workspace.mkdir()
    active = _chat(tmp_path, workspace, "active", "Active", [{"role": "user", "content": []}, {"role": "assistant", "content": [{"type": "text", "text": "RESTART_OK"}]}])
    monkeypatch.setattr(cursor, "_open_store_dbs", lambda *_: [active])
    monkeypatch.setattr(cursor, "pane_field", lambda _target, field: str(workspace) if field == "#{pane_current_path}" else "Active")

    monkeypatch.setattr(cursor, "_cursor_process", lambda *_: 77)
    first = resolve_cursor(tmp_path, "live:0.0", 77)
    monkeypatch.setattr(cursor, "_cursor_process", lambda *_: 88888)
    second = resolve_cursor(tmp_path, "live:0.0", 88888)
    assert first.confidence == "exact" and second.confidence == "exact"
    assert extract_cursor(second).text == "RESTART_OK"
