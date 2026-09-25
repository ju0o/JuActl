import io
import json
from contextlib import redirect_stdout

from actl import cli


def _status(monkeypatch, config, detections, updated=None, changes=None):
    calls = []
    monkeypatch.setattr(cli, "AGENTS", {"codex": cli.AGENTS["codex"]})
    monkeypatch.setattr(cli, "ensure_config", lambda: None)
    monkeypatch.setattr(cli, "load_config", lambda: config)
    monkeypatch.setattr(cli, "discover", lambda: detections)
    monkeypatch.setattr(cli, "reconcile", lambda *_: (updated or config, changes or []))
    monkeypatch.setattr(cli, "backup_config", lambda: calls.append("backup") or "backup")
    monkeypatch.setattr(cli, "save_config", lambda cfg: calls.append(cfg))
    monkeypatch.setattr(cli, "agent_status", lambda cfg, _: {
        "agent": "Codex", "target": cfg["agents"]["codex"]["target"], "pane": "UP",
        "command": "codex", "path": "/project", "binary": "codex",
    })
    return calls


def test_status_auto_reconciles_and_announces_in_korean(monkeypatch):
    config = {"agents": {"codex": {"target": "%old"}}}
    updated = {"agents": {"codex": {"target": "%10"}}}
    calls = _status(monkeypatch, config, [object()], updated, ["codex: %10"])
    out = io.StringIO()
    with redirect_stdout(out):
        cli.main(["status", "codex"])
    assert "자동 연결했어요: Codex → %10" in out.getvalue()
    assert "%10" in out.getvalue()
    assert calls == ["backup", updated]


def test_status_json_auto_reconciles_without_non_json_output(monkeypatch):
    config = {"agents": {"codex": {"target": "%old"}}}
    updated = {"agents": {"codex": {"target": "%10"}}}
    _status(monkeypatch, config, [object()], updated, ["codex: %10"])
    out = io.StringIO()
    with redirect_stdout(out):
        cli.main(["status", "codex", "--json"])
    assert json.loads(out.getvalue()) == [{"id": "codex", "agent": "Codex", "target": "%10", "pane": "UP", "command": "codex", "path": "/project", "binary": "codex"}]


def test_dash_status_auto_reconciles(monkeypatch):
    config = {"agents": {"codex": {"target": "%old"}}}
    updated = {"agents": {"codex": {"target": "%10"}}}
    _status(monkeypatch, config, [object()], updated, ["codex: %10"])
    out = io.StringIO()
    with redirect_stdout(out):
        cli.main(["--status", "codex"])
    assert "%10" in out.getvalue()


def test_status_discovery_error_keeps_old_read_only_rows(monkeypatch):
    config = {"agents": {"codex": {"target": "%old"}}}
    _status(monkeypatch, config, [])
    monkeypatch.setattr(cli, "discover", lambda: (_ for _ in ()).throw(RuntimeError("tmux unavailable")))
    out = io.StringIO()
    with redirect_stdout(out):
        cli.main(["status", "codex"])
    assert "%old" in out.getvalue()
