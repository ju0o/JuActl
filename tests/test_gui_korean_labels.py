import ast
import inspect
from pathlib import Path


SOURCE_PATH = Path(__file__).parents[1] / "src/actl/gui.py"
SOURCE = SOURCE_PATH.read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)


def _tk_text_literals():
    values = []
    for node in ast.walk(TREE):
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            if keyword.arg in {"text", "textvariable", "value"} and isinstance(keyword.value, ast.Constant):
                values.append(keyword.value.value)
    return values


def test_founder_facing_labels_are_korean():
    labels = _tk_text_literals()
    for label in (
        "PROJECTS", "ALL PROJECTS", "NEEDS ATTENTION", "LIVE RUNTIME INSTANCES",
        "RUNTIME INSPECTOR — select a runtime", "SEND PROMPT", "COPY RESULT",
        "FOCUS", "PANE BOARD", "▸ diagnostics", "▸ 로그", "▾ 로그",
        "COLLECT RESULT", "◉ 이벤트 감시 ON (health 60s)",
    ):
        assert label not in labels


def test_prompt_has_one_send_button_and_five_lines():
    assert SOURCE.count('text="보내기"') == 1
    assert 'PROMPT_PLACEHOLDER = "에이전트에게 보낼 내용 (Ctrl+Enter로 보내기)"' in SOURCE
    assert "ScrolledText(right, height=5" in SOURCE


def test_placeholder_cannot_be_restored_while_focused_or_submitted():
    from actl.gui import Board

    restore = inspect.getsource(Board._restore_prompt_placeholder)
    clear = inspect.getsource(Board._clear_prompt_at_submitted)
    send = inspect.getsource(Board.on_send)
    assert "self.msg.focus_get() is self.msg" in restore
    assert "self._restore_prompt_placeholder()" in clear
    assert "PROMPT_PLACEHOLDER in text" in send
