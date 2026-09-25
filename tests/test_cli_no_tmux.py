from contextlib import redirect_stderr
from io import StringIO

from actl import cli
from actl.core import discovery
from actl.core.tmux import TmuxError


def test_discover_without_tmux_prints_friendly_error(monkeypatch):
    monkeypatch.setattr(discovery, "list_panes", lambda: (_ for _ in ()).throw(TmuxError("server not running")))
    stderr = StringIO()

    try:
        with redirect_stderr(stderr):
            cli.main(["actl", "discover"])
    except SystemExit as raised:
        assert raised.code == 1
    else:
        raise AssertionError("expected SystemExit")

    assert "AI 작업창(tmux)이 켜져 있지 않아요 — ASUS에서 AI를 먼저 실행한 뒤 다시 시도하세요" in stderr.getvalue()
    assert "Traceback" not in stderr.getvalue()
