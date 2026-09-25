from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_backlog_documents_concurrent_send_status():
    backlog = ROOT / "BACKLOG.md"
    assert backlog.exists()
    assert "동시 전송" in backlog.read_text(encoding="utf-8")
