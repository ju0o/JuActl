from actl.core import activity
from actl.core.activity import classify_activity
from actl import gui, tui
from actl.core.projection import runtime_state


def test_codex_approval_prompt_is_waiting_input_and_blocked():
    pane = "\n".join([
        "Working on the request",
        "Would you like to run this command?",
        "Allow this action (y/n)",
    ])
    assert classify_activity(0.0, pane) == "WAITING_INPUT"
    assert runtime_state("WAITING_INPUT") == "BLOCKED"


def test_approval_label_is_visible_on_gui_card():
    assert gui._card_line({"activity_state": "WAITING_INPUT", "project": "juactl"}).startswith("승인 기다림")


def test_approval_label_is_visible_in_tui(monkeypatch):
    monkeypatch.setattr(activity, "observe_activity", lambda _: ("WAITING_INPUT", "prompt"))
    assert tui._pane_busy("%1") == "승인 기다림"
