import json

from actl.agents import grok
from actl.agents.grok import extract_grok, resolve_grok


def _session(root, cwd, session, rows):
    directory = root / "sessions" / str(cwd).replace("/", "%2F") / session
    directory.mkdir(parents=True)
    (directory / "events.jsonl").write_text(json.dumps({"type": "turn_started", "session_id": session}) + "\n")
    (directory / "chat_history.jsonl").write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    return directory / "events.jsonl"


def test_grok_process_owned_session_rejects_newer_other_project(monkeypatch, tmp_path):
    cwd = tmp_path / "JuTell"; cwd.mkdir()
    active = _session(tmp_path, cwd, "active", [{"type": "user", "content": "PROMPT"}, {"type": "assistant", "content": "VISIBLE"}])
    _session(tmp_path, tmp_path / "Other", "newer", [{"type": "assistant", "content": "WRONG"}])
    monkeypatch.setattr(grok, "_grok_pid", lambda _: 7)
    monkeypatch.setattr(grok, "_open_event_files", lambda *_: [active])
    monkeypatch.setattr(grok, "pane_field", lambda *_: str(cwd))
    resolution = resolve_grok(tmp_path, "%4")
    assert resolution.confidence == "exact" and extract_grok(resolution).text == "VISIBLE"


def test_grok_rejects_user_reasoning_tool_and_ambiguous_process_files(monkeypatch, tmp_path):
    cwd = tmp_path / "JuTell"; cwd.mkdir()
    active = _session(tmp_path, cwd, "active", [{"type": "user", "content": "PROMPT"}, {"type": "reasoning", "summary": "NO"}, {"type": "tool_result", "content": "NO"}, {"type": "assistant", "content": "VISIBLE"}])
    other = _session(tmp_path, cwd, "other", [{"type": "assistant", "content": "WRONG"}])
    monkeypatch.setattr(grok, "_grok_pid", lambda _: 7)
    monkeypatch.setattr(grok, "pane_field", lambda *_: str(cwd))
    monkeypatch.setattr(grok, "_open_event_files", lambda *_: [active])
    assert extract_grok(resolve_grok(tmp_path, "%4")).text == "VISIBLE"
    monkeypatch.setattr(grok, "_open_event_files", lambda *_: [active, other])
    assert resolve_grok(tmp_path, "%4").confidence == "none"


def test_grok_restart_with_new_pid_recorrelates(monkeypatch, tmp_path):
    cwd = tmp_path / "JuTell"; cwd.mkdir()
    active = _session(tmp_path, cwd, "active", [{"type": "user", "content": "PROMPT"}, {"type": "assistant", "content": "RESTART_OK"}])
    monkeypatch.setattr(grok, "pane_field", lambda *_: str(cwd))
    monkeypatch.setattr(grok, "_open_event_files", lambda *_: [active])

    monkeypatch.setattr(grok, "_grok_pid", lambda _: 7)
    first = resolve_grok(tmp_path, "%4")
    monkeypatch.setattr(grok, "_grok_pid", lambda _: 54321)
    second = resolve_grok(tmp_path, "%4")
    assert first.confidence == "exact" and second.confidence == "exact"
    assert extract_grok(second).text == "RESTART_OK"
