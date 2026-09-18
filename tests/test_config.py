from unittest.mock import patch

from actl import cli
from actl.core.config import DEFAULT_CONFIG, get_target, migration_warning


def test_direct_target_wins():
    cfg = {"tmux_session": "agents", "window": "0", "agents": {"cursor": {"pane": "3", "target": "x:2.9"}}}
    assert get_target(cfg, "cursor").target == "x:2.9"


def test_legacy_config_stays_legacy_and_warns():
    cfg = {"tmux_session": "agents", "window": "0", "agents": {"claude": {"pane": "0"}}}
    assert migration_warning(cfg)
    assert "claude-team" not in cfg["agents"]


def test_new_default_does_not_hardcode_agents_session():
    assert "tmux_session" not in DEFAULT_CONFIG


def test_three_explicit_mappings_leave_other_agents_unmapped():
    cfg = {"agents": {"claude-team": {"target": "0:0.0"}, "codex": {"target": "0:0.2"}, "claude-pro": {"target": "0:0.3"}}}
    assert get_target(cfg, "codex").target == "0:0.2"
    try:
        get_target(cfg, "opencode")
    except ValueError as exc:
        assert "not currently mapped" in str(exc)
    else:
        raise AssertionError("unmapped Agent must not fall back to agents session")


def test_numeric_session_target_is_preserved():
    cfg = {"agents": {"claude-team": {"target": "0:0.0"}}}
    assert get_target(cfg, "claude-team").target == "0:0.0"


def test_startup_auto_reconciles_stale_mapping():
    config = {"agents": {"codex": {"target": "%gone"}}}
    with patch.object(cli, "discover", return_value=[]), \
         patch.object(cli, "reconcile", return_value=({"agents": {}}, ["codex: stale mapping removed"])) as rec, \
         patch.object(cli, "backup_config", return_value="bak"), \
         patch.object(cli, "save_config") as save:
        updated = cli._auto_reconcile(config)
    rec.assert_called_once()
    save.assert_called_once_with({"agents": {}})
    assert updated == {"agents": {}}


def test_resolve_live_target_keeps_valid_mapping(monkeypatch):
    monkeypatch.setattr(cli, "get_target", lambda cfg, agent: type("T", (), {"target": "%1"})())
    monkeypatch.setattr(cli, "validate_target", lambda *_: type("V", (), {"valid": True})())
    assert cli._resolve_live_target({"agents": {"pro": {"target": "%1"}}}, "pro") == "%1"


def test_resolve_live_target_auto_remaps_unique_live_pane(monkeypatch):
    monkeypatch.setattr(cli, "get_target", lambda cfg, agent: type("T", (), {"target": "%gone"})())
    monkeypatch.setattr(cli, "validate_target", lambda *_: type("V", (), {"valid": False, "state": "DOWN", "detail": "gone"})())
    pane = type("P", (), {"pane_id": "%9"})()
    det = type("D", (), {"agent": "codex", "confidence": "high", "pane": pane})()
    monkeypatch.setattr(cli, "discover", lambda: [det])
    saved = {}
    monkeypatch.setattr(cli, "backup_config", lambda: "bak")
    monkeypatch.setattr(cli, "save_config", lambda cfg: saved.update(cfg))
    target = cli._resolve_live_target({"agents": {"codex": {"target": "%gone"}}}, "codex")
    assert target == "%9"
    assert saved["agents"]["codex"]["target"] == "%9"


def test_resolve_live_target_ambiguous_panes_fail_closed(monkeypatch):
    monkeypatch.setattr(cli, "get_target", lambda cfg, agent: type("T", (), {"target": "%gone"})())
    monkeypatch.setattr(cli, "validate_target", lambda *_: type("V", (), {"valid": False, "state": "DOWN", "detail": "gone"})())
    pane_a = type("P", (), {"pane_id": "%9"})()
    pane_b = type("P", (), {"pane_id": "%8"})()
    dets = [
        type("D", (), {"agent": "codex", "confidence": "high", "pane": pane_a})(),
        type("D", (), {"agent": "codex", "confidence": "high", "pane": pane_b})(),
    ]
    monkeypatch.setattr(cli, "discover", lambda: dets)
    monkeypatch.setattr(cli, "save_config", lambda *_: (_ for _ in ()).throw(AssertionError("must not write on ambiguity")))
    try:
        cli._resolve_live_target({"agents": {"codex": {"target": "%gone"}}}, "codex")
    except ValueError as exc:
        assert "Multiple" in str(exc)
    else:
        raise AssertionError("expected ambiguity failure")
