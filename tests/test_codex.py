import json

from actl.agents import codex
from actl.agents.codex import CodexResolution, extract_codex, resolve_codex
from actl.agents.extract import extract_last_response


def _rollout(path, session_id, cwd, events):
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {
            "type": "session_meta",
            "payload": {"session_id": session_id, "id": session_id, "cwd": str(cwd), "cli_version": "0.153.4"},
        },
        *events,
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    return path


def _completed(item_type, **item):
    return {"type": "event_msg", "payload": {"type": "item_completed", "item": {"type": item_type, **item}}}


def _agent(text, phase="final_answer"):
    return _completed("AgentMessage", phase=phase, content=[{"type": "Text", "text": text}])


def _task_complete(text, turn_id):
    return {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": turn_id, "last_agent_message": text}}


def _active(monkeypatch, root, rollout, cwd, session_id="active", locks=None):
    monkeypatch.setattr(codex, "_codex_process", lambda *_: 55)
    monkeypatch.setattr(codex, "_open_rollouts", lambda *_: [rollout])
    monkeypatch.setattr(codex, "_open_thread_locks", lambda *_: locks if locks is not None else [session_id])
    monkeypatch.setattr(codex, "_process_tty", lambda *_: "/dev/pts/9")
    monkeypatch.setattr(codex, "pane_field", lambda *_: str(cwd))
    return resolve_codex(root, "%1", 55)


def test_codex_process_owned_rollout_rejects_newer_unrelated_session(monkeypatch, tmp_path):
    cwd = tmp_path / "JuTell"
    cwd.mkdir()
    active = _rollout(
        tmp_path / "sessions" / "2026" / "09" / f"rollout-{cwd.name}-active.jsonl",
        "active",
        cwd,
        [_completed("UserMessage", content=[]), _agent("ACTIVE")],
    )
    newer = _rollout(
        tmp_path / "sessions" / "2026" / "09" / "rollout-other-newer.jsonl",
        "newer",
        tmp_path / "Other",
        [_agent("WRONG")],
    )
    newer.touch()
    resolution = _active(monkeypatch, tmp_path, active, cwd)
    assert resolution.confidence == "exact"
    assert extract_codex(resolution).text == "ACTIVE"


def test_codex_same_project_multiple_rollouts_require_unique_open_fd(monkeypatch, tmp_path):
    cwd = tmp_path / "JuTell"
    cwd.mkdir()
    one = _rollout(tmp_path / "sessions" / "rollout-one.jsonl", "one", cwd, [_agent("ONE")])
    two = _rollout(tmp_path / "sessions" / "rollout-two.jsonl", "two", cwd, [_agent("TWO")])
    monkeypatch.setattr(codex, "_codex_process", lambda *_: 55)
    monkeypatch.setattr(codex, "pane_field", lambda *_: str(cwd))
    monkeypatch.setattr(codex, "_open_rollouts", lambda *_: [one, two])
    assert resolve_codex(tmp_path, "%1", 55).confidence == "none"


def test_codex_fd_owned_rollout_is_decisive_but_lock_mismatch_fails_closed(monkeypatch, tmp_path):
    cwd = tmp_path / "JuTell"
    other = tmp_path / "Other"
    cwd.mkdir()
    other.mkdir()
    # FD ownership of the single open rollout proves the active conversation
    # even when the rollout cwd differs from the pane's reported cwd (Codex
    # may be launched from a parent dir or resume an older session).
    rollout = _rollout(tmp_path / "sessions" / "rollout-active.jsonl", "active", other, [_agent("OK")])
    assert _active(monkeypatch, tmp_path, rollout, cwd).confidence == "exact"
    # The matched session's own thread lock must be present; a process only
    # holding another thread's lock is not provably writing this session.
    good = _rollout(tmp_path / "sessions" / "rollout-active2.jsonl", "active", other, [_agent("YES")])
    assert _active(monkeypatch, tmp_path, good, cwd, locks=["other"]).confidence == "none"


def test_codex_extracts_completed_agent_messages_after_latest_user_only(tmp_path):
    cwd = tmp_path / "JuTell"
    cwd.mkdir()
    rollout = _rollout(
        tmp_path / "sessions" / "rollout-active.jsonl",
        "active",
        cwd,
        [
            _completed("UserMessage", content=[{"type": "text", "text": "OLD"}]),
            _agent("OLD_ANSWER"),
            _completed("UserMessage", content=[{"type": "text", "text": "NEW"}]),
            _completed("Reasoning", summary_text=["NO"]),
            _completed("CommandExecution", command="NO"),
            _agent("FIRST", phase="commentary"),
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "phase": "final_answer",
                    "content": [{"type": "output_text", "text": "PARTIAL_WITHOUT_ITEM_COMPLETED"}],
                },
            },
            _agent("FINAL", phase="final_answer"),
        ],
    )
    result = extract_codex(CodexResolution(session_id="active", rollout_path=rollout, confidence="exact"))
    assert result.text == "FIRST\nFINAL"


