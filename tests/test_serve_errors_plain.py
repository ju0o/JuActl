from io import BytesIO

from actl import serve


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
