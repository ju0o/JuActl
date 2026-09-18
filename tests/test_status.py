from actl.core.status import agent_status
from actl.core import status
from actl.core.validation import TargetValidation


def test_unmapped_status_does_not_guess_a_target():
    status = agent_status({"agents": {"codex": {"target": "0:0.2"}}}, "grok")
    assert status["pane"] == "UNMAPPED"
    assert status["target"] == "-"


def test_explicit_valid_mapping_reports_up(monkeypatch):
    monkeypatch.setattr(status, "validate_target", lambda agent, target: TargetValidation("UP", target, command="claude", path="/project"))
    result = agent_status({"agents": {"claude-team": {"target": "0:0.0"}}}, "claude-team")
    assert result["pane"] == "UP"
    assert result["target"] == "0:0.0"


def test_wrong_agent_process_reports_mismatch(monkeypatch):
    monkeypatch.setattr(status, "validate_target", lambda agent, target: TargetValidation("MISMATCH", target, command="agent", path="/project", detail="Cursor process"))
    result = agent_status({"agents": {"codex": {"target": "0:0.3"}}}, "codex")
    assert result["pane"] == "MISMATCH" and result["command"] == "agent"
