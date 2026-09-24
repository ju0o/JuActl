import ast
from pathlib import Path

from actl import gui


SOURCE = Path(__file__).parents[1] / "src" / "actl" / "gui.py"


def test_sidebar_line_is_korean():
    assert gui._project_sidebar_line(2, 1, 0) == "에이전트 2 · 작업 중 1 · 확인 0"


def test_attention_reason_maps_control_reasons():
    assert gui._attention_reason("pane gone") == "창이 사라졌어요"
    assert gui._attention_reason("STALE") == "오래된 정보"
    assert gui._attention_reason("other") == "확인 필요"


def test_legacy_chrome_text_is_gone():
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            if keyword.arg in {"text", "textvariable", "value"} and isinstance(keyword.value, ast.Constant):
                assert all(value not in keyword.value.value for value in ("runtime instances", "Reason:", "AGENT BOARD"))
