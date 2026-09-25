import queue
from types import SimpleNamespace

from actl import gui


class _Message:
    def __init__(self, value):
        self.value = value

    def get(self, *_args):
        return self.value

    def delete(self, *_args):
        self.value = ""

    def insert(self, _index, value):
        self.value += value

    def configure(self, **_kwargs):
        pass

    def focus_get(self):
        return None


def test_staged_send_drains_submitted_ui_callback_on_main_queue(monkeypatch):
    board = object.__new__(gui.Board)
    row = {
        "runtime_key": "rk",
        "agent": "codex",
        "display": "Codex",
        "target": "%1",
        "control_ready": True,
        "activity_state": "IDLE",
    }
    board.rows = [row]
    board.selected = "rk"
    board.current = lambda: row
    board.msg = _Message("payload")
    board.root = SimpleNamespace(after=lambda *_args: None)
    board.jobs = queue.Queue()
    board.ssh_target = None
    board.send_inflight = False
    board._busy_wait_token = 0
    board._result_ready_keys = set()
    board._post_send_running_keys = set()
    board.loop_phase = "READY"
    board.last_submitted_prompt = None
    board.log = lambda _text: None
    board.notify = lambda *_args: None
    board.set_status = lambda _text: None
    board._update_action_state = lambda: None
    board._cancel_busy_wait = lambda: None
    board._hide_busy_confirm = lambda: None
    board._start_follow_preview = lambda *_args, **_kwargs: None

    cleared = []
    clear = board._clear_prompt_at_submitted
    board._clear_prompt_at_submitted = lambda text: (cleared.append(text), clear(text))[1]

    from actl.core import tmux

    monkeypatch.setattr(
        tmux,
        "send_prompt_staged",
        lambda *_args, **_kwargs: {
            "ok": True,
            "completedStages": ["paste_buffer", "enter"],
            "deliveryDisposition": "TRANSPORT_SENT",
        },
    )
    board._bg = lambda work, done: done(work())

    board.on_send(busy_choice="now")
    assert cleared == []

    board._drain()

    assert cleared == ["payload"]
    assert board.msg.value == gui.PROMPT_PLACEHOLDER
