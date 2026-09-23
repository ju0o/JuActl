from actl.core import validation
from actl.core.models import PaneInfo
from actl.core.validation import ProcessInfo


def _pane_fields(_target, field):
    return {"#{pane_id}": "%1", "#{pane_current_command}": "agent", "#{pane_current_path}": "/project", "#{pane_pid}": "10"}[field]


def test_configured_cursor_target_with_cursor_process_is_up(monkeypatch):
    monkeypatch.setattr(validation, "target_exists", lambda _: True)
    monkeypatch.setattr(validation, "pane_field", _pane_fields)
    monkeypatch.setattr(validation, "pane_processes", lambda _: [ProcessInfo(11, 10, "/x/cursor-agent/index.js")])
    assert validation.validate_target("cursor", "0:0.3").state == "UP"


def test_batched_pane_metadata_skips_remote_field_reads(monkeypatch):
    pane = PaneInfo("%1", "0:0.3", "cursor", "/project", "", pane_pid=10)
    monkeypatch.setattr(validation, "target_exists", lambda *_: (_ for _ in ()).throw(AssertionError("re-read")))
    monkeypatch.setattr(validation, "pane_field", lambda *_: (_ for _ in ()).throw(AssertionError("re-read")))
    monkeypatch.setattr(validation, "pane_processes", lambda _: [ProcessInfo(11, 10, "/x/cursor-agent/index.js")])
    result = validation.validate_target("cursor", "0:0.3", pane=pane)
    assert (result.state, result.pane_id, result.pane_pid) == ("UP", "%1", 10)


def test_configured_codex_target_with_cursor_process_is_mismatch(monkeypatch):
    monkeypatch.setattr(validation, "target_exists", lambda _: True)
    monkeypatch.setattr(validation, "pane_field", _pane_fields)
    monkeypatch.setattr(validation, "pane_processes", lambda _: [ProcessInfo(11, 10, "/x/cursor-agent/index.js")])
    assert validation.validate_target("codex", "0:0.3").state == "MISMATCH"


def test_node_wrapped_cline_and_commandcode_are_valid(monkeypatch):
    monkeypatch.setattr(validation, "target_exists", lambda _: True)
    monkeypatch.setattr(validation, "pane_field", _pane_fields)
    monkeypatch.setattr(validation, "pane_processes", lambda _: [ProcessInfo(11, 10, "node /usr/local/bin/cline --tui")])
    assert validation.validate_target("cline", "%8").state == "UP"
    monkeypatch.setattr(validation, "pane_processes", lambda _: [ProcessInfo(11, 10, "Command Code commandcode")])
    assert validation.validate_target("commandcode", "%7").state == "UP"


def test_wrapped_claude_profile_is_valid(monkeypatch):
    monkeypatch.setattr(validation, "target_exists", lambda _: True)
    monkeypatch.setattr(validation, "pane_field", _pane_fields)
    monkeypatch.setattr(validation, "pane_processes", lambda _: [ProcessInfo(11, 10, "node C:\\Tools\\claude.cmd --tui")])
    monkeypatch.setattr(validation, "process_environment", lambda _: {"CLAUDE_CONFIG_DIR": str(validation.AGENTS["claude-team"].data_dirs[0])})
    assert validation.validate_target("claude-team", "%8").state == "UP"


def test_opencode_server_is_not_a_tui():
    assert validation.is_opencode_tui("opencode --auto")
    assert not validation.is_opencode_tui("opencode serve --hostname 127.0.0.1")
    assert not validation.is_opencode_tui("opencode acp")
