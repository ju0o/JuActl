"""Offline Board SEND -> RESULT -> COPY coverage with a disposable stub agent."""
from __future__ import annotations

import json
import io
from pathlib import Path

from actl import cli, serve
from actl.core import audit
from actl.core.models import CopyResult


class StubAgent:
    def __init__(self, root: Path) -> None:
        self.session = root / "tmux-disposable-board"
        self.session.mkdir()
        self.prompt = ""
        self.result = ""

    def send(self, prompt: str) -> str:
        self.prompt = prompt
        self.result = f"RESULT::{prompt}"
        (self.session / "result.txt").write_text(self.result, encoding="utf-8")
        return "disposable:0.0"

    def close(self) -> None:
        for path in self.session.glob("*"):
            path.unlink()
        self.session.rmdir()


def _call(path: str, *, data: dict | None = None) -> dict:
    handler = object.__new__(serve.Handler)
    handler.path = path
    handler.headers = {"Content-Length": str(len(json.dumps(data).encode()))} if data is not None else {}
    handler.rfile = io.BytesIO(json.dumps(data).encode() if data is not None else b"")
    handler._authorized = lambda: True
    captured: list[tuple[object, dict]] = []
    handler._json = lambda ok, payload=None, **kwargs: captured.append((ok, payload or kwargs))
    if data is None:
        serve.Handler.do_GET(handler)
    else:
        serve.Handler.do_POST(handler)
    assert captured and captured[0][0] is True
    return captured[0][1]


def test_disposable_board_send_result_copy(monkeypatch, tmp_path: Path):
    stub = StubAgent(tmp_path)
    config = {"agents": {"codex": {"target": "disposable:0.0"}}}
    monkeypatch.setattr(serve, "load_config", lambda: config)
    monkeypatch.setattr(cli, "_send_to_selected", lambda _cfg, _agent, text: stub.send(text))
    monkeypatch.setattr(
        serve,
        "extract_last_response",
        lambda _agent, _target, _config: CopyResult(stub.result, "stub-agent", "exact"),
    )
    monkeypatch.setattr(audit, "record", lambda *args, **kwargs: None)
    serve.Handler.auth_token = ""
    try:
        sent = _call("/api/send", data={"agent": "codex", "text": "WINDOWS_BOARD_E2E"})
        assert sent["target"] == "disposable:0.0"
        assert stub.prompt == "WINDOWS_BOARD_E2E"

        copied = _call("/api/copy?agent=codex")
        assert copied["text"] == "RESULT::WINDOWS_BOARD_E2E"
        assert copied["target"] == "disposable:0.0"
    finally:
        stub.close()

    assert not stub.session.exists()
