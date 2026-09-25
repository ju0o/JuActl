"""Dogfood-06: Founder UX closeout — prompt clear, live preview retain, loop phases."""
from __future__ import annotations

from actl.gui import _preview_text
from actl.core import send_truth
from actl.core import tmux


def test_preview_timeout_keeps_last_good_content():
    previous = "Codex> waiting\nACTL_V2_QA06_MARKER"
    text, fresh = _preview_text(
        previous, "(미리보기 불가: remote tmux command timed out after 10s)"
    )
    assert fresh is False
    assert "ACTL_V2_QA06_MARKER" in text
    assert "timed out after 10s" not in text


def test_preview_timeout_without_previous_is_soft_placeholder():
    text, fresh = _preview_text(None, "remote tmux command timed out after 10s")
    assert fresh is False
    assert "timed out after 10s" not in text
    assert "읽는 중" in text or "갱신" in text


def test_fresh_preview_replaces_content():
    text, fresh = _preview_text("old", "new pane line\nsecond")
    assert fresh is True
    assert text == "new pane line\nsecond"


def test_founder_loop_labels_exist():
    assert send_truth.LOOP_STATE_KO[send_truth.LOOP_READY] == "준비"
    assert send_truth.LOOP_STATE_KO[send_truth.LOOP_SUBMITTED] == "제출 완료"
    assert send_truth.LOOP_STATE_KO[send_truth.LOOP_WORKING] == "작업 중"
    assert send_truth.LOOP_STATE_KO[send_truth.LOOP_RESULT_READY] == "결과 준비됨"
    assert send_truth.LOOP_STATE_KO[send_truth.LOOP_COPIED] == "복사 완료"
    assert send_truth.SEND_STATE_KO[send_truth.START_ACKNOWLEDGED] == "작업 중"


def test_prompt_clear_helper_contract():
    """Board clears prompt only via _clear_prompt_at_submitted at SUBMITTED."""
    import inspect
    from actl import gui as gui_mod

    src = inspect.getsource(gui_mod.Board.on_send)
    assert "_clear_prompt_at_submitted" in src
    assert "on_committed" in src
    # Must not rely solely on done() msg.delete for success path clarity:
    # clear happens at commit callback.
    assert "mark_submitted" in src or "_clear_prompt_at_submitted(text)" in src


def test_capture_pane_live_exists_and_is_separate_from_control_mode():
    assert hasattr(tmux, "capture_pane_live")
    src = inspect_source = open(tmux.__file__, encoding="utf-8").read()
    assert "def capture_pane_live" in src
    # Live capture must use one-shot remote args path, not executeTmux exclusive.
    body = src.split("def capture_pane_live", 1)[1].split("\ndef list_panes", 1)[0]
    assert "executeTmux" not in body
    assert "_remote_args" in body
    assert "timeout" in body


def test_request_preview_does_not_submit_to_scheduler_when_remote():
    import inspect
    from actl import gui as gui_mod

    src = inspect.getsource(gui_mod.Board._request_preview)
    assert "scheduler_for" not in src or "do NOT" in src.lower() or "oneshot" in src.lower()
    # Stronger: no .submit( for preview in this method
    assert ".submit(" not in src