def test_codex_rejects_path_only_legacy_newest_file_api(tmp_path):
    cwd = tmp_path / "JuTell"
    cwd.mkdir()
    rollout = _rollout(tmp_path / "sessions" / "rollout-active.jsonl", "active", cwd, [_agent("NO")])
    assert extract_codex(tmp_path) is None
    assert extract_codex(rollout) is None


def test_codex_stale_previous_session_and_missing_process_fail_closed(monkeypatch, tmp_path):
    cwd = tmp_path / "JuTell"
    cwd.mkdir()
    _rollout(tmp_path / "sessions" / "rollout-stale.jsonl", "stale", cwd, [_agent("STALE")])
    monkeypatch.setattr(codex, "_codex_process", lambda *_: None)
    assert resolve_codex(tmp_path, "%1").confidence == "none"
    monkeypatch.setattr(codex, "_codex_process", lambda *_: 55)
    monkeypatch.setattr(codex, "pane_field", lambda *_: str(cwd))
    monkeypatch.setattr(codex, "_open_rollouts", lambda *_: [])
    # Unreadable process (no /proc start time) must fail closed, never guess.
    monkeypatch.setattr(codex, "_proc_start_ms", lambda pid: None)
    assert resolve_codex(tmp_path, "%1", 55).confidence == "none"


def test_codex_restart_with_new_pid_recorrelates(monkeypatch, tmp_path):
    # Killing and relaunching Codex changes the PID; /copy must re-correlate
    # to the new live process-owned rollout instead of caching the old one.
    cwd = tmp_path / "JuTell"
    cwd.mkdir()
    rollout = _rollout(
        tmp_path / "sessions" / "rollout-active.jsonl",
        "active",
        cwd,
        [_completed("UserMessage"), _agent("RESTART_OK")],
    )
    monkeypatch.setattr(codex, "_open_rollouts", lambda *_: [rollout])
    monkeypatch.setattr(codex, "_open_thread_locks", lambda *_: ["active"])
    monkeypatch.setattr(codex, "_process_tty", lambda *_: "/dev/pts/9")
    monkeypatch.setattr(codex, "pane_field", lambda *_: str(cwd))

    monkeypatch.setattr(codex, "_codex_process", lambda *_: 55)
    first = resolve_codex(tmp_path, "%1", 55)
    monkeypatch.setattr(codex, "_codex_process", lambda *_: 99999)
    second = resolve_codex(tmp_path, "%1", 99999)
    assert first.confidence == "exact" and second.confidence == "exact"
    assert extract_codex(second).text == "RESTART_OK"


