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
