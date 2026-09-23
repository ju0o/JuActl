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


def test_managed_socket_probe_timeout_is_normalized(monkeypatch):
    monkeypatch.setattr(tmux, "REMOTE_SSH_TARGET", "asus")
    monkeypatch.setattr(
        remote.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(subprocess.TimeoutExpired(args[0], kwargs["timeout"])),
    )
    try:
        remote.remote_managed_send("codex", "%0", "prompt")
    except RuntimeError as exc:
        assert str(exc) == "MANAGED_SOCKET_PROBE_TIMEOUT: retry discovery"
    else:
        raise AssertionError("socket probe timeout must fail closed")


def test_managed_socket_probe_bad_result_is_normalized(monkeypatch):
    monkeypatch.setattr(tmux, "REMOTE_SSH_TARGET", "asus")
    for result in (
        type("Result", (), {"returncode": 1, "stdout": "/tmp/tmux/default\n"})(),
        type("Result", (), {"returncode": 0, "stdout": "tmux/default\n"})(),
    ):
        monkeypatch.setattr(remote.subprocess, "run", lambda *args, result=result, **kwargs: result)
        try:
            remote.remote_managed_send("codex", "%0", "prompt")
        except RuntimeError as exc:
            assert str(exc) == "MANAGED_SOCKET_PROBE_FAILED: retry discovery"
        else:
            raise AssertionError("bad socket probe result must fail closed")


def test_malformed_runtime_error_is_normalized(monkeypatch):
    for error in ({}, {"code": "BROKEN"}, {"detail": "broken"}, {"code": 1, "detail": "broken"}):
        class Result:
            returncode = 1
            stdout = json.dumps({"ok": False, "error": error})
            stderr = ""

        monkeypatch.setattr(remote.subprocess, "run", lambda *args, **kwargs: Result())
        try:
            remote._runtime_request("asus", "discover", {})
        except RuntimeError as exc:
            assert str(exc) == "managed runtime invalid response: malformed error"
        else:
            raise AssertionError("malformed runtime error must fail closed")


def test_managed_send_rejects_malformed_nested_runtime_data(monkeypatch):
    monkeypatch.setattr(tmux, "REMOTE_SSH_TARGET", "asus")
    monkeypatch.setattr(
        remote.subprocess,
        "run",
        lambda *args, **kwargs: type("Result", (), {"returncode": 0, "stdout": "/tmp/tmux/default\n"})(),
    )
    for candidates in (None, ["x"], {}):
        monkeypatch.setattr(
            remote,
            "_runtime_request",
            lambda *args, candidates=candidates, **kwargs: {
                "ok": True,
                "data": {"candidates": candidates},
            },
        )
        try:
            remote.remote_managed_send("codex", "%0", "prompt")
        except RuntimeError as exc:
            assert str(exc) == "managed runtime invalid response: discover candidates is not an array of objects"
        else:
            raise AssertionError("malformed discover candidates must fail closed")


def test_managed_send_rejects_null_send_command(monkeypatch):
    monkeypatch.setattr(tmux, "REMOTE_SSH_TARGET", "asus")
    monkeypatch.setattr(
        remote.subprocess,
        "run",
        lambda *args, **kwargs: type("Result", (), {"returncode": 0, "stdout": "/tmp/tmux/default\n"})(),
    )

    def request(_target, operation, body, timeout=30.0):
        if operation == "discover":
            return {"ok": True, "data": {"candidates": [{
                "runtimeId": "rt-1", "agentKind": "codex", "issuable": True,
                "processState": "UP", "capabilities": {"managed.send": True},
                "identityEvidence": {"paneId": "%0"},
            }]}}
        if operation == "reserve":
            return {"ok": True, "observedAt": "now", "data": {
                "runtimeId": "rt-1", "reservationId": "rsv-1", "leaseToken": "tok-1",
                "fence": "1", "context": {}, "currentSnapshotHash": "snap-1",
            }}
        if operation == "send":
            return {"ok": True, "data": {"command": None}}
        raise AssertionError(operation)

    monkeypatch.setattr(remote, "_runtime_request", request)
    try:
        remote.remote_managed_send("codex", "%0", "prompt")
    except RuntimeError as exc:
        assert str(exc) == "managed runtime invalid response: send command is not an object"
    else:
        raise AssertionError("null send command must fail closed")


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
