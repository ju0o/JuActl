"""Phase 1B Managed Claude APIs — must not alter Direct expectations in test_claude.py."""
from __future__ import annotations

import json
from pathlib import Path

from actl.agents import claude
from actl.agents.claude import (
    _project_dir,
    bind_claude_user_turn,
    collect_claude_final,
    read_claude_jsonl_forward,
    resolve_claude_managed,
)
from actl.core import live_observe
from actl.core.models import PaneInfo
from actl.core.validation import ProcessInfo


def _record(role, content, stop_reason=None, *, session_id=None, cwd=None, sidechain=False, uuid=None):
    row = {"type": "assistant" if role == "assistant" else "user", "message": {"role": role, "content": content}}
    if stop_reason is not None:
        row["message"]["stop_reason"] = stop_reason
    if session_id is not None:
        row["sessionId"] = session_id
    if cwd is not None:
        row["cwd"] = str(cwd)
    if sidechain:
        row["isSidechain"] = True
    if uuid is not None:
        row["uuid"] = uuid
    return row


def _write_session(root, pane_path, session_id, rows, *, name=None):
    project = _project_dir(root, str(pane_path))
    project.mkdir(parents=True, exist_ok=True)
    path = project / (name or f"{session_id}.jsonl")
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    return path


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


def test_managed_wire_prompt_bind_and_terminal_final(tmp_path):
    root = tmp_path / ".claude-pro"
    pane = tmp_path / "Agent-Relay"
    pane.mkdir()
    sid = "sess-pro-1"
    prompt = "wire-prompt-claude-xyz"
    rows = [
        _record("user", [{"type": "text", "text": prompt}], session_id=sid, cwd=pane, uuid="u1"),
        _record(
            "assistant",
            [{"type": "text", "text": "FINAL_OK"}],
            "end_turn",
            session_id=sid,
            cwd=pane,
            uuid="a1",
        ),
    ]
    path = _write_session(root, pane, sid, rows)
    cursor = _cursor_for(path)
    forward = read_claude_jsonl_forward(path, cursor)
    binding = bind_claude_user_turn(cursor, prompt, forward.events, session_id=sid)
    assert binding.code == "OK"
    result = collect_claude_final(
        path=path,
        cursor=cursor,
        wire_prompt=prompt,
        command_id="cmd1_c",
        runtime_id="rt1_c",
        prompt_sha256="deadbeef",
        session_id=sid,
    )
    assert result.code == "FINAL"
    assert result.packet is not None
    assert result.packet["rawFinalText"] == "FINAL_OK"
    assert result.packet["sessionId"] == sid
    assert result.packet["turnId"] == "a1"
    assert result.packet["parserVersion"] == "claude-managed-final-v1"
    assert result.packet["sourceRef"]["path"] == str(path)


def test_managed_claude_team_wire_prompt_bind_and_terminal_final(tmp_path):
    """claude-team package: same Managed predicates on ~/.claude-team profile root."""
    root = tmp_path / ".claude-team"
    pane = tmp_path / "Team-WS"
    pane.mkdir()
    sid = "sess-team-1"
    prompt = "wire-prompt-claude-team-xyz"
    rows = [
        _record("user", [{"type": "text", "text": prompt}], session_id=sid, cwd=pane, uuid="tu1"),
        _record(
            "assistant",
            [{"type": "text", "text": "TEAM_FINAL_OK"}],
            "end_turn",
            session_id=sid,
            cwd=pane,
            uuid="ta1",
        ),
    ]
    path = _write_session(root, pane, sid, rows)
    cursor = _cursor_for(path)
    result = collect_claude_final(
        path=path,
        cursor=cursor,
        wire_prompt=prompt,
        command_id="cmd1_team",
        runtime_id="rt1_team",
        prompt_sha256="cafebabe",
        session_id=sid,
    )
    assert result.code == "FINAL"
    assert result.packet is not None
    assert result.packet["rawFinalText"] == "TEAM_FINAL_OK"
    assert result.packet["sessionId"] == sid
    assert result.packet["turnId"] == "ta1"
    assert result.packet["parserVersion"] == "claude-managed-final-v1"
    assert ".claude-team" in result.packet["sourceRef"]["path"]


