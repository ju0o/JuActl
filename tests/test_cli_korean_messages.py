import io
from contextlib import redirect_stdout

from actl import cli
from actl.core.models import CopyResult


def test_help_unknown_send_and_copy_messages_are_korean(monkeypatch):
    out = io.StringIO()
    with redirect_stdout(out):
        cli._print_cli_help()
        print(cli._unknown_agent("nobody"))
        monkeypatch.setattr(cli, "_resolve_live_target", lambda *_: "%0")
        monkeypatch.setattr(cli, "extract_last_response", lambda *_: CopyResult(None, "x", "none"))
        cli._copy({}, "codex")
    text = out.getvalue()
    assert 'echo "질문" | actl send AGENT' in text
    assert "Unknown agent: nobody — 쓸 수 있는 이름:" in text
    assert "아직 새 답이 없어요 — 작업이 끝나면 다시 해 보세요" in text
    assert "sent to" not in text
    assert "No response text found" not in text


def test_copy_no_result_keeps_reason_in_audit(monkeypatch):
    captured = {}
    monkeypatch.setattr(cli, "_resolve_live_target", lambda *_: "%0")
    monkeypatch.setattr(
        cli,
        "extract_last_response",
        lambda *_: CopyResult(None, "codex-rollout", "none", "No completed assistant AgentMessage in matched Codex rollout"),
    )
    from actl.core import audit

    monkeypatch.setattr(audit, "record", lambda event, **fields: captured.update(event=event, **fields))
    out = io.StringIO()
    with redirect_stdout(out):
        cli._copy({}, "codex")
    assert captured["detail"] == "No completed assistant AgentMessage in matched Codex rollout"
    assert "아직 새 답이 없어요 — 작업이 끝나면 다시 해 보세요" in out.getvalue()
    assert "No completed assistant AgentMessage" not in out.getvalue()


def test_send_and_osc52_messages_use_korean(monkeypatch):
    monkeypatch.setattr(cli, "ensure_config", lambda: None)
    monkeypatch.setattr(cli, "load_config", lambda: {})
    monkeypatch.setattr(cli, "_send_to_selected", lambda *_args, **_kwargs: "%0")
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("질문"))
    out = io.StringIO()
    with redirect_stdout(out):
        cli.main(["send", "codex"])
    assert "Codex에게 보냈어요 · 답이 오면: actl copy codex" in out.getvalue()

    monkeypatch.setattr(cli, "_resolve_live_target", lambda *_: "%0")
    monkeypatch.setattr(cli, "extract_last_response", lambda *_: CopyResult("답", "x", "exact"))
    monkeypatch.setattr(cli, "copy_text", lambda *_args, **_kwargs: "osc52")
    out = io.StringIO()
    with redirect_stdout(out):
        cli._copy({}, "codex")
    assert "터미널 클립보드로 보냈어요 (안 붙여지면 actl copy codex --print)" in out.getvalue()


def test_status_activity_column_uses_observe_activity(monkeypatch):
    monkeypatch.setattr(cli, "AGENTS", {"codex": cli.AGENTS["codex"]})
    monkeypatch.setattr(cli, "agent_status", lambda *_: {
        "agent": "Codex", "target": "%0", "pane": "UP", "command": "codex", "path": "/tmp",
        "activity": "RUNNING",
    })
    out = io.StringIO()
    with redirect_stdout(out):
        cli._print_status({}, "codex")
    assert "작업 중" in out.getvalue()
