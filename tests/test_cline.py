import json
import sqlite3
from pathlib import Path

from actl.agents import cline
from actl.agents.cline import extract_cline, resolve_cline
from actl.core.validation import ProcessInfo


def _session(root, session_id, pid, cwd, messages):
    path = root / "sessions" / session_id / f"{session_id}.messages.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"messages": messages}))
    db = root / "data" / "db" / "sessions.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE IF NOT EXISTS sessions (session_id TEXT, pid INTEGER, status TEXT, interactive INTEGER, cwd TEXT, messages_path TEXT)")
    con.execute("INSERT INTO sessions VALUES (?, ?, 'idle', 1, ?, ?)", (session_id, pid, str(cwd), str(path)))
    con.commit(); con.close()
    return path


def _resolve(monkeypatch, root, cwd, pid=10):
    monkeypatch.setattr(cline, "pane_field", lambda *_: str(cwd))
    monkeypatch.setattr(cline, "pane_processes", lambda _: [ProcessInfo(pid, 1, "node /usr/local/bin/cline")])
    return resolve_cline(root, "%8", pid)


def test_cline_exact_pid_session_rejects_same_project_second_session(monkeypatch, tmp_path):
    cwd = tmp_path / "project"; cwd.mkdir()
    _session(tmp_path, "active", 10, cwd, [{"role": "user", "content": []}, {"role": "assistant", "content": [{"type": "text", "text": "VISIBLE"}]}])
    _session(tmp_path, "other", 11, cwd, [{"role": "assistant", "content": [{"type": "text", "text": "WRONG"}]}])
    resolution = _resolve(monkeypatch, tmp_path, cwd)
    assert resolution.session_id == "active" and extract_cline(resolution).text == "VISIBLE"
    con = sqlite3.connect(tmp_path / "data" / "db" / "sessions.db")
    con.execute("UPDATE sessions SET status='running' WHERE session_id='active'")
    con.commit(); con.close()
    assert _resolve(monkeypatch, tmp_path, cwd).confidence == "none"


def test_cline_fails_closed_on_ambiguous_or_wrong_pid_and_filters_content(monkeypatch, tmp_path):
    cwd = tmp_path / "project"; cwd.mkdir()
    _session(tmp_path, "one", 10, cwd, [{"role": "user", "content": [{"type": "text", "text": "PROMPT"}]}, {"role": "assistant", "content": [{"type": "thinking", "text": "NO"}, {"type": "tool_use", "text": "NO"}, {"type": "text", "text": "VISIBLE"}]}])
    _session(tmp_path, "two", 10, cwd, [{"role": "assistant", "content": [{"type": "text", "text": "WRONG"}]}])
    assert _resolve(monkeypatch, tmp_path, cwd).confidence == "none"
    assert _resolve(monkeypatch, tmp_path, cwd, 99).confidence == "none"


def test_cline_restart_with_new_pid_recorrelates(monkeypatch, tmp_path):
    # Relaunching Cline updates the session registry pid; /copy must follow it.
    cwd = tmp_path / "project"; cwd.mkdir()
    _session(tmp_path, "active", 10, cwd, [{"role": "user", "content": []}, {"role": "assistant", "content": [{"type": "text", "text": "RESTART_OK"}]}])
    con = sqlite3.connect(tmp_path / "data" / "db" / "sessions.db")
    con.execute("UPDATE sessions SET pid = 999 WHERE session_id = 'active'")
    con.commit(); con.close()
    monkeypatch.setattr(cline, "pane_field", lambda *_: str(cwd))
    monkeypatch.setattr(cline, "pane_processes", lambda _: [ProcessInfo(999, 1, "node /usr/local/bin/cline")])
    resolution = resolve_cline(tmp_path, "%8", 999)
    assert resolution.confidence == "exact"
    assert extract_cline(resolution).text == "RESTART_OK"


def test_cline_data_dir_equals_form_respected(monkeypatch, tmp_path):
    # Launch variant `--data-dir=<path>` must resolve the same sessions.db as
    # the space-separated form instead of silently using the default root.
    cwd = tmp_path / "project"; cwd.mkdir()
    custom = tmp_path / "custom-cline-data"; custom.mkdir()
    _session(tmp_path, "ignored", 10, cwd, [{"role": "assistant", "content": [{"type": "text", "text": "DEFAULT_TRAP"}]}])
    db = custom / "db" / "sessions.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE sessions (session_id TEXT, pid INTEGER, status TEXT, interactive INTEGER, cwd TEXT, messages_path TEXT)")
    messages = custom / "sessions" / "real" / "real.messages.json"
    messages.parent.mkdir(parents=True, exist_ok=True)
    messages.write_text(json.dumps({"messages": [{"role": "user", "content": []}, {"role": "assistant", "content": [{"type": "text", "text": "EQUALS_FORM_OK"}]}]}))
    con.execute("INSERT INTO sessions VALUES (?, ?, 'idle', 1, ?, ?)", ("real", 10, str(cwd), str(messages)))
    con.commit(); con.close()

    monkeypatch.setattr(cline, "pane_field", lambda *_: str(cwd))
    monkeypatch.setattr(cline, "pane_processes", lambda _: [ProcessInfo(10, 1, "node /usr/local/bin/cline")])
    real_read = Path.read_bytes
    fake = b"node\x00/usr/local/bin/cline\x00" + f"--data-dir={custom}".encode("utf-8") + b"\x00"
    monkeypatch.setattr(
        Path,
        "read_bytes",
        lambda self: fake if str(self) == f"/proc/10/cmdline" else real_read(self),
    )

    resolution = resolve_cline(tmp_path, "%8", 10)
    assert resolution.confidence == "exact"
    assert extract_cline(resolution).text == "EQUALS_FORM_OK"
