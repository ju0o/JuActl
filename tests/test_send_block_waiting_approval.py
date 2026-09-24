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
    monkeypatch.setattr(activity, "observe_activity", lambda _: ("WAITING_INPUT", APPROVAL))
    board.on_send()
    assert "승인을 기다리고 있어요" in board.notice[0]
