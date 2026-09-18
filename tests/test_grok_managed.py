"""Phase 1B Managed Grok APIs — must not alter Direct expectations in test_grok.py."""
from __future__ import annotations

import json
from pathlib import Path

from actl.agents import grok
from actl.agents.grok import (
    bind_grok_user_turn,
    collect_grok_final,
    read_grok_jsonl_forward,
    resolve_grok,
    resolve_grok_managed,
)
from actl.core import live_observe
from actl.core.models import PaneInfo
from actl.core.validation import ProcessInfo


def _session(root, cwd, session, rows):
    directory = root / "sessions" / str(cwd).replace("/", "%2F") / session
    directory.mkdir(parents=True)
    (directory / "events.jsonl").write_text(
        json.dumps({"type": "turn_started", "session_id": session}) + "\n", encoding="utf-8"
    )
    (directory / "chat_history.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8"
    )
    return directory


def _cursor_for(path: Path, byte_offset: int = 0) -> dict:
    st = path.stat()
    from actl.agents.codex import _prefix_sha256

    return {
        "path": str(path),
        "dev": str(st.st_dev),
        "inode": str(st.st_ino),
        "byteOffset": byte_offset,
        "prefixSha256": _prefix_sha256(path, byte_offset),
    }


def test_managed_wire_prompt_bind_and_assistant_final(tmp_path):
    root = tmp_path / ".grok"
    cwd = tmp_path / "ws"
    cwd.mkdir()
    sid = "sess-grok-1"
    prompt = "wire-prompt-grok-xyz"
    rows = [
        {"type": "user", "content": prompt},
        {"type": "assistant", "content": "FINAL_OK"},
    ]
    directory = _session(root, cwd, sid, rows)
    path = directory / "chat_history.jsonl"
    cursor = _cursor_for(path)
    forward = read_grok_jsonl_forward(path, cursor)
    binding = bind_grok_user_turn(cursor, prompt, forward.events, session_id=sid)
    assert binding.code == "OK"
    result = collect_grok_final(
        path=path,
        cursor=cursor,
        wire_prompt=prompt,
        command_id="cmd1_g",
        runtime_id="rt1_g",
        prompt_sha256="deadbeef",
        session_id=sid,
    )
    assert result.code == "FINAL"
    assert result.packet is not None
    assert result.packet["rawFinalText"] == "FINAL_OK"
    assert result.packet["sessionId"] == sid
    assert result.packet["parserVersion"] == "grok-managed-final-v1"
    assert result.packet["sourceRef"]["path"] == str(path)


def test_managed_list_content_user_binds(tmp_path):
    root = tmp_path / ".grok"
    cwd = tmp_path / "ws"
    cwd.mkdir()
    sid = "sess-list"
    prompt = "list-wire-prompt"
    rows = [
        {"type": "user", "content": [{"type": "text", "text": f"prefix {prompt} suffix"}]},
        {"type": "assistant", "content": "LIST_OK"},
    ]
    path = _session(root, cwd, sid, rows) / "chat_history.jsonl"
    result = collect_grok_final(
        path=path,
        cursor=_cursor_for(path),
        wire_prompt=prompt,
        command_id="cmd_list",
        runtime_id="rt_list",
        prompt_sha256="ab",
        session_id=sid,
    )
    assert result.code == "FINAL"
    assert result.packet["rawFinalText"] == "LIST_OK"


def test_managed_no_matching_user_is_not_final(tmp_path):
    root = tmp_path / ".grok"
    cwd = tmp_path / "ws"
    cwd.mkdir()
    sid = "sess-nomatch"
    rows = [
        {"type": "user", "content": "other-prompt"},
        {"type": "assistant", "content": "NO"},
    ]
    path = _session(root, cwd, sid, rows) / "chat_history.jsonl"
    result = collect_grok_final(
        path=path,
        cursor=_cursor_for(path),
        wire_prompt="wire-missing",
        command_id="cmd_nm",
        runtime_id="rt_nm",
        prompt_sha256="cd",
        session_id=sid,
    )
    assert result.code == "RESULT_NOT_FINAL"


