from actl import cli
from actl.core.registry import ALIASES


CFG = {"agents": {"claude-team": {"target": "fake:0.0"}, "claude-pro": {"target": "fake:0.1"}, "codex": {"target": "fake:0.2"}}}


def test_claude_aliases_are_disjoint():
    assert ALIASES["ct"] == ALIASES["team"] == "claude-team"
    assert ALIASES["cp"] == ALIASES["pro"] == "claude-pro"
    assert "claude" not in ALIASES and "cc" not in ALIASES


def test_numeric_menu_order_is_canonical():
    assert cli._resolve_selection("1") == "claude-team"
    assert cli._resolve_selection("2") == "claude-pro"
    assert cli._resolve_selection("4") == "codex"
    assert cli._resolve_selection("5") == cli._resolve_selection("cursor") == cli._resolve_selection("cu") == "cursor"


def test_switching_re_resolves_target_for_each_send(monkeypatch):
    sent = []
    monkeypatch.setattr(cli, "_resolve_live_target", lambda config, agent: config["agents"][agent]["target"])
    monkeypatch.setattr(cli, "send_prompt", lambda target, prompt: sent.append((target, prompt)))
    for agent, prompt in [("claude-team", "TEAM"), ("claude-pro", "PRO"), ("claude-team", "TEAM2"), ("claude-pro", "PRO2")]:
        cli._send_to_selected(CFG, agent, prompt)
    assert sent == [("fake:0.0", "TEAM"), ("fake:0.1", "PRO"), ("fake:0.0", "TEAM2"), ("fake:0.1", "PRO2")]


def test_unmapped_agent_without_live_pane_never_sends(monkeypatch):
    monkeypatch.setattr(cli, "send_prompt", lambda *_: (_ for _ in ()).throw(AssertionError("must not send")))
    monkeypatch.setattr(cli, "discover", lambda: [])
    try:
        cli._send_to_selected(CFG, "opencode", "NO")
    except ValueError:
        pass
    else:
        raise AssertionError("expected unmapped failure")


def test_unmapped_agent_with_unique_live_pane_auto_maps_and_sends(monkeypatch):
    sent = []
    pane = type("P", (), {"pane_id": "%0"})()
    det = type("D", (), {"agent": "opencode", "confidence": "high", "pane": pane})()
    monkeypatch.setattr(cli, "discover", lambda: [det])
    monkeypatch.setattr(cli, "backup_config", lambda: "bak")
    monkeypatch.setattr(cli, "save_config", lambda cfg: None)
    monkeypatch.setattr(cli, "send_prompt", lambda target, prompt: sent.append((target, prompt)))
    cli._send_to_selected(CFG, "opencode", "AUTO")
    assert sent == [("%0", "AUTO")]


def test_stale_codex_target_cannot_send(monkeypatch):
    monkeypatch.setattr(cli, "send_prompt", lambda *_: (_ for _ in ()).throw(AssertionError("must not send")))
    monkeypatch.setattr(cli, "discover", lambda: [])
    monkeypatch.setattr(cli, "validate_target", lambda *_: type("V", (), {"valid": False, "state": "MISMATCH", "detail": "blocked"})())
    try:
        cli._send_to_selected(CFG, "codex", "NO")
    except ValueError as exc:
        assert "연결된 실행 화면을 확인하지 못했어요" in str(exc)
        assert "blocked" not in str(exc)
    else:
        raise AssertionError("expected stale mapping failure")
