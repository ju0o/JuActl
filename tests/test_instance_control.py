from actl.cli import _send_to_selected
from actl.core.validation import TargetValidation
import actl.cli as cli
import actl.core.remote as remote


def test_selected_runtime_send_targets_only_selected_pane(monkeypatch):
    sent = []
    monkeypatch.setattr(cli, "validate_target",
                        lambda agent, target: TargetValidation("UP", target, pane_id=target))
    monkeypatch.setattr(cli, "send_prompt", lambda target, prompt: sent.append((target, prompt)))

    _send_to_selected({}, "codex", "one", target="%3")
    _send_to_selected({}, "codex", "two", target="%4")

    assert sent == [("%3", "one"), ("%4", "two")]


def test_selected_stale_runtime_fails_closed_before_send(monkeypatch):
    sent = []
    monkeypatch.setattr(cli, "validate_target",
                        lambda agent, target: TargetValidation("MISMATCH", target, detail="pid changed"))
    monkeypatch.setattr(cli, "send_prompt", lambda target, prompt: sent.append((target, prompt)))

    try:
        _send_to_selected({}, "codex", "blocked", target="%3")
    except ValueError as exc:
        assert "MISMATCH" in str(exc)
    else:
        raise AssertionError("stale selected runtime must fail closed")
    assert sent == []


def test_selected_remote_send_uses_managed_contract_when_available(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "validate_target",
                        lambda agent, target: TargetValidation("UP", target, pane_id=target))
    monkeypatch.setattr(cli, "send_prompt", lambda *_: (_ for _ in ()).throw(AssertionError("direct path")))
    monkeypatch.setattr(remote, "is_remote", lambda: True)
    monkeypatch.setattr(remote, "remote_managed_send",
                        lambda agent, target, prompt: calls.append((agent, target, prompt)) or target)
    result = cli._send_to_selected({}, "codex", "managed", target="%3")
    assert result == "%3"
    assert calls == [("codex", "%3", "managed")]