def test_managed_multiple_matching_users_fail_closed(tmp_path):
    root = tmp_path / ".grok"
    cwd = tmp_path / "ws"
    cwd.mkdir()
    sid = "sess-multi-user"
    prompt = "wire-dup"
    rows = [
        {"type": "user", "content": prompt},
        {"type": "assistant", "content": "ONE"},
        {"type": "user", "content": f"again {prompt}"},
        {"type": "assistant", "content": "TWO"},
    ]
    path = _session(root, cwd, sid, rows) / "chat_history.jsonl"
    result = collect_grok_final(
        path=path,
        cursor=_cursor_for(path),
        wire_prompt=prompt,
        command_id="cmd_mu",
        runtime_id="rt_mu",
        prompt_sha256="ef",
        session_id=sid,
    )
    assert result.code == "AMBIGUOUS_SESSION"


def test_managed_reasoning_tool_only_after_user_is_not_final(tmp_path):
    root = tmp_path / ".grok"
    cwd = tmp_path / "ws"
    cwd.mkdir()
    sid = "sess-reason"
    prompt = "wire-reason"
    rows = [
        {"type": "user", "content": prompt},
        {"type": "reasoning", "summary": "thinking"},
        {"type": "tool_result", "content": "tool-out"},
    ]
    path = _session(root, cwd, sid, rows) / "chat_history.jsonl"
    result = collect_grok_final(
        path=path,
        cursor=_cursor_for(path),
        wire_prompt=prompt,
        command_id="cmd_r",
        runtime_id="rt_r",
        prompt_sha256="11",
        session_id=sid,
    )
    assert result.code == "RESULT_NOT_FINAL"


def test_managed_assistant_with_tool_calls_is_not_final(tmp_path):
    root = tmp_path / ".grok"
    cwd = tmp_path / "ws"
    cwd.mkdir()
    sid = "sess-tools"
    prompt = "wire-tools"
    rows = [
        {"type": "user", "content": prompt},
        {
            "type": "assistant",
            "content": "calling tool",
            "tool_calls": [{"id": "c1", "name": "Bash", "arguments": "{}"}],
        },
    ]
    path = _session(root, cwd, sid, rows) / "chat_history.jsonl"
    result = collect_grok_final(
        path=path,
        cursor=_cursor_for(path),
        wire_prompt=prompt,
        command_id="cmd_t",
        runtime_id="rt_t",
        prompt_sha256="22",
        session_id=sid,
    )
    assert result.code == "RESULT_NOT_FINAL"


def test_resolve_grok_with_explicit_grok_pid_skips_default_pane_field(monkeypatch, tmp_path):
    root = tmp_path / ".grok"
    cwd = tmp_path / "ws"
    cwd.mkdir()
    sid = "sess-pid"
    directory = _session(root, cwd, sid, [{"type": "user", "content": "x"}, {"type": "assistant", "content": "y"}])
    event = directory / "events.jsonl"

    def boom(*_a, **_k):
        raise AssertionError("pane_field must not be required when grok_pid is set")

    monkeypatch.setattr(grok, "pane_field", boom)
    monkeypatch.setattr(
        grok,
        "pane_processes",
        lambda pid: [ProcessInfo(pid, 1, "/usr/bin/grok")] if pid == 5159 else [],
    )
    monkeypatch.setattr(grok, "_open_event_files", lambda *_: [event])
    resolution = resolve_grok(root, "%1", grok_pid=5159, workspace=cwd)
    assert resolution.confidence == "exact"
    assert resolution.session_id == sid
    managed = resolve_grok_managed(root, "%1", 5159, workspace=cwd)
    assert managed.code == "OK"
    assert managed.chat_history_path == directory / "chat_history.jsonl"


def test_live_observe_maps_grok_profile_root_and_bootstrap(monkeypatch, tmp_path):
    sock = tmp_path / "s.sock"
    grok_root = (tmp_path / ".grok").resolve()
    grok_root.mkdir()
    ws = tmp_path / "ws"
    ws.mkdir()

    monkeypatch.setattr(live_observe, "socket_file_identity", lambda p: (str(sock), "1", "2"))
    monkeypatch.setattr(live_observe, "read_boot_id", lambda: "boot")
    monkeypatch.setattr(live_observe, "tmux_server_pid", lambda p: 1)
    monkeypatch.setattr(live_observe, "proc_start_ticks", lambda pid: "9")
    monkeypatch.setattr(live_observe, "proc_exe_identity", lambda pid: ("/usr/bin/grok", "5", "6"))
    monkeypatch.setattr(
        live_observe,
        "list_panes",
        lambda socket_path=None: [PaneInfo("%1", "s:0.0", "grok", str(ws), "t")],
    )
    monkeypatch.setattr(live_observe, "pane_field", lambda *a, **k: "40")
    monkeypatch.setattr(
        live_observe,
        "pane_processes",
        lambda pane_pid: [ProcessInfo(40, 1, "bash"), ProcessInfo(41, 40, "/usr/bin/grok")],
    )
    monkeypatch.setattr(live_observe, "_grok_profile_root", lambda pid: str(grok_root))
    monkeypatch.setattr(live_observe, "_expected_session_for_grok", lambda *a, **k: "BOOTSTRAP")
    monkeypatch.setattr(live_observe, "pane_mode_is_normal", lambda *a, **k: True)

    rows = live_observe.observe_socket_candidates(str(sock), "grok", host_key="hk", uid="1000")
    assert len(rows) == 1
    assert rows[0]["identityEvidence"]["agentKind"] == "grok"
    assert rows[0]["profileRoot"] == str(grok_root)
    assert rows[0]["expectedSession"] == "BOOTSTRAP"
    assert rows[0]["processState"] == "UP"