def test_codex_adopts_active_rollout_when_fd_closed(monkeypatch, tmp_path):
    # Codex closes the rollout FD between events; adoption must pick the one
    # written within the process lifetime (cwd + active + unique).
    cwd = tmp_path / "JuTell"
    cwd.mkdir()
    import time as _time
    import os as _os
    start = int(_time.time() * 1000) - 5_000
    active = _rollout(
        tmp_path / "sessions" / "2026" / "09" / "rollout-active.jsonl",
        "active",
        cwd,
        [_completed("UserMessage"), _agent("ADOPT_OK")],
    )
    _os.utime(active, (start / 1000 + 4, start / 1000 + 4))
    stale = _rollout(
        tmp_path / "sessions" / "2026" / "09" / "rollout-stale.jsonl",
        "stale",
        cwd,
        [_agent("STALE_TRAP")],
    )
    _os.utime(stale, (start / 1000 - 60, start / 1000 - 60))

    monkeypatch.setattr(codex, "_codex_process", lambda *_: 55)
    monkeypatch.setattr(codex, "_open_rollouts", lambda *_: [])
    monkeypatch.setattr(codex, "_open_thread_locks", lambda *_: [])
    monkeypatch.setattr(codex, "_process_tty", lambda *_: "/dev/pts/9")
    monkeypatch.setattr(codex, "pane_field", lambda *_: str(cwd))
    monkeypatch.setattr(codex, "_proc_start_ms", lambda pid: start)
    resolution = resolve_codex(tmp_path, "%1", 55)
    assert resolution.confidence == "exact"
    assert "adoption" in resolution.match_method
    assert extract_codex(resolution).text == "ADOPT_OK"


def test_codex_adoption_fails_closed_on_multiple_active(monkeypatch, tmp_path):
    cwd = tmp_path / "JuTell"
    cwd.mkdir()
    import time as _time
    import os as _os
    start = int(_time.time() * 1000) - 5_000
    one = _rollout(tmp_path / "sessions" / "rollout-one.jsonl", "one", cwd, [_agent("ONE")])
    _os.utime(one, (start / 1000 + 4, start / 1000 + 4))
    two = _rollout(tmp_path / "sessions" / "rollout-two.jsonl", "two", cwd, [_agent("TWO")])
    _os.utime(two, (start / 1000 + 3, start / 1000 + 3))
    monkeypatch.setattr(codex, "_codex_process", lambda *_: 55)
    monkeypatch.setattr(codex, "_open_rollouts", lambda *_: [])
    monkeypatch.setattr(codex, "_proc_start_ms", lambda pid: start)
    monkeypatch.setattr(codex, "pane_field", lambda *_: str(cwd))
    resolution = resolve_codex(tmp_path, "%1", 55)
    assert resolution.confidence == "none"
    assert "Multiple Codex rollouts" in resolution.detail


def test_codex_adoption_requires_cwd_match(monkeypatch, tmp_path):
    cwd = tmp_path / "JuTell"
    cwd.mkdir()
    import time as _time
    import os as _os
    start = int(_time.time() * 1000) - 5_000
    other = _rollout(tmp_path / "sessions" / "rollout-other.jsonl", "other", tmp_path / "Other", [_agent("WRONG")])
    _os.utime(other, (start / 1000 + 4, start / 1000 + 4))
    monkeypatch.setattr(codex, "_codex_process", lambda *_: 55)
    monkeypatch.setattr(codex, "_open_rollouts", lambda *_: [])
    monkeypatch.setattr(codex, "_proc_start_ms", lambda pid: start)
    monkeypatch.setattr(codex, "pane_field", lambda *_: str(cwd))
    assert resolve_codex(tmp_path, "%1", 55).confidence == "none"


def test_extract_last_response_wires_codex_resolution(monkeypatch, tmp_path):
    from actl.agents import extract as extract_mod

    cwd = tmp_path / "JuTell"
    cwd.mkdir()
    rollout = _rollout(tmp_path / "sessions" / "rollout-active.jsonl", "active", cwd, [_completed("UserMessage"), _agent("WIRED")])
    resolution = CodexResolution(session_id="active", rollout_path=rollout, confidence="exact", match_method="test")
    monkeypatch.setattr(extract_mod, "validate_target", lambda *_: type("V", (), {"valid": True, "agent_pid": 55, "detail": ""})())
    monkeypatch.setattr(extract_mod, "resolve_codex", lambda *_: resolution)
    monkeypatch.setattr(extract_mod, "AGENTS", {"codex": type("S", (), {"data_dirs": (tmp_path,)})()})
    assert extract_last_response("codex", "%1").text == "WIRED"


# --- Managed APIs (Slice 5); must not alter Direct expectations above ---


