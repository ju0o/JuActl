import io

import actl.cli as cli


def run_send(monkeypatch, error=None, prompt="질문"):
    stderr = io.StringIO()
    monkeypatch.setattr(cli, "ensure_config", lambda: None)
    monkeypatch.setattr(cli, "load_config", lambda: {})
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(prompt))
    monkeypatch.setattr(cli.sys, "stderr", stderr)
    monkeypatch.setattr(cli, "_send_to_selected", lambda *_: (_ for _ in ()).throw(error) if error else "%0")
    try:
        cli.main(["send", "codex"])
    except SystemExit as exc:
        return exc.code, stderr.getvalue()
    return 0, stderr.getvalue()


def test_send_empty_prompt_is_plain_korean(monkeypatch):
    code, stderr = run_send(monkeypatch, prompt="\n")
    assert code == 1
    assert stderr == '보낼 내용이 비어 있어요 — echo "질문" | actl send AGENT\n'


def test_send_failures_hide_exception_text(monkeypatch):
    code, stderr = run_send(monkeypatch, RuntimeError("WRITER_DENIED: secret detail"))
    assert code == 1
    assert stderr == "보내지 못했어요 — 잠시 후 다시 보내 주세요\n"
    code, stderr = run_send(monkeypatch, ValueError("Selected runtime is STALE; sending blocked: secret detail"))
    assert code == 1
    assert stderr == "보내지 못했어요 — 잠시 후 다시 보내 주세요\n"


def test_send_approval_wait_keeps_message_and_exit_code(monkeypatch):
    message = "승인을 기다리고 있어요 — 먼저 에이전트에서 승인해 주세요"
    code, stderr = run_send(monkeypatch, RuntimeError(message))
    assert code == 2
    assert stderr == message + "\n"
