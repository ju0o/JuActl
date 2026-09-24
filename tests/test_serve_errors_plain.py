from io import BytesIO

from actl import cli, serve
from actl.core.runtime import WriterDenied


class Headers(dict):
    def get(self, key, default=""):
        return super().get(key, default)


def _handler(path, *, method="GET", body=b""):
    handler = object.__new__(serve.Handler)
    handler.auth_token = ""
    handler.path = path
    handler.headers = Headers({"Content-Length": str(len(body))})
    handler.rfile = BytesIO(body)
    errors = []
    handler._json = lambda *args, **kwargs: errors.append(kwargs)
    getattr(handler, f"do_{method}")()
    return errors[0]


def test_web_error_bodies_are_plain_korean(monkeypatch):
    get_unknown = _handler("/missing")
    post_invalid = _handler("/api/send", method="POST", body=b"{")
    monkeypatch.setattr(serve, "_board_data", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    get_server = _handler("/api/board")
    post_server = _handler("/api/refresh", method="POST")

    bodies = [error["error"] for error in (get_unknown, post_invalid, get_server, post_server)]
    assert all("RuntimeError" not in body for body in bodies)
    assert all("unknown endpoint" not in body for body in bodies)
    assert all("invalid JSON" not in body for body in bodies)
    assert all("HTTP" not in body for body in bodies)
    assert get_unknown["code"] == 404
    assert post_invalid["code"] == 400
    assert get_server["code"] == post_server["code"] == 500


def test_web_fetch_uses_plain_connection_fallback():
    assert '"연결이 끊겼어요 — 새로고침해 주세요"' in serve.BOARD_HTML
    assert '"HTTP "+r.status' not in serve.BOARD_HTML


def test_web_send_keeps_approval_reason(monkeypatch):
    reason = "Codex가 승인을 기다리고 있어요. 보내면 승인 창에 들어가요 — ASUS 화면에서 직접 확인하세요"
    monkeypatch.setattr(cli, "_send_to_selected", lambda *_args: (_ for _ in ()).throw(RuntimeError(reason)))

    response = _handler("/api/send", method="POST", body=b'{"agent":"codex","text":"hello"}')

    assert response["error"] == reason
    assert "RuntimeError" not in response["error"]


def test_web_send_hides_writer_denied_details(monkeypatch):
    monkeypatch.setattr(cli, "_send_to_selected", lambda *_args: (_ for _ in ()).throw(WriterDenied("BUSY", "lease held by another sender")))

    response = _handler("/api/send", method="POST", body=b'{"agent":"codex","text":"hello"}')

    assert response["error"] == "다른 곳에서 보내는 중이에요 — 잠시 후 다시 보내 주세요"
    assert "BUSY" not in response["error"]
    assert "WriterDenied" not in response["error"]
    assert "lease held" not in response["error"]


def test_web_send_hides_selected_runtime_details(monkeypatch):
    monkeypatch.setattr(
        cli,
        "_send_to_selected",
        lambda *_args: (_ for _ in ()).throw(
            ValueError("Selected runtime is STALE; sending blocked: pane disappeared")
        ),
    )

    response = _handler("/api/send", method="POST", body=b'{"agent":"codex","text":"hello"}')

    assert response["error"] == "에이전트 화면을 찾지 못했어요 — 새로고침 후 다시 시도해 주세요"
    assert "Selected runtime" not in response["error"]
    assert "ValueError" not in response["error"]
