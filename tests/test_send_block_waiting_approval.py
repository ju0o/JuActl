import io
import sys
import types

from actl import cli, gui
from actl.core import activity
from actl.core.validation import TargetValidation


APPROVAL = "Would you like to run `ls`?\n1. Yes, proceed\nPress enter to confirm"


def test_shared_reason_detects_approval_prompt(monkeypatch):
    monkeypatch.setattr(activity, "observe_activity", lambda _: ("WAITING_INPUT", APPROVAL))
    assert "Codex" in activity.send_blocked_reason("Codex")


def test_cli_send_sends_no_keys_when_approval_is_waiting(monkeypatch):
    sent = []
    monkeypatch.setattr(cli, "validate_target", lambda *_: TargetValidation("UP", "%1", pane_id="%1"))
    monkeypatch.setattr(cli, "send_prompt", lambda *args: sent.append(args))
    monkeypatch.setattr(activity, "observe_activity", lambda _: ("WAITING_INPUT", APPROVAL))
    try:
        cli._send_to_selected({}, "codex", "hello", target="%1")
    except RuntimeError as exc:
        assert "승인을 기다리고 있어요" in str(exc)
    else:
        raise AssertionError("approval prompt must block CLI send")
    assert sent == []


def test_gui_send_sends_no_keys_when_approval_is_waiting(monkeypatch):
    tkinter = types.ModuleType("tkinter")
    tkinter.messagebox = types.SimpleNamespace(askyesno=lambda *_: True)
    monkeypatch.setattr(sys, "modules", {**sys.modules, "tkinter": tkinter})
    board = gui.Board.__new__(gui.Board)
    board.current = lambda: {"control_ready": True, "target": "%1", "display": "Codex", "runtime_key": "%1"}
    board.send_inflight = False
    board.msg = type("Message", (), {"get": lambda *_: "hello"})()
    board.notify = lambda message, level: setattr(board, "notice", (message, level))
    board._bg = lambda *args: (_ for _ in ()).throw(AssertionError("send"))
    monkeypatch.setattr(activity, "observe_activity", lambda _: ("WAITING_INPUT", APPROVAL))
    board.on_send()
    assert "승인을 기다리고 있어요" in board.notice[0]


def test_tui_send_sends_no_keys_when_approval_is_waiting(monkeypatch):
    import actl.tui as tui

    sent = []
    monkeypatch.setattr(tui, "_render", lambda *args: None)
    monkeypatch.setattr(tui, "_rows", lambda *_: [{
        "target": "%1", "control_ready": True, "display": "Codex", "agent": "codex",
    }])
    monkeypatch.setattr(tui, "load_config", lambda: {})
    monkeypatch.setattr(cli, "_send_to_selected", lambda *args: sent.append(args))
    monkeypatch.setattr(activity, "observe_activity", lambda _: ("WAITING_INPUT", APPROVAL))
    class Input(io.StringIO):
        def isatty(self): return True
        def fileno(self): return 0

    class FakeCbreak:
        def __init__(self): self.keys = iter(("s", "q"))
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def read_key(self, timeout=None): return next(self.keys)
        def raw(self): pass
        def restore(self): pass

    monkeypatch.setattr(tui.sys, "stdin", Input("hello\n::send\n"))
    monkeypatch.setattr(tui, "_Cbreak", lambda _fd: FakeCbreak())
    tui.run_tui()
    assert sent == []


def test_cli_no_target_remote_send_checks_waiting_approval(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "_resolve_live_target", lambda *_: "%1")
    monkeypatch.setattr(cli, "send_prompt", lambda *args: calls.append(args))
    monkeypatch.setattr(activity, "observe_activity", lambda _: ("WAITING_INPUT", APPROVAL))
    from actl.core import remote, tmux

    monkeypatch.setattr(tmux, "REMOTE_SSH_TARGET", "asus")
    monkeypatch.setattr(remote, "remote_send", lambda *args: calls.append(args))
    try:
        cli._send_to_selected({}, "codex", "hello")
    except RuntimeError as exc:
        assert "승인을 기다리고 있어요" in str(exc)
    else:
        raise AssertionError("remote approval prompt must block CLI send")
    assert calls == []