def test_managed_user_content_string_form_binds(tmp_path):
    root = tmp_path / ".claude-pro"
    pane = tmp_path / "ws"
    pane.mkdir()
    sid = "sess-str"
    prompt = "string-wire-prompt"
    rows = [
        _record("user", prompt, session_id=sid, cwd=pane, uuid="u-str"),
        _record("assistant", [{"type": "text", "text": "STR_OK"}], "stop_sequence", session_id=sid, cwd=pane, uuid="a-str"),
    ]
    path = _write_session(root, pane, sid, rows)
    cursor = _cursor_for(path)
    result = collect_claude_final(
        path=path,
        cursor=cursor,
        wire_prompt=prompt,
        command_id="cmd_str",
        runtime_id="rt_str",
        prompt_sha256="ab",
        session_id=sid,
    )
    assert result.code == "FINAL"
    assert result.packet["rawFinalText"] == "STR_OK"


def test_managed_tool_use_only_after_user_is_not_final(tmp_path):
    root = tmp_path / ".claude-pro"
    pane = tmp_path / "ws"
    pane.mkdir()
    sid = "sess-tool"
    prompt = "wire-tool"
    rows = [
        _record("user", [{"type": "text", "text": prompt}], session_id=sid, cwd=pane),
        _record(
            "assistant",
            [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}],
            "tool_use",
            session_id=sid,
            cwd=pane,
        ),
    ]
    path = _write_session(root, pane, sid, rows)
    cursor = _cursor_for(path)
    result = collect_claude_final(
        path=path,
        cursor=cursor,
        wire_prompt=prompt,
        command_id="cmd_t",
        runtime_id="rt_t",
        prompt_sha256="cd",
        session_id=sid,
    )
    assert result.code == "RESULT_NOT_FINAL"


def test_managed_multiple_terminal_finals_fail_closed(tmp_path):
    root = tmp_path / ".claude-pro"
    pane = tmp_path / "ws"
    pane.mkdir()
    sid = "sess-multi"
    prompt = "wire-multi"
    rows = [
        _record("user", [{"type": "text", "text": prompt}], session_id=sid, cwd=pane),
        _record("assistant", [{"type": "text", "text": "ONE"}], "end_turn", session_id=sid, cwd=pane, uuid="a1"),
        _record("assistant", [{"type": "text", "text": "TWO"}], "end_turn", session_id=sid, cwd=pane, uuid="a2"),
    ]
    path = _write_session(root, pane, sid, rows)
    cursor = _cursor_for(path)
    result = collect_claude_final(
        path=path,
        cursor=cursor,
        wire_prompt=prompt,
        command_id="cmd_m",
        runtime_id="rt_m",
        prompt_sha256="ef",
        session_id=sid,
    )
    assert result.code == "AMBIGUOUS_SESSION"


def test_managed_pro_cannot_admit_team_transcript(monkeypatch, tmp_path):
    pro = tmp_path / ".claude-pro"
    team = tmp_path / ".claude-team"
    pane = tmp_path / "ws"
    pane.mkdir()
    sid = "shared"
    rows = [
        _record("user", [{"type": "text", "text": "x"}], session_id=sid, cwd=pane),
        _record("assistant", [{"type": "text", "text": "TEAM_ONLY"}], "end_turn", session_id=sid, cwd=pane),
    ]
    _write_session(team, pane, sid, rows)
    monkeypatch.setattr(claude, "_active_session_binding", lambda root, *_: (sid, 12, "/dev/pts/1", "") if root == team else (None, None, None, "no"))
    managed_pro = resolve_claude_managed(pro, str(pane), 12)
    assert managed_pro.code != "OK" or managed_pro.transcript_path is None
    managed_team = resolve_claude_managed(team, str(pane), 12)
    assert managed_team.code == "OK"
    assert managed_team.transcript_path is not None


def test_managed_team_cannot_admit_pro_transcript(monkeypatch, tmp_path):
    pro = tmp_path / ".claude-pro"
    team = tmp_path / ".claude-team"
    pane = tmp_path / "ws"
    pane.mkdir()
    sid = "shared-pro"
    rows = [
        _record("user", [{"type": "text", "text": "x"}], session_id=sid, cwd=pane),
        _record("assistant", [{"type": "text", "text": "PRO_ONLY"}], "end_turn", session_id=sid, cwd=pane),
    ]
    _write_session(pro, pane, sid, rows)
    monkeypatch.setattr(claude, "_active_session_binding", lambda root, *_: (sid, 13, "/dev/pts/2", "") if root == pro else (None, None, None, "no"))
    assert resolve_claude_managed(team, str(pane), 13).code != "OK" or resolve_claude_managed(team, str(pane), 13).transcript_path is None
    assert resolve_claude_managed(pro, str(pane), 13).code == "OK"


