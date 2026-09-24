import io

from actl import tui


def test_render_has_one_korean_working_state_column(monkeypatch):
    out = io.StringIO()
    monkeypatch.setattr(tui.sys, "stdout", out)

    tui._render([{
        "key": "1", "display": "Codex", "target": "%1",
        "state": "WORKING", "busy": "실행중", "runtime_state": "WORKING",
        "result_flag": "●4자", "preview": "DONE", "detail": "",
    }], 0)

    text = out.getvalue()
    assert text.count("작업 중") == 1
    assert "WORKING" not in text


def test_render_hides_extractor_detail_when_result_is_missing(monkeypatch):
    out = io.StringIO()
    monkeypatch.setattr(tui.sys, "stdout", out)

    tui._render([{
        "key": "1", "display": "Codex", "target": "%1",
        "state": "UP", "busy": "실행중", "result_flag": "○대기",
        "preview": "", "detail": "No Codex rollout in this pane directory was active",
    }], 0)

    text = out.getvalue()
    assert "아직 답 없음" in text
    assert "No Codex rollout" not in text
