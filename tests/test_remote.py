import subprocess

from actl.core import remote, tmux


def test_remote_extract_does_not_treat_failed_stdout_as_result(monkeypatch):
    class Result:
        returncode = 1
        stdout = "error accidentally on stdout"
        stderr = "No response text found"

    monkeypatch.setattr(tmux, "REMOTE_SSH_TARGET", "asus")
    monkeypatch.setattr(remote.subprocess, "run", lambda *args, **kwargs: Result())
    result = remote.remote_extract("claude-team", "%0")
    assert result is not None
    assert result.text is None
    assert result.confidence == "none"
    assert "No response" in result.detail


def test_managed_discovery_is_bounded_and_retryable(monkeypatch):
    calls = []

    def fake_run(*args, **kwargs):
        if not calls:
            calls.append("socket")
            return type("Result", (), {"returncode": 0, "stdout": "/tmp/tmux/default\n", "stderr": ""})()
        calls.append(kwargs["timeout"])
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(tmux, "REMOTE_SSH_TARGET", "asus")
    monkeypatch.setattr(remote.subprocess, "run", fake_run)

    try:
        remote.remote_managed_send("codex", "%0", "prompt", timeout=45.0)
    except RuntimeError as exc:
        assert str(exc) == "DISCOVERY_TIMEOUT: retry discovery"
    else:
        raise AssertionError("managed discovery must fail closed")
    assert calls == ["socket", 10.0]
