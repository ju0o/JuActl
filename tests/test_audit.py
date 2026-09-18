import json

from actl.core import audit
from actl import cli


def test_audit_records_metadata_without_content(monkeypatch, tmp_path):
    path = tmp_path / "audit.jsonl"
    monkeypatch.setattr(audit, "audit_path", lambda: path)

    audit.record("send", agent="commandcode", target="%12", chars=42)
    audit.record("copy", source="codex-rollout:/private/session.jsonl", prompt="PRIVATE")

    rows = audit.read()
    assert rows[0]["event"] == "send"
    assert rows[0]["chars"] == 42
    assert "prompt" not in rows[0]
    assert "response" not in rows[0]
    for line in path.read_text().splitlines():
        json.loads(line)
    copy_row = rows[1]
    assert copy_row["source"] == "codex-rollout"
    assert "prompt" not in copy_row


def test_audit_drops_all_content_fields(monkeypatch, tmp_path):
    path = tmp_path / "audit.jsonl"
    monkeypatch.setattr(audit, "audit_path", lambda: path)

    audit.record("copy", text="secret", response="secret", content="secret", body="secret", raw="secret")

    row = audit.read()[0]
    assert set(row) == {"ts", "event"}


def test_audit_read_skips_corrupt_tail(monkeypatch, tmp_path):
    path = tmp_path / "audit.jsonl"
    monkeypatch.setattr(audit, "audit_path", lambda: path)
    path.write_text('{"event":"ok"}\nnot-json\n')

    assert audit.read() == [{"event": "ok"}]


def test_history_filters_successful_copy_records_without_body(monkeypatch, tmp_path):
    path = tmp_path / "audit.jsonl"
    monkeypatch.setattr(audit, "audit_path", lambda: path)
    audit.record("copy", agent="commandcode", ok=True, result_hash="abc", chars=3)
    audit.record("copy", agent="commandcode", ok=False, error="unresolved")
    audit.record("send", agent="commandcode", ok=True, chars=20)

    import io
    from contextlib import redirect_stdout

    out = io.StringIO()
    monkeypatch.setattr(cli, "AGENTS", {"commandcode": cli.AGENTS["commandcode"]})
    with redirect_stdout(out):
        cli._history("commandcode", 10)
    assert '"result_hash":"abc"' in out.getvalue()
    assert "unresolved" not in out.getvalue()
    assert '"chars":20' not in out.getvalue()


def test_history_non_positive_limit_is_empty(monkeypatch, tmp_path):
    path = tmp_path / "audit.jsonl"
    monkeypatch.setattr(audit, "audit_path", lambda: path)
    audit.record("copy", agent="commandcode", ok=True, result_hash="abc")

    import io
    from contextlib import redirect_stdout

    out = io.StringIO()
    with redirect_stdout(out):
        cli._history("commandcode", 0)
    assert out.getvalue() == ""


def test_audit_rotates_large_log(monkeypatch, tmp_path):
    path = tmp_path / "audit.jsonl"
    monkeypatch.setattr(audit, "audit_path", lambda: path)
    path.write_bytes(b"x" * audit._MAX_BYTES)
    audit.record("rotated", ok=True)
    assert (tmp_path / "audit.jsonl.1").is_file()
    assert json.loads(path.read_text(encoding="utf-8"))["event"] == "rotated"
