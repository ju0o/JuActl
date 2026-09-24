import io
from types import SimpleNamespace

from actl import tui
from actl.core.discovery import Detection
from actl.core.models import PaneInfo


def test_render_uses_one_korean_state_column_and_result_labels(monkeypatch):
    out = io.StringIO()
    monkeypatch.setattr(tui.sys, "stdout", out)

    tui._render([
        {
            "key": "1", "display": "Codex", "target": "%1",
            "state": "BLOCKED", "busy": "승인 기다림",
            "result_flag": "답 없음", "preview": "", "detail": "extractor detail",
        },
        {
            "key": "2", "display": "CommandCode", "target": "%2",
            "state": "WORKING", "busy": "실행중",
            "result_flag": "답 3자", "preview": "DONE", "detail": "",
        },
    ], 0)

    text = out.getvalue()
    assert text.count("승인 기다림") == 1
    assert "실행중" not in text
    assert "답 없음" in text
    assert "답 3자" in text
    assert "extractor detail" not in text


def test_rows_turn_waiting_input_into_blocked(monkeypatch):
    pane = PaneInfo("%1", "0:0.0", "codex", "-", "", pane_pid=10)
    detection = Detection(pane, "codex", "high", "pid 10: codex")
    monkeypatch.setattr(
        tui, "_validate_live_pane",
        lambda *_args, **_kwargs: SimpleNamespace(state="UP", valid=True, detail=""),
    )
    from actl.core import activity

    monkeypatch.setattr(activity, "observe_activity", lambda _: ("WAITING_INPUT", "prompt"))
    monkeypatch.setattr(tui, "extract_last_response", lambda *_args: SimpleNamespace(text="", detail=""))

    row = tui._rows({}, detections=[detection])[0]

    assert row["state"] == "BLOCKED"
    assert row["result_flag"] == "답 없음"


def test_rows_keep_non_up_validation_state_and_skip_busy_extraction(monkeypatch):
    cases = {"DOWN": "IDLE", "DEGRADED": "WAITING_INPUT", "TRANSPORT_BUSY": "RUNNING"}
    extracted = []
    from actl.core import activity

    monkeypatch.setattr(
        tui,
        "extract_last_response",
        lambda *_args: (extracted.append(True) or SimpleNamespace(text="", detail="")),
    )
    for validation_state, observed_state in cases.items():
        pane = PaneInfo("%1", "0:0.0", "codex", "-", "", pane_pid=10)
        detection = Detection(pane, "codex", "high", "pid 10: codex")
        monkeypatch.setattr(
            tui, "_validate_live_pane",
            lambda *_args, state=validation_state, **_kwargs: SimpleNamespace(
                state=state, valid=True, detail=""
            ),
        )
        monkeypatch.setattr(activity, "observe_activity", lambda _: (observed_state, "observed"))
        row = tui._rows({}, detections=[detection])[0]
        assert row["state"] == validation_state
    assert len(extracted) == 2
