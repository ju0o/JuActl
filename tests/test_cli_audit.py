from actl import cli


def test_send_failure_is_audited_without_prompt(monkeypatch, tmp_path):
    from actl.core import audit

    monkeypatch.setattr(audit, "audit_path", lambda: tmp_path / "audit.jsonl")
    monkeypatch.setattr(cli, "_resolve_live_target", lambda *_: "%12")
    monkeypatch.setattr(cli, "send_prompt", lambda *_: (_ for _ in ()).throw(RuntimeError("transport")))

    try:
        cli._send_to_selected({}, "commandcode", "PRIVATE PROMPT")
    except RuntimeError:
        pass
    else:
        raise AssertionError("expected send failure")

    row = audit.read()[0]
    assert row["event"] == "send"
    assert row["ok"] is False
    assert "PRIVATE PROMPT" not in (tmp_path / "audit.jsonl").read_text()
