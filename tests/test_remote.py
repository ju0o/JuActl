import subprocess
import json

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


def test_managed_socket_probe_failures_are_normalized(monkeypatch):
    monkeypatch.setattr(tmux, "REMOTE_SSH_TARGET", "asus")

    def fail(*args, **kwargs):
        raise OSError("connection reset")

    monkeypatch.setattr(remote.subprocess, "run", fail)
    try:
        remote.remote_managed_send("codex", "%0", "prompt")
    except RuntimeError as exc:
        assert str(exc) == "MANAGED_SOCKET_PROBE_FAILED: retry discovery"
    else:
        raise AssertionError("socket probe failure must fail closed")


def test_malformed_runtime_reply_is_normalized(monkeypatch):
    class Result:
        returncode = 0
        stdout = json.dumps([])
        stderr = ""

    monkeypatch.setattr(remote.subprocess, "run", lambda *args, **kwargs: Result())
    try:
        remote._runtime_request("asus", "discover", {})
    except RuntimeError as exc:
        assert str(exc) == "managed runtime invalid response: malformed envelope"
    else:
        raise AssertionError("malformed runtime envelope must fail closed")


def test_malformed_runtime_data_is_normalized(monkeypatch):
    class Result:
        returncode = 0
        stdout = json.dumps({"ok": True, "data": []})
        stderr = ""

    monkeypatch.setattr(remote.subprocess, "run", lambda *args, **kwargs: Result())
    try:
        remote._runtime_data(remote._runtime_request("asus", "discover", {}), "discover")
    except RuntimeError as exc:
        assert str(exc) == "managed runtime invalid response: discover data is not an object"
    else:
        raise AssertionError("malformed runtime data must fail closed")
