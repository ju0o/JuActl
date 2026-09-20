from actl import cli
from actl.core import discovery, tmux
from actl.core.discovery import Detection, detect_pane, manual_map, reconcile
from actl.core.models import PaneInfo
from actl.core.registry import AGENTS
from actl.core.validation import ProcessInfo, TargetValidation


PANE = PaneInfo("%3", "work:0.3", "bash", "/project", "")


def _detect(monkeypatch, processes, env=None):
    monkeypatch.setattr(discovery, "pane_field", lambda *_: "10")
    monkeypatch.setattr(discovery, "pane_processes", lambda _: processes)
    monkeypatch.setattr(discovery, "process_environment", lambda _: env or {})
    return detect_pane(PANE)


def test_claude_team_detection(monkeypatch):
    result = _detect(monkeypatch, [ProcessInfo(11, 10, "/x/claude")], {"CLAUDE_CONFIG_DIR": str(AGENTS["claude-team"].data_dirs[0])})
    assert (result.agent, result.confidence) == ("claude-team", "exact")


def test_remote_claude_team_profile_matches_by_provider_directory(monkeypatch):
    monkeypatch.setattr(tmux, "REMOTE_SSH_TARGET", "asus")
    result = _detect(monkeypatch, [ProcessInfo(11, 10, "/x/claude")], {"CLAUDE_CONFIG_DIR": "/home/remote/.claude-team"})
    assert (result.agent, result.confidence) == ("claude-team", "exact")


def test_wrapped_claude_team_detection(monkeypatch):
    result = _detect(monkeypatch, [ProcessInfo(11, 10, "node /opt/claude.cmd --tui")], {"CLAUDE_CONFIG_DIR": str(AGENTS["claude-team"].data_dirs[0])})
    assert (result.agent, result.confidence) == ("claude-team", "exact")


def test_claude_pro_detection(monkeypatch):
    result = _detect(monkeypatch, [ProcessInfo(11, 10, "claude")], {"CLAUDE_CONFIG_DIR": str(AGENTS["claude-pro"].data_dirs[0])})
    assert (result.agent, result.confidence) == ("claude-pro", "exact")


def test_claude_team_pro_isolation(monkeypatch):
    result = _detect(monkeypatch, [ProcessInfo(11, 10, "claude")], {"CLAUDE_CONFIG_DIR": "/tmp/not-a-supported-profile"})
    assert result.agent is None and result.confidence == "low"


def test_opencode_detection(monkeypatch):
    assert _detect(monkeypatch, [ProcessInfo(11, 10, "/bin/opencode")]).agent == "opencode"


def test_opencode_server_is_not_detected(monkeypatch):
    result = _detect(monkeypatch, [ProcessInfo(11, 10, "/bin/opencode serve --port 4111")])
    assert result.agent is None and result.confidence == "unknown"


def test_reconcile_does_not_map_opencode_server(monkeypatch):
    detected = _detect(monkeypatch, [ProcessInfo(11, 10, "/bin/opencode serve --port 4111")])
    updated, changes = reconcile({"agents": {}}, [detected])
    assert "opencode" not in updated["agents"]
    assert not changes


def test_grok_detection(monkeypatch):
    assert _detect(monkeypatch, [ProcessInfo(11, 10, "/bin/grok")]).agent == "grok"


def test_node_wrapped_cline_and_commandcode_detection(monkeypatch):
    assert _detect(monkeypatch, [ProcessInfo(11, 10, "node /usr/local/bin/cline --tui")]).agent == "cline"
    assert _detect(monkeypatch, [ProcessInfo(11, 10, "Command Code commandcode")]).agent == "commandcode"


def test_codex_detection(monkeypatch):
    assert _detect(monkeypatch, [ProcessInfo(11, 10, "/bin/codex")]).agent == "codex"


def test_cursor_detection(monkeypatch):
    assert _detect(monkeypatch, [ProcessInfo(11, 10, "/x/cursor-agent/index.js")]).agent == "cursor"


def test_unknown_pane_is_ignored(monkeypatch):
    result = _detect(monkeypatch, [ProcessInfo(11, 10, "/bin/bash")])
    assert result.agent is None and result.confidence == "unknown"


def test_nested_agent_tool_does_not_override_pane_agent(monkeypatch):
    result = _detect(monkeypatch, [ProcessInfo(11, 10, "grok"), ProcessInfo(12, 11, "/bin/opencode")])
    assert result.agent == "grok"


def test_duplicate_pane_mapping_is_rejected(monkeypatch):
    monkeypatch.setattr(discovery, "validate_target", lambda *_: TargetValidation("UP", "%3"))
    detection = Detection(PANE, "cursor", "exact", "pid 11")
    try:
        manual_map({"agents": {"codex": {"target": "%3"}}}, "cursor", detection)
    except ValueError as exc:
        assert "already mapped" in str(exc)
    else:
        raise AssertionError("duplicate pane mapping was accepted")


def test_apply_removes_stale_mapping_and_stores_pane_id(monkeypatch):
    monkeypatch.setattr(discovery, "validate_target", lambda *_: TargetValidation("DOWN", "%gone"))
    detected = Detection(PANE, "opencode", "high", "pid 11: opencode")
    updated, changes = reconcile({"agents": {"codex": {"target": "%gone"}}}, [detected])
    assert "codex" not in updated["agents"]
    assert updated["agents"]["opencode"]["target"] == "%3"
    assert changes


def test_low_confidence_is_not_auto_applied():
    detected = Detection(PANE, "cursor", "low", "unproven")
    updated, changes = reconcile({"agents": {}}, [detected])
    assert updated["agents"] == {} and not changes


def test_unique_only_reconcile_maps_one_strong_runtime(monkeypatch):
    detected = Detection(PANE, "cursor", "high", "pid 11: cursor")
    updated, changes = reconcile({"agents": {}}, [detected], unique_only=True)
    assert updated["agents"]["cursor"]["target"] == "%3"
    assert changes


def test_unique_only_reconcile_does_not_map_ambiguous_agent():
    first = Detection(PANE, "codex", "high", "pid 11: codex")
    second = Detection(PaneInfo("%4", "work:0.4", "codex", "/project", ""), "codex", "high", "pid 12: codex")
    updated, changes = reconcile({"agents": {}}, [first, second], unique_only=True)
    assert updated["agents"] == {} and not changes


def test_manual_map_override_stores_pane_id(monkeypatch):
    monkeypatch.setattr(discovery, "validate_target", lambda *_: TargetValidation("UP", "%3"))
    updated = manual_map({"agents": {}}, "cursor", Detection(PANE, None, "unknown", "manual choice"))
    assert updated["agents"]["cursor"]["target"] == "%3"


def test_pane_id_resolution(monkeypatch):
    class Result:
        returncode = 0
        stdout = "%3\twork:0.3\n"

    monkeypatch.setattr(tmux.subprocess, "run", lambda *_, **__: Result())
    assert tmux.target_exists("%3")


def test_discovery_never_sends(monkeypatch):
    monkeypatch.setattr(cli, "send_prompt", lambda *_: (_ for _ in ()).throw(AssertionError("must not send")))
    cli._discover({"agents": {}}, [Detection(PANE, "codex", "high", "pid 11: codex")])