def test_managed_resolve_refuses_without_active_binding(monkeypatch, tmp_path):
    root = tmp_path / ".claude-pro"
    pane = tmp_path / "ws"
    pane.mkdir()
    sid = "orphan"
    _write_session(
        root,
        pane,
        sid,
        [_record("assistant", [{"type": "text", "text": "NO"}], "end_turn", session_id=sid, cwd=pane)],
    )
    monkeypatch.setattr(claude, "_active_session_binding", lambda *_: (None, None, None, "No selected-profile Claude session"))
    managed = resolve_claude_managed(root, str(pane), 12)
    assert managed.code == "DOWN"
    assert managed.transcript_path is None


def test_claude_agent_kind_maps_config_dir(monkeypatch, tmp_path):
    from actl.core import registry
    from actl.core.models import AgentSpec

    pro_root = (tmp_path / ".claude-pro").resolve()
    team_root = (tmp_path / ".claude-team").resolve()
    monkeypatch.setattr(
        registry,
        "AGENTS",
        {
            **registry.AGENTS,
            "claude-pro": AgentSpec("claude-pro", "Claude Pro", ("cp",), ("claude",), (pro_root,)),
            "claude-team": AgentSpec("claude-team", "Claude Team", ("ct",), ("claude",), (team_root,)),
        },
    )
    monkeypatch.setattr(live_observe, "_claude_config_dir", lambda pid: str(pro_root) if pid == 1 else str(team_root) if pid == 2 else None)
    assert live_observe._claude_agent_kind(1) == "claude-pro"
    assert live_observe._claude_agent_kind(2) == "claude-team"
    assert live_observe._claude_agent_kind(3) == "claude"
    monkeypatch.setattr(live_observe, "_claude_config_dir", lambda pid: "/tmp/unknown-profile")
    assert live_observe._claude_agent_kind(9) == "claude"


def test_live_observe_maps_claude_config_dir_to_agent_kind(monkeypatch, tmp_path):
    sock = tmp_path / "s.sock"
    pro_root = (tmp_path / ".claude-pro").resolve()
    pro_root.mkdir()
    ws = tmp_path / "ws"
    ws.mkdir()

    monkeypatch.setattr(live_observe, "socket_file_identity", lambda p: (str(sock), "1", "2"))
    monkeypatch.setattr(live_observe, "read_boot_id", lambda: "boot")
    monkeypatch.setattr(live_observe, "tmux_server_pid", lambda p: 1)
    monkeypatch.setattr(live_observe, "proc_start_ticks", lambda pid: "9")
    monkeypatch.setattr(live_observe, "proc_exe_identity", lambda pid: ("/usr/bin/claude", "5", "6"))
    monkeypatch.setattr(
        live_observe,
        "list_panes",
        lambda socket_path=None: [PaneInfo("%7", "s:0.0", "claude", str(ws), "t")],
    )
    monkeypatch.setattr(live_observe, "pane_field", lambda *a, **k: "40")
    monkeypatch.setattr(
        live_observe,
        "pane_processes",
        lambda pane_pid: [ProcessInfo(40, 1, "bash"), ProcessInfo(41, 40, "/usr/bin/claude")],
    )
    monkeypatch.setattr(live_observe, "_claude_agent_kind", lambda pid: "claude-pro")
    monkeypatch.setattr(live_observe, "_claude_profile_root", lambda pid: str(pro_root))
    monkeypatch.setattr(live_observe, "_expected_session_for_claude", lambda *a, **k: "BOOTSTRAP")
    monkeypatch.setattr(live_observe, "pane_mode_is_normal", lambda *a, **k: True)

    rows = live_observe.observe_socket_candidates(str(sock), "claude-pro", host_key="hk", uid="1000")
    assert len(rows) == 1
    assert rows[0]["identityEvidence"]["agentKind"] == "claude-pro"
    assert rows[0]["profileRoot"] == str(pro_root)
    assert rows[0]["expectedSession"] == "BOOTSTRAP"
    assert rows[0]["processState"] == "UP"


