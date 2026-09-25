from actl import serve, tui
from actl.agents.extract import CopyResult
from actl.core import validation
from actl.core.validation import TargetValidation


def test_verify_row_returns_plain_korean_next_steps(monkeypatch):
    monkeypatch.setattr(validation, "validate_target", lambda *_: TargetValidation("STALE", "%1", detail="English detail"))
    stale = tui._verify_row("codex", "%1", {})
    assert stale == "매핑: 확인 필요 — 다음 단계: 다시 매핑하세요"
    assert "STALE" not in stale and "English" not in stale

    monkeypatch.setattr(validation, "validate_target", lambda *_: TargetValidation("UP", "%1"))
    monkeypatch.setattr(tui, "extract_last_response", lambda *_: CopyResult("완료", "codex-rollout:/private", "exact", "English detail"))
    ready = tui._verify_row("codex", "%1", {})
    assert ready == "매핑: 정상 · 복사 가능 (2자) — 다음 단계: 복사하세요"
    assert "codex-rollout" not in ready


def test_verify_row_hides_exceptions_and_tui_hints(monkeypatch):
    monkeypatch.setattr(validation, "validate_target", lambda *_: (_ for _ in ()).throw(RuntimeError("raw exception")))
    text = tui._verify_row("codex", "%1", {})
    assert text == "매핑: 확인 필요 — 다음 단계: 다시 매핑하세요"
    assert "raw exception" not in text and "m 눌러" not in text


def test_web_pane_title_uses_korean_live_suffix():
    assert "— 실시간`" in serve.BOARD_HTML
    assert "— live`" not in serve.BOARD_HTML
