from actl.core import activity, tmux


class _Proc:
    returncode = 0

    def __init__(self, stdout):
        self.stdout = stdout


def test_idle_uses_cpu_delta_not_lifetime_percent(monkeypatch):
    activity._CPU_SAMPLES.clear()
    outputs = iter(["00:00:20\n", "00:00:20\n"])
    clocks = iter([10.0, 11.0])
    monkeypatch.setattr(activity.subprocess, "run", lambda *args, **kwargs: _Proc(next(outputs)))
    monkeypatch.setattr(activity.time, "monotonic", lambda: next(clocks))
    monkeypatch.setattr(tmux, "pane_field", lambda *args: "123")
    monkeypatch.setattr(tmux, "capture_pane", lambda *args, **kwargs: "ready ❯")

    assert activity.observe_activity("%1") == ("IDLE", "cpu=0.0")
    assert activity.observe_activity("%1") == ("IDLE", "cpu=0.0")


def test_cpu_delta_marks_running(monkeypatch):
    activity._CPU_SAMPLES.clear()
    outputs = iter(["00:00:01\n", "00:00:02\n"])
    clocks = iter([20.0, 21.0])
    monkeypatch.setattr(activity.subprocess, "run", lambda *args, **kwargs: _Proc(next(outputs)))
    monkeypatch.setattr(activity.time, "monotonic", lambda: next(clocks))
    monkeypatch.setattr(tmux, "pane_field", lambda *args: "123")
    monkeypatch.setattr(tmux, "capture_pane", lambda *args, **kwargs: "ready ❯")

    activity.observe_activity("%2")
    assert activity.observe_activity("%2") == ("RUNNING", "cpu=100.0")
