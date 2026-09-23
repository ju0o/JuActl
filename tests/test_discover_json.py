from __future__ import annotations

import io
import json
from contextlib import redirect_stdout

from actl import cli
from actl.core.discovery import Detection
from actl.core.models import PaneInfo


def _detection() -> Detection:
    return Detection(PaneInfo("%3", "work:0.3", "bash", "/project", ""), "codex", "high", "pid 11: codex")


def test_discover_json_shape_and_tail(monkeypatch):
    monkeypatch.setattr(cli, "capture_pane", lambda pane_id, history: "\nold\n\ncurrent\nready\n")
    output = io.StringIO()
    with redirect_stdout(output):
        cli._discover_json({"agents": {}}, [_detection()])
    payload = json.loads(output.getvalue())
    assert list(payload) == ["panes"]
    assert payload["panes"] == [{
        "paneId": "%3",
        "cwd": "/project",
        "command": "bash",
        "detected": "codex",
        "confidence": "high",
        "mapping": "UNMAPPED",
        "evidence": "pid 11: codex",
        "tail": ["old", "current", "ready"],
    }]


def test_discover_json_empty(monkeypatch):
    output = io.StringIO()
    with redirect_stdout(output):
        cli._discover_json({"agents": {}}, [])
    assert json.loads(output.getvalue()) == {"panes": []}


def test_discover_json_capture_error_has_empty_tail(monkeypatch):
    monkeypatch.setattr(cli, "capture_pane", lambda *_: (_ for _ in ()).throw(RuntimeError("tmux down")))
    output = io.StringIO()
    with redirect_stdout(output):
        cli._discover_json({"agents": {}}, [_detection()])
    assert json.loads(output.getvalue())["panes"][0]["tail"] == []


def test_discover_json_rejects_apply(monkeypatch):
    monkeypatch.setattr(cli.sys, "argv", ["actl", "discover", "--json", "--apply"])
    monkeypatch.setattr(cli, "ensure_config", lambda: (_ for _ in ()).throw(AssertionError("config mutation")))
    try:
        cli.main()
    except SystemExit as exc:
        assert exc.code != 0
    else:
        raise AssertionError("expected rejection")
