from types import SimpleNamespace

from actl import gui
from actl.core import send_truth


class _Var:
    def __init__(self):
        self.value = ""

    def set(self, value):
        self.value = value


class _Widget:
    def __init__(self):
        self.value = ""

    def configure(self, **kwargs):
        self.options = getattr(self, "options", {})
        self.options.update(kwargs)
        self.value = kwargs.get("fg", self.value)

    def delete(self, *_args):
        self.value = ""

    def insert(self, _index, value):
        self.value += value


def _board():
    board = object.__new__(gui.Board)
    board.notice_var = _Var()
    board.status_var = _Var()
    board.notice_label = _Widget()
    board.log = lambda _text: None
    board.config = {}
    board.ssh_target = None
    board.send_inflight = False
    board.preview_hold_until = 0.0
    board.last_previews = {}
    board.preview = _Widget()
    board.root = SimpleNamespace(after=lambda _delay, fn: fn())
    board.rows = [{"runtime_key": "rk", "agent": "codex", "display": "Codex",
                   "target": "%1", "control_ready": True}]
    board.selected = "rk"
    board.current = lambda: board.rows[0]
    board.set_status = lambda value: board.status_var.set(value)
    board._set_loop_phase = lambda _phase, status=None: board.set_status(status or _phase)
    board._bg = lambda work, done: done(work())
    board.send_btn = _Widget()
    board.action_buttons = {name: _Widget() for name in ("SEND PROMPT", "COPY RESULT", "FOCUS")}
    board._render_cards = lambda _selected=None: None
    board._result_ready_keys = set()
    board._post_send_running_keys = set()
    board._copied_result_keys = set()
    return board


def test_notify_updates_notice_status_color_and_log():
    board = _board()
    logged = []
    board.log = logged.append

    board.notify("전송 실패: timeout", "bad")

    assert board.notice_var.value == "전송 실패: timeout"
    assert board.status_var.value == "전송 실패: timeout"
    assert board.notice_label.value == gui.BAD
    assert logged == ["전송 실패: timeout"]


def test_on_copy_done_paths_are_visible(monkeypatch):
    cases = [
        (send_truth.NEW_RESULT, "answer", "복사 완료 6자"),
        (send_truth.RESULT_PENDING, "working", "아직 새 결과가 없습니다 — 작업이 끝나면 다시 눌러 주세요"),
        (send_truth.STALE_RESULT, "old", "이전 결과와 같아서 복사하지 않았습니다"),
        (send_truth.NO_RESULT, "", "아직 새 결과가 없습니다 — 작업이 끝나면 다시 눌러 주세요"),
    ]
    for result_class, text, expected in cases:
        board = _board()
        monkeypatch.setattr(gui, "extract_last_response", lambda *_args: SimpleNamespace(
            text=text, source="stub", confidence="high", detail="none"
        ))
        corr = None if result_class == send_truth.NEW_RESULT else SimpleNamespace(result_hash_after=None)
        monkeypatch.setattr(send_truth, "classify_result", lambda *_args, **_kwargs: (result_class, corr))
        monkeypatch.setattr(gui, "copy_text", lambda *_args, **_kwargs: "stub")
        board.on_copy()
        assert board.notice_var.value == expected


def test_on_copy_clipboard_failure_is_visible(monkeypatch):
    board = _board()
    monkeypatch.setattr(gui, "extract_last_response", lambda *_args: SimpleNamespace(
        text="answer", source="stub", confidence="high", detail="none"
    ))
    monkeypatch.setattr(send_truth, "classify_result", lambda *_args, **_kwargs: (send_truth.NEW_RESULT, None))
    monkeypatch.setattr(gui, "copy_text", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("no clipboard")))

    board.on_copy()

    assert board.notice_var.value == "클립보드 복사 실패"


def test_result_ready_waits_for_real_post_send_change(monkeypatch):
    send_truth.reset_all_correlations()
    try:
        send_truth.begin_send("codex", "%1", previous_result_hash="old")
        send_truth.set_send_state("codex", "%1", send_truth.SUBMITTED)
        board = _board()
        unchanged = [{"runtime_key": "rk", "agent": "codex", "target": "%1",
                      "activity_state": "RUNNING", "result_hash": "old"}]
        board._update_result_ready(unchanged, {})
        assert "rk" not in board._result_ready_keys
        assert not gui._card_line(unchanged[0]).startswith("답이 왔어요")

        rows = [{"runtime_key": "rk", "agent": "codex", "target": "%1",
                 "activity_state": "IDLE", "result_hash": "new"}]
        board._update_result_ready(rows, {"rk": {"activity_state": "IDLE", "result_hash": "old"}})
        assert rows[0]["_result_ready"]
        assert gui._card_line(rows[0]).startswith("답이 왔어요")
        board.rows = rows
        board.hydrating = True
        board.previous_rows = {"rk": {"activity_state": "IDLE", "result_hash": "old"}}
        board.summary_var = _Var()
        board._render_projects = lambda: None
        board._render_cards = lambda _selected=None: None
        board.pane_title = _Var()
        board.detail_var = _Var()
        board._diagnostic_runtime_key = None
        board._request_preview = lambda *_args, **_kwargs: None
        board._bg = lambda work, done: done(work())
        board._hydration_done(rows)
        assert board.status_var.value == "답이 왔어요"

        copied = _board()
        copied._copied_result_keys.add(("codex", "%1", send_truth.result_hash("answer")))
        monkeypatch.setattr(gui, "extract_last_response", lambda *_args: SimpleNamespace(
            text="answer", source="stub", confidence="high", detail="none"
        ))
        monkeypatch.setattr(send_truth, "classify_result", lambda *_args, **_kwargs: (
            send_truth.STALE_RESULT, SimpleNamespace(result_hash_after=send_truth.result_hash("answer"))
        ))
        copied.on_copy()
        assert copied.notice_var.value == "이미 복사한 결과예요"
    finally:
        send_truth.reset_all_correlations()


def test_request_preview_does_not_rewrite_during_copy_hold(monkeypatch):
    board = _board()
    board.preview.value = "복사 완료 6자"
    board.preview_hold_until = 10**12
    board.preview_inflight = set()
    board.preview_degraded = set()
    board.pane_title = _Var()
    board.detail_var = _Var()
    monkeypatch.setattr(gui, "_pane_preview", lambda *_args, **_kwargs: "live overwrite")

    board._request_preview(board.rows[0])

    assert board.preview.value == "복사 완료 6자"
