from actl import cli, serve
from actl import tui


def test_board_get_is_read_only_and_exposes_activity(monkeypatch):
    calls = []
    monkeypatch.setattr(serve, "load_config", lambda: {"agents": {}})
    monkeypatch.setattr(serve, "AGENTS", {"commandcode": object()})
    monkeypatch.setattr(tui, "_rows", lambda _: [{
        "agent": "commandcode", "display": "CommandCode", "target": "%12",
        "state": "UP", "preview": "SAFE", "detail": "", "busy": "유휴",
        "result_flag": "●3자",
    }])
    monkeypatch.setattr(tui, "_pane_board", lambda _: "  [1] %12 cmd commandcode 미확인 결과●3자 정상 /tmp")
    monkeypatch.setattr(cli, "_auto_reconcile", lambda cfg: calls.append(cfg) or cfg)

    data = serve._board_data()

    assert calls == []
    assert data["agents"][0]["activity_ko"] == "유휴"
    assert data["agents"][0]["result_flag"] == "●3자"
    assert 'data-pane="%12"' in data["boardHtml"]
    assert 'data-agent="commandcode"' in data["boardHtml"]


def test_board_refresh_is_the_explicit_reconcile_path(monkeypatch):
    calls = []
    monkeypatch.setattr(serve, "load_config", lambda: {"agents": {}})
    monkeypatch.setattr(cli, "_auto_reconcile", lambda cfg: calls.append(cfg) or cfg)
    monkeypatch.setattr(tui, "_rows", lambda _: [])
    monkeypatch.setattr(tui, "_pane_board", lambda _: "")

    serve._board_data(reconcile=True)

    assert len(calls) == 1


def test_board_html_escapes_dynamic_card_values():
    assert "function esc" in serve.BOARD_HTML
    assert "esc((a.preview||a.detail" in serve.BOARD_HTML


def test_web_copy_ack_is_separate_from_clipboard_fallback():
    assert "복사는 완료됐지만 상태 확인 실패" in serve.BOARD_HTML
    assert "return doPrint()" in serve.BOARD_HTML
    assert "result_hash:d.result_hash" in serve.BOARD_HTML
    assert "REFRESHING" in serve.BOARD_HTML
    assert "ArrowDown" in serve.BOARD_HTML


def test_web_input_contract_canonicalizes_and_rejects_bad_values():
    assert serve._canonical_agent("cmd") == "commandcode"
    assert serve._prompt_value("hello") == "hello"
    assert serve._pane_value("%12") == "%12"
    for fn, value in ((serve._canonical_agent, "unknown"),
                      (serve._prompt_value, ""),
                      (serve._pane_value, 12)):
        try:
            fn(value)
        except ValueError:
            pass
        else:
            raise AssertionError("expected web input rejection")
    try:
        serve._prompt_value("x" * 100001)
    except ValueError:
        pass
    else:
        raise AssertionError("expected prompt size rejection")


def test_web_auth_accepts_bearer_and_rejects_missing():
    class Headers(dict):
        def get(self, key, default=""):
            return super().get(key, default)

    handler = object.__new__(serve.Handler)
    handler.auth_token = "secret"
    handler.path = "/api/board"
    handler.headers = Headers()
    errors = []
    handler._json = lambda *args, **kwargs: errors.append((args, kwargs))
    assert handler._authorized() is False
    handler.headers["Authorization"] = "Bearer secret"
    assert handler._authorized() is True
    assert errors and errors[0][1]["code"] == 401
