import json
import sqlite3

from actl import cli
from actl.agents.codex import CodexResolution, extract_codex
from actl.agents.extract import extract_last_response
from actl.agents.opencode import extract_opencode, resolve_opencode
from actl.agents.generic_store import _candidate_files
from actl.core.models import CopyResult


def test_codex_requires_correlated_resolution_not_newest_root(tmp_path):
    p = tmp_path / "sessions" / "2026" / "x.jsonl"
    p.parent.mkdir(parents=True)
    p.write_text(
        json.dumps(
            {
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "item": {"type": "AgentMessage", "phase": "final_answer", "content": [{"type": "Text", "text": "ACTL_OK"}]},
                },
            }
        )
        + "\n"
    )
    assert extract_codex(tmp_path) is None
    assert extract_codex(CodexResolution(rollout_path=p, confidence="exact")).text == "ACTL_OK"


def test_opencode_fails_closed_without_live_session_binding(monkeypatch, tmp_path):
    from actl.agents import extract as extract_mod

    p = tmp_path / "opencode.db"
    from dataclasses import replace as _replace
    _fake_agents = dict(extract_mod.AGENTS)
    _fake_agents["opencode"] = _replace(extract_mod.AGENTS["opencode"], data_dirs=(tmp_path,))
    monkeypatch.setattr(extract_mod, "AGENTS", _fake_agents)
    con = sqlite3.connect(p)
    con.executescript(
        """
      CREATE TABLE session (id TEXT, time_updated INTEGER, time_created INTEGER);
      CREATE TABLE message (id TEXT, session_id TEXT, time_updated INTEGER, time_created INTEGER, data TEXT);
      CREATE TABLE part (message_id TEXT, time_updated INTEGER, time_created INTEGER, data TEXT);
    """
    )
    con.execute("INSERT INTO session VALUES ('s', 2, 1)")
    con.execute("INSERT INTO message VALUES ('m', 's', 2, 1, ?)", (json.dumps({"role": "assistant"}),))
    con.execute("INSERT INTO part VALUES ('m', 2, 1, ?)", (json.dumps({"type": "text", "text": "opencode answer"}),))
    con.commit()
    con.close()
    assert extract_opencode(tmp_path) is None
    resolution = resolve_opencode(tmp_path, "%9")
    assert resolution.confidence == "none"
    assert resolution.session_id is None
    monkeypatch.setattr(
        extract_mod,
        "validate_target",
        lambda *_: type("V", (), {"valid": True, "agent_pid": 1, "detail": ""})(),
    )
    result = extract_last_response("opencode", "%9")
    assert result.text is None and result.source == "opencode-blocked"


def test_generic_store_excludes_sensitive_file_and_directory(tmp_path):
    (tmp_path / "auth.json").write_text('{"role":"assistant"}')
    secret_dir = tmp_path / "oauth-cache"
    secret_dir.mkdir()
    (secret_dir / "session.jsonl").write_text('{"role":"assistant"}')
    safe = tmp_path / "session.jsonl"
    safe.write_text('{"role":"assistant"}')
    assert _candidate_files([tmp_path]) == [safe]


def test_commandcode_is_blocked_without_read_only_session_binding():
    # Without a live process correlation the extractor fails closed; the legacy
    # unconditional "blocked" behavior was replaced by real correlation.
    result = extract_last_response("commandcode", "%3")
    assert result.text is None and result.confidence == "none"
    assert result.source in {"commandcode-unresolved", "commandcode-blocked"}


def test_copy_and_copy_diagnostic_never_send(monkeypatch):
    monkeypatch.setattr(cli, "_resolve_live_target", lambda *_: "%3")
    monkeypatch.setattr(cli, "extract_last_response", lambda *_: CopyResult(None, "unresolved", "none"))
    monkeypatch.setattr(cli, "send_prompt", lambda *_: (_ for _ in ()).throw(AssertionError("must not send")))
    cli._copy({}, "commandcode")
    assert cli._copy_diagnostic({}, "commandcode") == 1
