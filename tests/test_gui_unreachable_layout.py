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


def _failed_board():
    board = object.__new__(gui.Board)
    board.refreshing = True
    board.rows = []
    board.selected = None
    board.ssh_target = "asus"
    board.preview = _Widget()
    board.project_counts = _Var()
    board.status_var = _Var()
    board.send_btn = _Widget()
    board.action_buttons = {name: _Widget() for name in ("SEND PROMPT", "COPY RESULT", "FOCUS")}
    board.send_inflight = False
    board._render_cards = lambda *_args: None
    board._render_projects = lambda: None
    board.set_status = lambda value: board.status_var.set(value)
    board.logw = _Widget()
    board.log = gui.Board.log.__get__(board)
    return board


def test_unreachable_message_is_plain_and_detail_is_logged():
    detail = "ssh: connect to host 10.0.0.8 port 22: Connection timed out"
    board = _failed_board()

    assert gui._state_message("unreachable", detail) == (
        "ASUS에 연결할 수 없습니다. ASUS 전원과 네트워크를 확인한 뒤 [다시 시도]를 누르세요."
    )
    board._refresh_done(RuntimeError(detail))

    assert board.preview.value == gui._state_message("unreachable")
    assert board.project_counts.value == "연결 안 됨"
    assert detail in board.logw.value


def test_unreachable_center_label_wraps_without_sidebar_message():
    source = open("src/actl/gui.py", encoding="utf-8").read()

    assert "wraplength=CENTER_WRAPLENGTH" in source
    assert 'self.project_counts.set("연결 안 됨")' in source
