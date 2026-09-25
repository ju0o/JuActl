from actl import tui
from actl.core import tmux


def test_pane_preview_drops_trailing_blank_rows_before_taking_tail(monkeypatch):
    monkeypatch.setattr(tmux, "REMOTE_SSH_TARGET", None)
    monkeypatch.setattr(tmux, "capture_pane", lambda *_args, **_kwargs: "one\ntwo\nthree\n" + "\n" * 30)

    assert tui._pane_preview("%1", lines=12) == "one\ntwo\nthree"
