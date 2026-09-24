from actl import gui


class _Var:
    def __init__(self, value=""):
        self.value = value

    def set(self, value):
        self.value = value


class _Text:
    def __init__(self, value=""):
        self.value = value

    def delete(self, *_args):
        self.value = ""

    def insert(self, _index, value):
        self.value += value


def _board():
    board = object.__new__(gui.Board)
    board.rows = [
        {"runtime_key": "a", "agent": "codex", "display": "Codex", "target": "%1"},
        {"runtime_key": "b", "agent": "claude", "display": "Claude", "target": "%2"},
    ]
    board.selected = "a"
    board.preview = _Text("A copied text")
    board.pane_title = _Var()
    board.detail_var = _Var()
    board.preview_inflight = set()
    board.last_previews = {"a": "A live text", "b": "B live text"}
    board.preview_degraded = set()
    board.preview_hold = ("a", 10**12)
    board.ssh_target = None
    board.send_inflight = False
    board.loop_phase = "READY"
    board.config = {}
    board._diagnostic_runtime_key = None
    board.set_status = lambda _value: None
    board.log = lambda _value: None
    board._bg = lambda work, done: setattr(board, "pending", (work, done))
    return board


def test_preview_hold_is_per_agent_and_selection_clears_old_body(monkeypatch):
    board = _board()
    monkeypatch.setattr(gui, "_pane_preview", lambda target, lines: f"{target} fresh")

    board._request_preview(board.rows[0])
    work, done = board.pending
    done(work())
    assert board.preview.value == "A copied text"

    board.selected = "b"
    board.on_select()

    assert board.preview.value == "Claude 화면 불러오는 중…"
    work, done = board.pending
    done(work())

    assert "A copied text" not in board.preview.value
    assert "%2 fresh" in board.preview.value

    board.selected = "a"
    board.on_select()
    assert board.preview.value == "Codex 화면 불러오는 중…"
    work, done = board.pending
    done(work())

    assert "Claude" not in board.preview.value
    assert "%1 fresh" in board.preview.value