def test_managed_refuses_adoption_when_fd_closed(monkeypatch, tmp_path):
    cwd = tmp_path / "JuTell"
    cwd.mkdir()
    import time as _time
    import os as _os

    start = int(_time.time() * 1000) - 5_000
    active = _rollout(
        tmp_path / "sessions" / "rollout-active.jsonl",
        "active",
        cwd,
        [_completed("UserMessage"), _agent("ADOPT_TRAP")],
    )
    _os.utime(active, (start / 1000 + 4, start / 1000 + 4))
    monkeypatch.setattr(codex, "_codex_process", lambda *_: 55)
    monkeypatch.setattr(codex, "_open_rollouts", lambda *_: [])
    monkeypatch.setattr(codex, "_open_thread_locks", lambda *_: [])
    monkeypatch.setattr(codex, "_proc_start_ms", lambda pid: start)
    monkeypatch.setattr(codex, "pane_field", lambda *_: str(cwd))
    # Direct still adopts:
    assert resolve_codex(tmp_path, "%1", 55).confidence == "exact"
    managed = codex.resolve_codex_managed(tmp_path, "%1", 55)
    assert managed.code == "AMBIGUOUS_SESSION"
    assert "adoption" in managed.detail.lower() or "refuses" in managed.detail.lower() or "No exact" in managed.detail


def test_managed_dynamic_panes_recreated_concurrent_and_stale_rollouts(monkeypatch, tmp_path):
    cwd = tmp_path / "JuTell"
    cwd.mkdir()
    stale = _rollout(tmp_path / "sessions" / "rollout-stale.jsonl", "stale", cwd, [_agent("STALE")])
    def events(text, turn):
        return [
            _completed("UserMessage", turn_id=turn, content=[{"type": "text", "text": "wire"}]),
            _completed("AgentMessage", phase="final_answer", turn_id=turn, content=[{"type": "Text", "text": text}]),
            _task_complete(text, turn),
        ]

    current = _rollout(tmp_path / "sessions" / "rollout-current.jsonl", "current", cwd, events("CURRENT", "turn-current"))
    recreated = _rollout(tmp_path / "sessions" / "rollout-recreated.jsonl", "recreated", cwd, events("RECREATED", "turn-recreated"))
    concurrent = _rollout(tmp_path / "sessions" / "rollout-concurrent.jsonl", "concurrent", cwd, events("CONCURRENT", "turn-concurrent"))
    locks = tmp_path / "thread-writer-locks"
    locks.mkdir()
    for session_id in ("current", "recreated", "concurrent"):
        (locks / f"{session_id}.lock").touch()

    pane_pids = {"%09": 70, "%17": 71, "%23": 72, "%31": 73}
    # The filesystem contains a stale rollout and all three live sessions;
    # only the selected pane's process-owned FD set may choose a rollout.
    owned = {
        70: [stale, locks / "stale.lock"],
        71: [current, locks / "current.lock"],
        72: [recreated, locks / "recreated.lock"],
        73: [concurrent, locks / "concurrent.lock"],
    }
    monkeypatch.setattr(codex, "pane_field", lambda pane, field: str(pane_pids[pane]) if field == "#{pane_pid}" else str(cwd))
    monkeypatch.setattr(codex, "pane_processes", lambda pid: [type("Process", (), {"pid": pid, "args": "/usr/bin/codex"})()])
    monkeypatch.setattr(codex, "_open_paths", lambda pid: owned[pid])

    def collect(pane, prompt, expected):
        resolution = codex.resolve_codex_managed(tmp_path, pane)
        assert resolution.code == "OK" and resolution.session_id == expected
        assert resolution.rollout_path != stale
        result = codex.collect_codex_final(
            path=resolution.rollout_path,
            cursor={"path": str(resolution.rollout_path), "byteOffset": 0},
            wire_prompt=prompt or "wire",
            command_id=f"cmd-{expected}",
            runtime_id=f"runtime-{expected}",
            prompt_sha256="prompt-sha",
        )
        assert result.code == "FINAL"
        return result.packet["rawFinalText"]

    assert "STALE" in stale.read_text(encoding="utf-8")
    assert collect("%17", "", "current") == "CURRENT"
    assert collect("%23", "", "recreated") == "RECREATED"
    assert collect("%31", "", "concurrent") == "CONCURRENT"


