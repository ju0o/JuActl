import io
from contextlib import redirect_stdout

from actl import cli
from actl.core import activity, tmux
from actl.core.models import CopyResult


class _Proc:
    returncode = 0

    def __init__(self, stdout):
        self.stdout = stdout


def _samples(monkeypatch, outputs, clocks, pane_text="ready ❯"):
    activity._CPU_SAMPLES.clear()
    monkeypatch.setattr(activity.subprocess, "run", lambda *args, **kwargs: _Proc(next(outputs)))
    monkeypatch.setattr(activity.time, "monotonic", lambda: next(clocks))
    monkeypatch.setattr(activity.time, "sleep", lambda _: None)
    monkeypatch.setattr(tmux, "pane_field", lambda *args: "123")
    monkeypatch.setattr(tmux, "capture_pane", lambda *args, **kwargs: pane_text)


def test_stale_high_cpu_with_prompt_is_idle(monkeypatch):
    _samples(monkeypatch, iter(["00:00:01\n", "00:00:02\n", "00:00:02\n", "00:00:03\n"]), iter([0.0, 0.4, 2.4, 2.8]))
    activity.observe_activity("%1")
    assert activity.observe_activity("%1") == ("IDLE", "cpu=250.0")


def test_first_sample_uses_fresh_delta(monkeypatch):
    _samples(monkeypatch, iter(["00:00:01\n", "00:00:02\n"]), iter([0.0, 0.4]), "working...")
    assert activity.observe_activity("%2") == ("RUNNING", "cpu=250.0")


def test_status_label_and_copy_detail(monkeypatch):
    assert cli._activity_label("WAITING_INPUT") == "승인 기다림"
    monkeypatch.setattr(cli, "AGENTS", {"codex": cli.AGENTS["codex"]})
    monkeypatch.setattr(cli, "agent_status", lambda *_: {
        "agent": "Codex", "target": "-", "pane": "UNMAPPED", "command": "-", "path": "-",
        "activity": "UNKNOWN",
    })
    out = io.StringIO()
    with redirect_stdout(out):
        cli._print_status({}, "codex")
    assert "연결 안 됨" in out.getvalue()

    monkeypatch.setattr(cli, "_resolve_live_target", lambda *_: "%0")
    monkeypatch.setattr(cli, "extract_last_response", lambda *_: CopyResult(None, "adapter", "none", "adapter detail"))
    out = io.StringIO()
    with redirect_stdout(out):
        cli._copy({}, "codex")
    assert "아직 새 답이 없어요" in out.getvalue()
    assert "adapter detail" in out.getvalue()
