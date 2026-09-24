import ast
from pathlib import Path

from actl.gui import _pane_action_failure_text


GUI = Path(__file__).parents[1] / "src" / "actl" / "gui.py"


def test_pane_action_failure_text_is_plain_and_actionable():
    next_step = "새로고침 후 다시 시도해 주세요"
    for action in ("rename", "session_create", "window_create", "pane_split", "pane_move"):
        text = _pane_action_failure_text(action)
        assert next_step in text
        assert "실패" not in text
        assert "Exception" not in text


def test_pane_board_showerror_does_not_expose_tmux_detail():
    tree = ast.parse(GUI.read_text(encoding="utf-8"))
    board = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Board")
    on_board = next(node for node in board.body if isinstance(node, ast.FunctionDef) and node.name == "on_board")
    calls = [
        node
        for node in ast.walk(on_board)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "showerror"
    ]
    assert len(calls) == 6
    assert sum(
        len(call.args) >= 2
        and isinstance(call.args[1], ast.Call)
        and isinstance(call.args[1].func, ast.Name)
        and call.args[1].func.id == "_pane_action_failure_text"
        for call in calls
    ) == 5
    assert sum(
        len(call.args) >= 2
        and isinstance(call.args[1], ast.Call)
        and isinstance(call.args[1].func, ast.Name)
        and call.args[1].func.id == "str"
        for call in calls
    ) == 1
