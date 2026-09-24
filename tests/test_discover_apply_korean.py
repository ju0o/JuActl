import io
from contextlib import redirect_stdout

from actl import cli
from actl.core.discovery import Detection
from actl.core.models import PaneInfo


def test_discover_read_only_message_is_korean():
    detection = Detection(PaneInfo("%1", "work:0.1", "codex", "/tmp", ""), "codex", "high", "")
    output = io.StringIO()
    with redirect_stdout(output):
        cli._discover({"agents": {}}, [detection])
    assert "보기만 했어요 — 연결하려면 actl discover --apply" in output.getvalue()


def test_discover_apply_message_is_connected_and_not_read_only(monkeypatch):
    detections = [
        Detection(PaneInfo("%1", "work:0.1", "codex", "/tmp", ""), "codex", "high", ""),
        Detection(PaneInfo("%0", "work:0.0", "commandcode", "/tmp", ""), "commandcode", "high", ""),
    ]
    monkeypatch.setattr(cli, "backup_config", lambda: "/tmp/backup.json")
    monkeypatch.setattr(cli, "save_config", lambda _: None)
    monkeypatch.setattr(
        cli,
        "reconcile",
        lambda *_: ({"agents": {"codex": {"target": "%1"}, "commandcode": {"target": "%0"}}}, ["codex: %1", "commandcode: %0"]),
    )
    output = io.StringIO()
    with redirect_stdout(output):
        cli._discover({"agents": {}}, detections, apply=True)
        cli._apply_discovery({"agents": {}}, detections)
    text = output.getvalue()
    assert "연결했어요: Codex → %1, CommandCode → %0 (이전 설정은 백업해 뒀어요)" in text
    assert "read-only" not in text
    assert "Discovery is read-only" not in text


def test_help_descriptions_are_korean():
    output = io.StringIO()
    with redirect_stdout(output):
        cli._print_cli_help()
    text = output.getvalue()
    assert "대화형 모드" in text
    assert "에이전트 보드" in text
    assert "live pane 감지 목록" in text
    assert "REPL (Agent" not in text
    assert "List (or apply) live pane detections" not in text
