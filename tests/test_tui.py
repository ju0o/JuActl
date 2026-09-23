import io

from actl import tui
from actl.core.activity import classify_activity
from actl.core import discovery
from actl.core import validation
from actl.core.discovery import Detection
from actl.core.models import PaneInfo
from actl.core.validation import ProcessInfo


def test_activity_classification_fails_closed_on_missing_or_unknown_tail():
    assert classify_activity(None, "prompt ❯") == "UNKNOWN"
    assert classify_activity(0.0, "ordinary output") == "UNKNOWN"
    assert classify_activity(0.0, "agent ❯") == "IDLE"
    assert classify_activity(0.0, "working...") == "RUNNING"
    assert classify_activity(6.0, "ordinary output") == "RUNNING"


def test_render_shows_live_and_result_state(monkeypatch):
    out = io.StringIO()
    monkeypatch.setattr(tui.sys, "stdout", out)
    tui._render(
        [{
            "key": "1", "display": "CommandCode", "target": "%12",
            "state": "UP", "busy": "유휴", "result_flag": "●4자",
            "preview": "DONE", "detail": "",
        }],
        0,
    )
    text = out.getvalue()
    assert "유휴" in text
    assert "●4자" in text
    assert "DONE" in text


def test_pane_board_uses_selectable_row_keys_for_large_pane_ids(monkeypatch):
    pane = type("Pane", (), {
        "pane_id": "%12", "current_command": "cmd",
        "current_path": "/tmp",
    })()
    detection = type("Detection", (), {"agent": "commandcode"})()
    monkeypatch.setattr(tui, "_all_panes", lambda: [(pane, detection)])
    monkeypatch.setattr(tui, "_pane_busy", lambda _: "유휴")
    monkeypatch.setattr(tui, "_pane_result_flag", lambda *_: "●")
    monkeypatch.setattr(discovery, "mapping_state", lambda *_: "UP")

    text = tui._pane_board({})

    assert "[1] %12" in text
    assert tui._pane_board_cache({})[0][0] == "1"


def test_event_scope_separates_topology_and_pane_local_events():
    topology, pane_ids = tui._event_scope([
        "%output %12 text",
        "%pane-mode-changed %13",
        "%layout-change @1",
    ])

    assert topology is True
    assert pane_ids == {"%12", "%13"}


def test_event_scope_ignores_non_refresh_notifications():
    assert tui._event_scope(["%client-session-changed $1", "plain output"]) == (False, set())


def test_rows_passes_batched_pane_metadata_to_real_validator(monkeypatch):
    pane = PaneInfo("%1", "0:0.0", "codex", "/project", "", pane_pid=10)
    detection = Detection(pane, "codex", "high", "pid 11: codex")
    monkeypatch.setattr(validation, "target_exists", lambda *_: (_ for _ in ()).throw(AssertionError("re-read")))
    monkeypatch.setattr(validation, "pane_field", lambda *_: (_ for _ in ()).throw(AssertionError("re-read")))
    monkeypatch.setattr(validation, "pane_processes", lambda _: [ProcessInfo(11, 10, "/bin/codex")])

    rows = tui._rows({}, detections=[detection], hydrate=False)

    assert rows[0]["state"] == "UP"
    assert rows[0]["control_ready"] is True