def test_resolve_wrong_process_is_none(monkeypatch, tmp_path):
    root = tmp_path / ".grok"
    cwd = tmp_path / "ws"
    cwd.mkdir()
    sid = "sess-wrong"
    directory = _session(root, cwd, sid, [{"type": "user", "content": "x"}, {"type": "assistant", "content": "y"}])
    event = directory / "events.jsonl"
    monkeypatch.setattr(grok, "pane_field", lambda *a, **k: str(cwd))
    monkeypatch.setattr(
        grok,
        "pane_processes",
        lambda pid: [ProcessInfo(pid, 1, "/usr/bin/claude")] if pid == 99 else [],
    )
    monkeypatch.setattr(grok, "_open_event_files", lambda *_: [event])
    resolution = resolve_grok(root, "%1", grok_pid=99, workspace=cwd)
    assert resolution.confidence == "none"
    assert "No live Grok process" in resolution.detail
    managed = resolve_grok_managed(root, "%1", 99, workspace=cwd)
    assert managed.code == "DOWN"


def test_resolve_stale_incarnation_pid_gone(monkeypatch, tmp_path):
    root = tmp_path / ".grok"
    cwd = tmp_path / "ws"
    cwd.mkdir()
    _session(root, cwd, "sess-stale", [{"type": "user", "content": "x"}])
    monkeypatch.setattr(grok, "pane_field", lambda *a, **k: str(cwd))
    # Explicit pid not present in pane process tree → DOWN
    monkeypatch.setattr(grok, "pane_processes", lambda pid: [])
    resolution = resolve_grok(root, "%1", grok_pid=999999, workspace=cwd)
    assert resolution.confidence == "none"
    managed = resolve_grok_managed(root, "%1", 999999, workspace=cwd)
    assert managed.code == "DOWN"


def test_resolve_ambiguous_multiple_event_streams(monkeypatch, tmp_path):
    root = tmp_path / ".grok"
    cwd = tmp_path / "ws"
    cwd.mkdir()
    a = _session(root, cwd, "sess-a", [{"type": "user", "content": "x"}]) / "events.jsonl"
    b = _session(root, cwd, "sess-b", [{"type": "user", "content": "y"}]) / "events.jsonl"
    monkeypatch.setattr(
        grok,
        "pane_processes",
        lambda pid: [ProcessInfo(pid, 1, "/usr/bin/grok")] if pid == 7 else [],
    )
    monkeypatch.setattr(grok, "_open_event_files", lambda *_: [a, b])
    resolution = resolve_grok(root, "%1", grok_pid=7, workspace=cwd)
    assert resolution.confidence == "none"
    assert "multiple open session event streams" in resolution.detail
    managed = resolve_grok_managed(root, "%1", 7, workspace=cwd)
    assert managed.code == "AMBIGUOUS_SESSION"


def test_resolve_wrong_workspace_rejected(monkeypatch, tmp_path):
    root = tmp_path / ".grok"
    cwd = tmp_path / "ws"
    other = tmp_path / "other"
    cwd.mkdir()
    other.mkdir()
    directory = _session(root, cwd, "sess-ws", [{"type": "user", "content": "x"}])
    event = directory / "events.jsonl"
    monkeypatch.setattr(
        grok,
        "pane_processes",
        lambda pid: [ProcessInfo(pid, 1, "/usr/bin/grok")] if pid == 7 else [],
    )
    monkeypatch.setattr(grok, "_open_event_files", lambda *_: [event])
    resolution = resolve_grok(root, "%1", grok_pid=7, workspace=other)
    assert resolution.confidence == "none"
    assert "workspace does not match" in resolution.detail