def test_live_observe_maps_claude_team_config_dir(monkeypatch, tmp_path):
    sock = tmp_path / "s.sock"
    team_root = (tmp_path / ".claude-team").resolve()
    team_root.mkdir()
    ws = tmp_path / "ws"
    ws.mkdir()

    monkeypatch.setattr(live_observe, "socket_file_identity", lambda p: (str(sock), "1", "2"))
    monkeypatch.setattr(live_observe, "read_boot_id", lambda: "boot")
    monkeypatch.setattr(live_observe, "tmux_server_pid", lambda p: 1)
    monkeypatch.setattr(live_observe, "proc_start_ticks", lambda pid: "9")
    monkeypatch.setattr(live_observe, "proc_exe_identity", lambda pid: ("/usr/bin/claude", "5", "6"))
    monkeypatch.setattr(
        live_observe,
        "list_panes",
        lambda socket_path=None: [PaneInfo("%8", "s:0.0", "claude", str(ws), "t")],
    )
    monkeypatch.setattr(live_observe, "pane_field", lambda *a, **k: "50")
    monkeypatch.setattr(
        live_observe,
        "pane_processes",
        lambda pane_pid: [ProcessInfo(50, 1, "bash"), ProcessInfo(51, 50, "/usr/bin/claude")],
    )
    monkeypatch.setattr(live_observe, "_claude_agent_kind", lambda pid: "claude-team")
    monkeypatch.setattr(live_observe, "_claude_profile_root", lambda pid: str(team_root))
    monkeypatch.setattr(live_observe, "_expected_session_for_claude", lambda *a, **k: "BOOTSTRAP")
    monkeypatch.setattr(live_observe, "pane_mode_is_normal", lambda *a, **k: True)

    rows = live_observe.observe_socket_candidates(str(sock), "claude-team", host_key="hk", uid="1000")
    assert len(rows) == 1
    assert rows[0]["identityEvidence"]["agentKind"] == "claude-team"
    assert rows[0]["profileRoot"] == str(team_root)
    assert rows[0]["expectedSession"] == "BOOTSTRAP"
    assert rows[0]["processState"] == "UP"


def test_live_observe_unknown_claude_config_dir_non_issuable(monkeypatch, tmp_path):
    from actl.core import runtime

    sock = tmp_path / "s.sock"
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setattr(live_observe, "socket_file_identity", lambda p: (str(sock), "1", "2"))
    monkeypatch.setattr(live_observe, "read_boot_id", lambda: "boot")
    monkeypatch.setattr(live_observe, "tmux_server_pid", lambda p: 1)
    monkeypatch.setattr(live_observe, "proc_start_ticks", lambda pid: "9")
    monkeypatch.setattr(live_observe, "proc_exe_identity", lambda pid: ("/usr/bin/claude", "5", "6"))
    monkeypatch.setattr(
        live_observe,
        "list_panes",
        lambda socket_path=None: [PaneInfo("%1", "s:0.0", "claude", str(ws), "t")],
    )
    monkeypatch.setattr(live_observe, "pane_field", lambda *a, **k: "2")
    monkeypatch.setattr(
        live_observe,
        "pane_processes",
        lambda pane_pid: [ProcessInfo(2, 1, "/usr/bin/claude")],
    )
    monkeypatch.setattr(live_observe, "_claude_config_dir", lambda pid: "/tmp/not-a-supported-profile")
    monkeypatch.setattr(live_observe, "pane_mode_is_normal", lambda *a, **k: True)

    rows = live_observe.observe_socket_candidates(str(sock), None, host_key="hk", uid="1")
    assert len(rows) == 1
    assert rows[0]["identityEvidence"]["agentKind"] == "claude"
    assert rows[0]["processState"] == "DOWN"
    assert rows[0]["profileRoot"] is None
    assert rows[0]["expectedSession"] is None
    cand = runtime._candidate_from_observation(rows[0], runtime.observed_at_now())
    assert cand["issuable"] is False
    assert cand["capabilities"]["managed.collect.final"] is False