def test_managed_jsonl_incomplete_tail_and_malformed_and_inode(tmp_path):
    path = tmp_path / "rollout.jsonl"
    path.write_text('{"type":"ok"}\n{"type":"partial"', encoding="utf-8")
    st = path.stat()
    cursor = {"path": str(path), "dev": str(st.st_dev), "inode": str(st.st_ino), "byteOffset": 0, "prefixSha256": codex._prefix_sha256(path, 0)}
    forward = codex.read_jsonl_forward(path, cursor)
    assert forward.code == "OK"
    assert forward.pending_incomplete is True
    assert len(forward.events) == 1

    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"type":"ok"}\nNOT_JSON\n', encoding="utf-8")
    st2 = bad.stat()
    cursor2 = {"dev": str(st2.st_dev), "inode": str(st2.st_ino), "byteOffset": 0, "prefixSha256": codex._prefix_sha256(bad, 0)}
    assert codex.read_jsonl_forward(bad, cursor2).code == "SOURCE_INTEGRITY"

    swapped = tmp_path / "swap.jsonl"
    swapped.write_text('{"type":"ok"}\n', encoding="utf-8")
    st3 = swapped.stat()
    cursor3 = {"dev": str(st3.st_dev), "inode": "999999999", "byteOffset": 0, "prefixSha256": codex._prefix_sha256(swapped, 0)}
    assert codex.read_jsonl_forward(swapped, cursor3).code == "SOURCE_INTEGRITY"


def test_managed_bind_and_final_predicates(tmp_path):
    prompt = "wire-prompt-xyz"
    sid, turn = "sess1", "turn1"
    path = tmp_path / "rollout-sess1.jsonl"
    rows = [
        {"type": "session_meta", "payload": {"session_id": sid, "id": sid}},
        {
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "turn_id": turn,
                "session_id": sid,
                "item": {"type": "UserMessage", "turn_id": turn, "content": [{"type": "text", "text": prompt}]},
            },
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "turn_id": turn,
                "item": {
                    "type": "AgentMessage",
                    "phase": "commentary",
                    "id": "c1",
                    "content": [{"type": "Text", "text": "NO"}],
                },
            },
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "turn_id": turn,
                "item": {
                    "type": "AgentMessage",
                    "phase": "final_answer",
                    "id": "f1",
                    "content": [{"type": "Text", "text": "YES"}],
                },
            },
        },
        {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": turn, "last_agent_message": "YES"}},
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    st = path.stat()
    cursor = {"path": str(path), "dev": str(st.st_dev), "inode": str(st.st_ino), "byteOffset": 0, "prefixSha256": codex._prefix_sha256(path, 0)}
    forward = codex.read_jsonl_forward(path, cursor)
    binding = codex.bind_user_turn(cursor, prompt, forward.events, session_id=sid)
    assert binding.code == "OK" and binding.turn_id == turn
    result = codex.collect_codex_final(
        path=path,
        cursor=cursor,
        wire_prompt=prompt,
        command_id="cmd1_x",
        runtime_id="rt1_x",
        prompt_sha256="deadbeef",
        session_id=sid,
    )
    assert result.code == "FINAL"
    assert result.packet["rawFinalText"] == "YES"
    assert result.packet["finalItemId"] == "f1"

    # Duplicate matching user turns → ambiguous
    rows2 = rows[:1] + [
        rows[1],
        {
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "turn_id": "turn2",
                "session_id": sid,
                "item": {"type": "UserMessage", "turn_id": "turn2", "content": [{"type": "text", "text": prompt}]},
            },
        },
    ]
    path2 = tmp_path / "dup.jsonl"
    path2.write_text("\n".join(json.dumps(r) for r in rows2) + "\n", encoding="utf-8")
    st2 = path2.stat()
    cursor2 = {"byteOffset": 0, "dev": str(st2.st_dev), "inode": str(st2.st_ino), "prefixSha256": codex._prefix_sha256(path2, 0)}
    fwd2 = codex.read_jsonl_forward(path2, cursor2)
    assert codex.bind_user_turn(cursor2, prompt, fwd2.events, session_id=sid).code == "AMBIGUOUS_SESSION"