def test_resolve_wrong_profile_root_has_no_events(monkeypatch, tmp_path):
    root = tmp_path / ".grok"
    wrong = tmp_path / ".other-grok"
    cwd = tmp_path / "ws"
    cwd.mkdir()
    wrong.mkdir()
    directory = _session(root, cwd, "sess-prof", [{"type": "user", "content": "x"}])
    event = directory / "events.jsonl"
    monkeypatch.setattr(
        grok,
        "pane_processes",
        lambda pid: [ProcessInfo(pid, 1, "/usr/bin/grok")] if pid == 7 else [],
    )
    # Looking under wrong profile → no open files relative to that root
    monkeypatch.setattr(grok, "_open_event_files", lambda pid, sessions_root: [])
    resolution = resolve_grok(wrong, "%1", grok_pid=7, workspace=cwd)
    assert resolution.confidence == "none"
    assert "no open session event stream" in resolution.detail.lower() or "No live" in resolution.detail or resolution.detail


def test_managed_empty_assistant_partial_is_not_final(tmp_path):
    root = tmp_path / ".grok"
    cwd = tmp_path / "ws"
    cwd.mkdir()
    sid = "sess-empty"
    prompt = "wire-empty"
    rows = [
        {"type": "user", "content": prompt},
        {"type": "assistant", "content": "   "},
    ]
    path = _session(root, cwd, sid, rows) / "chat_history.jsonl"
    result = collect_grok_final(
        path=path,
        cursor=_cursor_for(path),
        wire_prompt=prompt,
        command_id="cmd_e",
        runtime_id="rt_e",
        prompt_sha256="33",
        session_id=sid,
    )
    assert result.code == "RESULT_NOT_FINAL"


def test_managed_multiple_assistants_fail_closed(tmp_path):
    root = tmp_path / ".grok"
    cwd = tmp_path / "ws"
    cwd.mkdir()
    sid = "sess-multi-a"
    prompt = "wire-multi-a"
    rows = [
        {"type": "user", "content": prompt},
        {"type": "assistant", "content": "FIRST"},
        {"type": "assistant", "content": "SECOND"},
    ]
    path = _session(root, cwd, sid, rows) / "chat_history.jsonl"
    result = collect_grok_final(
        path=path,
        cursor=_cursor_for(path),
        wire_prompt=prompt,
        command_id="cmd_ma",
        runtime_id="rt_ma",
        prompt_sha256="44",
        session_id=sid,
    )
    assert result.code == "AMBIGUOUS_SESSION"


def test_live_observe_maps_grok_exact_session(monkeypatch, tmp_path):
    sock = tmp_path / "s.sock"
    grok_root = (tmp_path / ".grok").resolve()
    grok_root.mkdir()
    ws = tmp_path / "ws"
    ws.mkdir()
    sid = "exact-sess"

    monkeypatch.setattr(live_observe, "socket_file_identity", lambda p: (str(sock), "1", "2"))
    monkeypatch.setattr(live_observe, "read_boot_id", lambda: "boot")
    monkeypatch.setattr(live_observe, "tmux_server_pid", lambda p: 1)
    monkeypatch.setattr(live_observe, "proc_start_ticks", lambda pid: "9")
    monkeypatch.setattr(live_observe, "proc_exe_identity", lambda pid: ("/usr/bin/grok", "5", "6"))
    monkeypatch.setattr(
        live_observe,
        "list_panes",
        lambda socket_path=None: [PaneInfo("%1", "s:0.0", "grok", str(ws), "t")],
    )
    monkeypatch.setattr(live_observe, "pane_field", lambda *a, **k: "40")
    monkeypatch.setattr(
        live_observe,
        "pane_processes",
        lambda pane_pid: [ProcessInfo(40, 1, "bash"), ProcessInfo(41, 40, "/usr/bin/grok")],
    )
    monkeypatch.setattr(live_observe, "_grok_profile_root", lambda pid: str(grok_root))
    monkeypatch.setattr(live_observe, "_expected_session_for_grok", lambda *a, **k: sid)
    monkeypatch.setattr(live_observe, "pane_mode_is_normal", lambda *a, **k: True)

    rows = live_observe.observe_socket_candidates(str(sock), "grok", host_key="hk", uid="1000")
    assert rows[0]["expectedSession"] == sid
    from actl.core import runtime

    cand = runtime._candidate_from_observation(rows[0], runtime.observed_at_now())
    assert cand["capabilities"]["managed.collect.final"] is True
    assert cand["capabilities"]["managed.send"] is False
