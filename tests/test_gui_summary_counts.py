from types import SimpleNamespace

from actl import gui


def test_summary_text_counts_running_rows():
    rows = [{"runtime_state": "WORKING", "activity_state": "RUNNING"}]
    assert "작업 중 1" in gui._summary_text(rows)


def test_hydration_done_updates_summary_for_working_rows():
    board = object.__new__(gui.Board)
    board.hydrating = True
    board.selected = None
    board.summary_var = SimpleNamespace(set=lambda value: setattr(board, "summary", value))
    board._render_projects = lambda: None
    board._render_cards = lambda _selected: None
    board._update_action_state = lambda: None
    board.log = lambda _message: None

    board._hydration_done([{"runtime_state": "WORKING", "activity_state": "RUNNING"}])

    assert "작업 중 1" in board.summary
    assert board.refresh_interval_ms == 3000
