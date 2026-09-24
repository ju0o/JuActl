from types import SimpleNamespace

from actl import gui


class _Var:
    def __init__(self):
        self.value = ""

    def set(self, value):
        self.value = value


class _Widget:
    def __init__(self):
        self.value = ""
        self.states = []

    def configure(self, **kwargs):
        if "state" in kwargs:
            self.states.append(kwargs["state"])

    def delete(self, *_args):
        self.value = ""

    def insert(self, _index, value):
        self.value += value


def _board():
    board = object.__new__(gui.Board)
    board.refreshing = True
    board.rows = []
    board.selected = None
    board.ssh_target = "asus"
    board.auto_var = _Var()
    board.previous_rows = {}
    board.auto_refresh = True
    board.preview = _Widget()
    board.project_counts = _Var()
    board.summary_var = _Var()
    board.status_var = _Var()
    board.send_btn = _Widget()
    board.action_buttons = {name: _Widget() for name in ("SEND PROMPT", "COPY RESULT", "FOCUS")}
    board.send_inflight = False
    board.current = lambda: None
    board._render_cards = lambda *_args: None
    board._render_projects = lambda: None
    board._start_hydration = lambda: None
    board.set_status = lambda value: board.status_var.set(value)
    board.log = lambda _message: None
    return board


def test_state_messages_are_exact_and_unreachable_includes_detail():
    assert gui._state_message("loading") == "ASUS에서 에이전트를 찾는 중…"
    assert gui._state_message("empty") == "ASUS에서 실행 중인 에이전트가 없습니다. ASUS tmux에서 에이전트를 시작하면 자동으로 나타납니다."
    assert gui._state_message("unreachable", "timeout") == (
        "ASUS에 연결할 수 없습니다. ASUS 전원과 네트워크를 확인한 뒤 [다시 시도]를 누르세요. (timeout)"
    )


def test_refresh_failure_shows_error_and_disables_actions():
    board = _board()

    board._refresh_done(RuntimeError("timeout"))

    assert board.status_var.value == "ASUS 연결 안 됨"
    assert "timeout" in board.preview.value
    assert board.project_counts.value == board.preview.value
    assert all(widget.states[-1] == "disabled" for widget in board.action_buttons.values())


def test_empty_refresh_shows_empty_state_and_disables_actions():
    board = _board()

    board._refresh_done({"rows": [], "detections": []})

    assert board.status_var.value == "ASUS 연결됨 · 에이전트 0"
    assert board.preview.value == gui._state_message("empty")
    assert board.project_counts.value == gui._state_message("empty")
    assert all(widget.states[-1] == "disabled" for widget in board.action_buttons.values())
