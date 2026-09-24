from actl import serve, tui


def test_board_cards_use_korean_state_and_plain_metadata(monkeypatch):
    monkeypatch.setattr(serve, "load_config", lambda: {"agents": {}})
    monkeypatch.setattr(serve, "AGENTS", {"commandcode": object()})
    monkeypatch.setattr(tui, "_rows", lambda _: [{
        "agent": "commandcode", "display": "CommandCode", "target": "%12",
        "state": "WORKING", "runtime_state": "WORKING", "preview": "SAFE", "detail": "",
        "busy": "작업 중", "result_flag": "●3자", "project": "UNKNOWN", "role": "",
        "model_profile": "UNASSIGNED", "current_task": "UNKNOWN",
    }])
    monkeypatch.setattr(tui, "_pane_board", lambda _: "")

    data = serve._board_data()

    assert data["agents"][0]["state_ko"] == "일하는 중"
    assert "ADAPTIVE" not in serve.BOARD_HTML
    assert "OFF" not in serve.BOARD_HTML
    assert "live pane — 에이전트 클릭" not in serve.BOARD_HTML
    assert 'esc(a.runtime_state||\'UNKNOWN\')' not in serve.BOARD_HTML
    assert "a.state_ko" in serve.BOARD_HTML
