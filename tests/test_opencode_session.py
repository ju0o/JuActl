"""Session-aware OpenCode binding: exact stored-session correlation, fail-closed."""
import json
import sqlite3
import time

from actl import cli
from actl.agents import opencode as oc
from actl.agents.extract import extract_last_response
from actl.agents.opencode import (
    OpenCodeSessionError,
    create_session,
    extract_opencode,
    live_cmdline_session,
    resolve_opencode,
    stored_session_id,
    valid_session_id,
)
from actl.core.discovery import Detection, reconcile
from actl.core.models import PaneInfo

BOUND = "ses_bound11111111111111111111"
NEWER = "ses_newer22222222222222222222"
STALE = "ses_stale33333333333333333333"
CWD = "/work/proj"

#: Mocked process start time (epoch ms) for bare-TUI adoption tests.
START = 1_000_000_000_000


def _db(root, sessions):
    db = root / "opencode.db"
    con = sqlite3.connect(db)
    con.executescript(
        """
      CREATE TABLE session (id TEXT PRIMARY KEY, directory TEXT, title TEXT, time_created INTEGER, time_updated INTEGER);
      CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER, data TEXT);
      CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT, time_created INTEGER, data TEXT);
    """
    )
    now = int(time.time() * 1000)
    for entry in sessions:
        if isinstance(entry, tuple) and len(entry) >= 3:
            sid, directory, updated = entry[0], entry[1], entry[2]
        else:
            sid, directory = entry
            updated = now
        con.execute(
            "INSERT INTO session VALUES (?, ?, ?, ?, ?)",
            (sid, directory, f"title-{sid}", now, updated),
        )
    con.commit()
    con.close()
    return root


def _msg(root, mid, sid, ts, role, finish):
    con = sqlite3.connect(root / "opencode.db")
    con.execute(
        "INSERT INTO message VALUES (?, ?, ?, ?)",
        (mid, sid, ts, json.dumps({"role": role, "finish": finish})),
    )
    con.commit()
    con.close()


def _part(root, pid, mid, sid, ts, ptype, text=""):
    con = sqlite3.connect(root / "opencode.db")
    payload = {"type": ptype}
    if ptype == "text":
        payload["text"] = text
    con.execute("INSERT INTO part VALUES (?, ?, ?, ?, ?)", (pid, mid, sid, ts, json.dumps(payload)))
    con.commit()
    con.close()


def _live(monkeypatch, argv):
    monkeypatch.setattr(oc, "_proc_args", lambda pid: argv)
    monkeypatch.setattr(oc, "pane_field", lambda target, fmt: CWD)


def test_valid_session_id_format():
    assert valid_session_id(BOUND)
    assert not valid_session_id("ses_short")
    assert not valid_session_id("latest")
    assert not valid_session_id(None)
    assert not valid_session_id("ses_f8048b1a0ffecJZS8ir1AQn3BZ; rm -rf /")


def test_stored_session_id_only_from_mapping():
    assert stored_session_id({"agents": {"opencode": {"target": "%7", "session_id": BOUND}}}) == BOUND
    assert stored_session_id({"agents": {"opencode": {"target": "%7"}}}) is None
    assert stored_session_id({}) is None
    assert stored_session_id(None) is None


def test_live_cmdline_session_parsing(monkeypatch):
    monkeypatch.setattr(oc, "_proc_args", lambda pid: ["opencode", "--session", BOUND, CWD])
    assert live_cmdline_session(1) == BOUND
    monkeypatch.setattr(oc, "_proc_args", lambda pid: ["opencode", "-s", BOUND])
    assert live_cmdline_session(1) == BOUND
    monkeypatch.setattr(oc, "_proc_args", lambda pid: ["opencode", f"--session={BOUND}"])
    assert live_cmdline_session(1) == BOUND
    monkeypatch.setattr(oc, "_proc_args", lambda pid: ["opencode", "--auto"])
    assert live_cmdline_session(1) is None
    monkeypatch.setattr(oc, "_proc_args", lambda pid: None)
    assert live_cmdline_session(1) is None


def test_exact_binding_beats_newest_unrelated_session(monkeypatch, tmp_path):
    _db(tmp_path, [(BOUND, CWD), (NEWER, "/other"), (STALE, CWD)])
    _msg(tmp_path, "m-bound", BOUND, 10, "assistant", "stop")
    _part(tmp_path, "p-bound", "m-bound", BOUND, 10, "text", "BOUND_OK")
    _msg(tmp_path, "m-newer", NEWER, 99, "assistant", "stop")
    _part(tmp_path, "p-newer", "m-newer", NEWER, 99, "text", "NEWER_TRAP")
    _live(monkeypatch, ["opencode", "--session", BOUND, CWD])
    resolution = resolve_opencode(tmp_path, "%7", agent_pid=4242, expected_session_id=BOUND)
    assert resolution.confidence == "exact"
    assert resolution.session_id == BOUND
    assert extract_opencode(resolution).text == "BOUND_OK"


def test_latest_completed_assistant_skips_streaming_and_nontext(monkeypatch, tmp_path):
    _db(tmp_path, [(BOUND, CWD)])
    _msg(tmp_path, "m1", BOUND, 1, "assistant", "stop")
    _part(tmp_path, "p1", "m1", BOUND, 1, "text", "FIRST")
    _msg(tmp_path, "mu", BOUND, 2, "user", "stop")
    _part(tmp_path, "pu", "mu", BOUND, 2, "text", "USER_TRAP")
    _msg(tmp_path, "m2", BOUND, 3, "assistant", "tool-calls")
    _part(tmp_path, "p2r", "m2", BOUND, 3, "reasoning", "THINK_TRAP")
    _part(tmp_path, "p2t", "m2", BOUND, 4, "tool", "TOOL_TRAP")
    _part(tmp_path, "p2x", "m2", BOUND, 5, "text", "SECOND")
    _part(tmp_path, "p2p", "m2", BOUND, 6, "patch", "PATCH_TRAP")
    _msg(tmp_path, "m3", BOUND, 7, "assistant", None)
    _part(tmp_path, "p3", "m3", BOUND, 7, "text", "STREAMING_TRAP")
    _live(monkeypatch, ["opencode", "--session", BOUND])
    resolution = resolve_opencode(tmp_path, "%7", agent_pid=4242, expected_session_id=BOUND)
    assert extract_opencode(resolution).text == "SECOND"


def test_wrong_live_session_fails_closed(monkeypatch, tmp_path):
    _db(tmp_path, [(BOUND, CWD), (STALE, CWD)])
    _msg(tmp_path, "m", STALE, 5, "assistant", "stop")
    _part(tmp_path, "p", "m", STALE, 5, "text", "WRONG")
    _live(monkeypatch, ["opencode", "--session", STALE])
    resolution = resolve_opencode(tmp_path, "%7", agent_pid=4242, expected_session_id=BOUND)
    assert resolution.confidence == "none" and "wrong conversation" in resolution.detail
    assert extract_opencode(resolution) is None


def test_arbitrary_tui_without_session_flag_never_adopted(monkeypatch, tmp_path):
    # Bare TUI with NO session active during its lifetime -> fail closed.
    _db(tmp_path, [(BOUND, CWD, START - 60_000)])
    _msg(tmp_path, "m", BOUND, 5, "assistant", "stop")
    _part(tmp_path, "p", "m", BOUND, 5, "text", "GUESS_TRAP")
    _live(monkeypatch, ["opencode", "--auto"])
    monkeypatch.setattr(oc, "_proc_start_ms", lambda pid: START)
    resolution = resolve_opencode(tmp_path, "%7", agent_pid=4242, expected_session_id=BOUND)
    assert resolution.confidence == "none"
    assert "cannot adopt" in resolution.detail
    assert extract_opencode(resolution) is None


def test_bare_auto_tui_adopts_unique_active_session(monkeypatch, tmp_path):
    _db(tmp_path, [(BOUND, CWD, START + 5_000)])
    _msg(tmp_path, "m", BOUND, 10, "assistant", "stop")
    _part(tmp_path, "p", "m", BOUND, 10, "text", "AUTO_OK")
    _live(monkeypatch, ["opencode", "--auto"])
    monkeypatch.setattr(oc, "_proc_start_ms", lambda pid: START)
    resolution = resolve_opencode(tmp_path, "%7", agent_pid=4242, expected_session_id=BOUND)
    assert resolution.confidence == "exact"
    assert resolution.session_id == BOUND
    assert "adoption" in resolution.match_method
    assert extract_opencode(resolution).text == "AUTO_OK"


def test_bare_auto_tui_adopts_even_when_unbound(monkeypatch, tmp_path):
    _db(tmp_path, [(BOUND, CWD, START + 5_000)])
    _msg(tmp_path, "m", BOUND, 10, "assistant", "stop")
    _part(tmp_path, "p", "m", BOUND, 10, "text", "UNBOUND_OK")
    _live(monkeypatch, ["opencode", "--auto"])
    monkeypatch.setattr(oc, "_proc_start_ms", lambda pid: START)
    resolution = resolve_opencode(tmp_path, "%7", agent_pid=4242, expected_session_id=None)
    assert resolution.confidence == "exact"
    assert extract_opencode(resolution).text == "UNBOUND_OK"


def test_bare_auto_tui_stale_session_not_adopted(monkeypatch, tmp_path):
    # Updated only BEFORE this process started -> belongs to a previous run.
    _db(tmp_path, [(BOUND, CWD, START - 60_000)])
    _msg(tmp_path, "m", BOUND, 10, "assistant", "stop")
    _part(tmp_path, "p", "m", BOUND, 10, "text", "STALE_TRAP")
    _live(monkeypatch, ["opencode", "--auto"])
    monkeypatch.setattr(oc, "_proc_start_ms", lambda pid: START)
    resolution = resolve_opencode(tmp_path, "%7", agent_pid=4242, expected_session_id=BOUND)
    assert resolution.confidence == "none"
    assert extract_opencode(resolution) is None


def test_bare_auto_tui_multiple_active_auto_maps_to_newest(monkeypatch, tmp_path):
    # Two sessions in the same cwd active during this process -> auto-map to
    # whichever was touched most recently (only one can be on screen).
    _db(tmp_path, [(BOUND, CWD, START + 5_000), (NEWER, CWD, START + 4_000)])
    _msg(tmp_path, "m", BOUND, 10, "assistant", "stop")
    _part(tmp_path, "p", "m", BOUND, 10, "text", "NEWEST_OK")
    _live(monkeypatch, ["opencode", "--auto"])
    monkeypatch.setattr(oc, "_proc_start_ms", lambda pid: START)
    resolution = resolve_opencode(tmp_path, "%7", agent_pid=4242, expected_session_id=BOUND)
    assert resolution.confidence == "exact"
    assert resolution.session_id == BOUND
    assert extract_opencode(resolution).text == "NEWEST_OK"


def test_bare_auto_tui_adoption_respects_pane_cwd(monkeypatch, tmp_path):
    # Active session in a DIFFERENT directory must not be adopted for this pane.
    _db(tmp_path, [(BOUND, "/elsewhere", START + 5_000)])
    _live(monkeypatch, ["opencode", "--auto"])
    monkeypatch.setattr(oc, "_proc_start_ms", lambda pid: START)
    resolution = resolve_opencode(tmp_path, "%7", agent_pid=4242, expected_session_id=BOUND)
    assert resolution.confidence == "none"
    assert "cannot adopt" in resolution.detail


def test_process_restart_with_same_session_stays_exact(monkeypatch, tmp_path):
    _db(tmp_path, [(BOUND, CWD)])
    _msg(tmp_path, "m", BOUND, 5, "assistant", "stop")
    _part(tmp_path, "p", "m", BOUND, 5, "text", "RESTART_OK")
    _live(monkeypatch, ["opencode", "--session", BOUND])
    first = resolve_opencode(tmp_path, "%7", agent_pid=100, expected_session_id=BOUND)
    second = resolve_opencode(tmp_path, "%7", agent_pid=99999, expected_session_id=BOUND)
    assert first.confidence == "exact" and second.confidence == "exact"
    assert extract_opencode(second).text == "RESTART_OK"


def test_missing_row_cwd_mismatch_and_bad_id_fail_closed(monkeypatch, tmp_path):
    _db(tmp_path, [(BOUND, CWD)])
    _live(monkeypatch, ["opencode", "--session", BOUND])
    assert resolve_opencode(tmp_path, "%7", agent_pid=1, expected_session_id=NEWER).confidence == "none"
    monkeypatch.setattr(oc, "pane_field", lambda target, fmt: "/elsewhere")
    assert "cwd" in resolve_opencode(tmp_path, "%7", agent_pid=1, expected_session_id=BOUND).detail
    assert resolve_opencode(tmp_path, "%7", agent_pid=1, expected_session_id="newest").confidence == "none"
    assert resolve_opencode(tmp_path, "%7", agent_pid=1, expected_session_id=None).confidence == "none"


def test_legacy_path_and_unbound_extract_stay_blocked(tmp_path):
    assert extract_opencode(tmp_path) is None
    assert resolve_opencode(tmp_path, "%9").confidence == "none"


def test_reconcile_pane_change_drops_session_but_same_pane_keeps(monkeypatch):
    from actl.core import discovery as discovery_mod

    pane = PaneInfo("%3", "s:0.0", "opencode", CWD, "t")
    monkeypatch.setattr(discovery_mod, "validate_target", lambda *a: type("V", (), {"state": "DOWN"})())
    updated, _ = reconcile(
        {"agents": {"opencode": {"target": "%9", "session_id": BOUND}}},
        [Detection(pane, "opencode", "high", "pid 1: opencode")],
    )
    assert updated["agents"]["opencode"] == {"target": "%3"}
    monkeypatch.setattr(discovery_mod, "validate_target", lambda *a: type("V", (), {"state": "UP"})())
    kept, _ = reconcile(
        {"agents": {"opencode": {"target": "%3", "session_id": BOUND}}},
        [Detection(pane, "opencode", "high", "pid 1: opencode")],
    )
    assert kept["agents"]["opencode"] == {"target": "%3", "session_id": BOUND}


def test_bind_flow_verifies_before_persisting(monkeypatch, tmp_path):
    _db(tmp_path, [(BOUND, CWD)])
    _live(monkeypatch, ["opencode", "--session", BOUND])
    monkeypatch.setattr(cli, "validate_target", lambda *a: type("V", (), {"valid": True, "agent_pid": 7, "detail": ""})())
    monkeypatch.setattr(oc, "session_row", lambda root, sid: {"id": sid, "directory": CWD, "title": "t"})
    config = {"agents": {"opencode": {"target": "%7"}}}
    bound = cli._bind_opencode_session(config, "%7", BOUND)
    assert bound["agents"]["opencode"] == {"target": "%7", "session_id": BOUND}
    assert config == {"agents": {"opencode": {"target": "%7"}}}
    _live(monkeypatch, ["opencode", "--auto"])
    assert cli._bind_opencode_session(config, "%7", BOUND) is None
    assert cli._bind_opencode_session(config, "%7", "bogus") is None


def test_copy_uses_bound_session_not_newest(monkeypatch, tmp_path):
    from actl.agents import extract as extract_mod

    _db(tmp_path, [(BOUND, CWD), (NEWER, CWD)])
    _msg(tmp_path, "m1", BOUND, 1, "assistant", "stop")
    _part(tmp_path, "p1", "m1", BOUND, 1, "text", "BOUND_OK")
    _msg(tmp_path, "m2", NEWER, 99, "assistant", "stop")
    _part(tmp_path, "p2", "m2", NEWER, 99, "text", "NEWER_TRAP")
    _live(monkeypatch, ["opencode", "--session", BOUND])
    monkeypatch.setattr(
        extract_mod, "validate_target", lambda *_: type("V", (), {"valid": True, "agent_pid": 7, "detail": ""})(),
    )
    monkeypatch.setattr(oc, "session_row", lambda root, sid: {"id": sid, "directory": CWD, "title": "t"})
    import actl.agents.opencode as oc_mod

    monkeypatch.setattr(oc_mod, "_db_path", lambda root: tmp_path / "opencode.db")
    config = {"agents": {"opencode": {"target": "%7", "session_id": BOUND}}}
    result = extract_last_response("opencode", "%7", config)
    assert result.text == "BOUND_OK" and result.confidence == "exact"
    assert extract_last_response("opencode", "%7", {"agents": {"opencode": {"target": "%7"}}}).source == "opencode-blocked"


class _FakeStdio:
    def __init__(self, lines):
        self._lines = list(lines)
        self.written = []

    def write(self, data):
        self.written.append(data)

    def flush(self):
        pass

    def readline(self):
        return self._lines.pop(0) if self._lines else ""


class _FakeProc:
    def __init__(self, replies):
        self.stdin = _FakeStdio([])
        self.stdout = _FakeStdio(replies)
        self.stderr = _FakeStdio([])
        self.terminated = False
        self.waited = False

    def terminate(self):
        self.terminated = True

    def wait(self):
        self.waited = True


def _acp(monkeypatch, replies, *, fail_spawn=False):
    import subprocess as sp

    holders = {}

    def fake_popen(*args, **kwargs):
        holders["args"] = args[0]
        if fail_spawn:
            raise FileNotFoundError("no opencode")
        proc = _FakeProc(replies)
        holders["proc"] = proc
        return proc

    monkeypatch.setattr(sp, "Popen", fake_popen)
    import select as select_mod

    monkeypatch.setattr(select_mod, "select", lambda r, w, x, t=None: (r, w, x))
    return holders


def test_create_session_via_acp_without_prompt(monkeypatch, tmp_path):
    import json as json_mod

    replies = [
        json_mod.dumps({"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": 1}}) + "\n",
        json_mod.dumps({"jsonrpc": "2.0", "id": 2, "result": {"sessionId": BOUND}}) + "\n",
    ]
    holders = _acp(monkeypatch, replies)
    assert create_session(tmp_path) == BOUND
    assert holders["args"][:3] == ["opencode", "acp", "--cwd"]
    sent = "".join(holders["proc"].stdin.written)
    assert "session/new" in sent and "initialize" in sent
    assert "prompt" not in sent
    assert holders["proc"].terminated and holders["proc"].waited


def test_create_session_reaps_acp_process_on_failure(monkeypatch, tmp_path):
    replies = [json.dumps({"jsonrpc": "2.0", "id": 1, "error": "boom"}) + "\n"]
    holders = _acp(monkeypatch, replies)
    try:
        create_session(tmp_path)
    except OpenCodeSessionError:
        pass
    else:
        raise AssertionError("create_session should fail")
    assert holders["proc"].terminated and holders["proc"].waited


def test_create_session_rejects_invalid_or_missing_id(monkeypatch, tmp_path):
    import json as json_mod

    replies = [
        json_mod.dumps({"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": 1}}) + "\n",
        json_mod.dumps({"jsonrpc": "2.0", "id": 2, "result": {"sessionId": "newest"}}) + "\n",
    ]
    holders = _acp(monkeypatch, replies)
    try:
        create_session(tmp_path)
    except OpenCodeSessionError:
        pass
    else:
        raise AssertionError("invalid sessionId must be rejected")
    _acp(monkeypatch, [], fail_spawn=True)
    try:
        create_session(tmp_path)
    except OpenCodeSessionError:
        pass
    else:
        raise AssertionError("missing binary must fail closed")


NEWBOUND = "ses_newbound44444444444444444"
OLDSTORED = "ses_oldstored5555555555555555"


def _up_validation(**over):
    fields = {
        "state": "UP", "target": "%2", "pane_id": "%2", "pane_pid": 99,
        "command": "opencode", "path": CWD, "agent_pid": 7, "detail": "",
    }
    fields.update(over)
    valid = fields.pop("valid", fields["state"] == "UP")
    fields["valid"] = valid
    return type("V", (), fields)()


NEWBOUND = "ses_newbound44444444444444444"
OLDSTORED = "ses_oldstored5555555555555555"


def _up_validation(**over):
    fields = {
        "state": "UP", "target": "%2", "pane_id": "%2", "pane_pid": 99,
        "command": "opencode", "path": CWD, "agent_pid": 7, "detail": "",
    }
    fields.update(over)
    valid = fields.pop("valid", fields["state"] == "UP")
    fields["valid"] = valid
    return type("V", (), fields)()


def _exact(session_id):
    return oc.OpenCodeResolution(
        session_id=session_id, storage_path=None, match_method="m", confidence="exact",
        detail="", live_session_id=session_id, foreground_pid=7, tty=None,
    )


def _fail(detail):
    return oc.OpenCodeResolution(detail=detail)


def test_bind_one_command_success(monkeypatch, tmp_path):
    calls = {"keys": [], "saved": []}
    monkeypatch.setattr(cli, "validate_target", lambda *a: _up_validation())
    monkeypatch.setattr(cli, "_pane_opencode_pids", lambda pid: [7])
    monkeypatch.setattr(oc, "live_cmdline_session", lambda pid: None)
    monkeypatch.setattr(cli, "_confirm", lambda prompt: True)
    monkeypatch.setattr(oc, "create_session", lambda cwd: NEWBOUND)
    monkeypatch.setattr(cli, "send_keys", lambda target, *keys: calls["keys"].append((target, keys)))
    monkeypatch.setattr(cli, "_wait_while", lambda pred, timeout, interval=0.5: True)
    monkeypatch.setattr(cli, "_wait_live_session", lambda sid, pane_pid, timeout=30.0: 8)
    monkeypatch.setattr(oc, "resolve_opencode", lambda *a: _fail("unbound") if len(calls["keys"]) == 0 else _exact(NEWBOUND))
    monkeypatch.setattr(cli, "backup_config", lambda: "bak")
    monkeypatch.setattr(cli, "save_config", lambda cfg: calls["saved"].append(cfg))
    config = {"agents": {"opencode": {"target": "%2"}}}
    updated, code = cli._bind_opencode(config)
    assert code == 0
    assert updated["agents"]["opencode"] == {"target": "%2", "session_id": NEWBOUND}
    assert calls["saved"] == [updated]
    assert calls["keys"][0] == ("%2", ("C-c",))
    assert calls["keys"][1][0] == "%2" and NEWBOUND in calls["keys"][1][1][0]


def test_bind_already_bound_is_noop(monkeypatch):
    def _boom(*a):
        raise AssertionError("must not create or relaunch when already bound")

    monkeypatch.setattr(cli, "validate_target", lambda *a: _up_validation())
    monkeypatch.setattr(oc, "resolve_opencode", lambda *a: _exact(BOUND))
    monkeypatch.setattr(oc, "create_session", _boom)
    monkeypatch.setattr(cli, "send_keys", _boom)
    config = {"agents": {"opencode": {"target": "%2", "session_id": BOUND}}}
    updated, code = cli._bind_opencode(config)
    assert code == 0 and updated == config


def test_bind_stale_pane_rejected_without_write(monkeypatch):
    def _boom(*a):
        raise AssertionError("must not write on stale pane")

    monkeypatch.setattr(cli, "validate_target", lambda *a: _up_validation(state="DOWN", detail="gone"))
    monkeypatch.setattr(cli, "save_config", _boom)
    monkeypatch.setattr(oc, "create_session", _boom)
    config = {"agents": {"opencode": {"target": "%gone", "session_id": BOUND}}}
    updated, code = cli._bind_opencode(config)
    assert code == 1 and updated == config


def test_bind_mismatch_rebinds_new_session(monkeypatch):
    saved = []
    monkeypatch.setattr(cli, "validate_target", lambda *a: _up_validation())
    monkeypatch.setattr(cli, "_pane_opencode_pids", lambda pid: [7])
    monkeypatch.setattr(oc, "live_cmdline_session", lambda pid: None)
    monkeypatch.setattr(cli, "_confirm", lambda prompt: True)
    monkeypatch.setattr(oc, "create_session", lambda cwd: NEWBOUND)
    monkeypatch.setattr(cli, "send_keys", lambda target, *keys: None)
    monkeypatch.setattr(cli, "_wait_while", lambda pred, timeout, interval=0.5: True)
    monkeypatch.setattr(cli, "_wait_live_session", lambda sid, pane_pid, timeout=30.0: 8)
    seen = []
    def fake_resolve(root, target, pid, expected):
        seen.append(expected)
        return _fail("stale") if expected == OLDSTORED else _exact(NEWBOUND)
    monkeypatch.setattr(oc, "resolve_opencode", fake_resolve)
    monkeypatch.setattr(cli, "backup_config", lambda: "bak")
    monkeypatch.setattr(cli, "save_config", lambda cfg: saved.append(cfg))
    config = {"agents": {"opencode": {"target": "%2", "session_id": OLDSTORED}}}
    updated, code = cli._bind_opencode(config)
    assert code == 0
    assert updated["agents"]["opencode"]["session_id"] == NEWBOUND
    assert OLDSTORED not in seen[1:]


def test_bind_decline_sends_nothing_writes_nothing(monkeypatch):
    def _boom(*a):
        raise AssertionError("declined bind must not act")

    monkeypatch.setattr(cli, "validate_target", lambda *a: _up_validation())
    monkeypatch.setattr(cli, "_pane_opencode_pids", lambda pid: [7])
    monkeypatch.setattr(oc, "live_cmdline_session", lambda pid: None)
    monkeypatch.setattr(cli, "_confirm", lambda prompt: False)
    monkeypatch.setattr(oc, "resolve_opencode", lambda *a: _fail("unbound"))
    monkeypatch.setattr(oc, "create_session", _boom)
    monkeypatch.setattr(cli, "send_keys", _boom)
    monkeypatch.setattr(cli, "save_config", _boom)
    config = {"agents": {"opencode": {"target": "%2"}}}
    updated, code = cli._bind_opencode(config)
    assert code == 1 and updated == config


def test_bind_adopts_exact_cmdline_session_without_relaunch(monkeypatch):
    def _boom(*a):
        raise AssertionError("adopt path must not relaunch or create")

    monkeypatch.setattr(cli, "validate_target", lambda *a: _up_validation())
    monkeypatch.setattr(cli, "_pane_opencode_pids", lambda pid: [7])
    monkeypatch.setattr(oc, "live_cmdline_session", lambda pid: BOUND)
    monkeypatch.setattr(cli, "_confirm", lambda prompt: True)
    monkeypatch.setattr(oc, "resolve_opencode", lambda *a: _exact(BOUND) if a[3] == BOUND else _fail("stale"))
    monkeypatch.setattr(oc, "create_session", _boom)
    monkeypatch.setattr(cli, "send_keys", _boom)
    monkeypatch.setattr(cli, "backup_config", lambda: "bak")
    saved = []
    monkeypatch.setattr(cli, "save_config", lambda cfg: saved.append(cfg))
    config = {"agents": {"opencode": {"target": "%2"}}}
    updated, code = cli._bind_opencode(config)
    assert code == 0
    assert updated["agents"]["opencode"] == {"target": "%2", "session_id": BOUND}
    assert saved == [updated]


def test_copy_unbound_gives_one_line_guidance(tmp_path):
    resolution = resolve_opencode(tmp_path, "%9")
    assert resolution.confidence == "none"
    assert resolution.detail in {
        "OpenCode /copy needs a one-time session bind. Run: actl bind opencode",
        "opencode.db is not readable",
    }


def test_bind_never_selects_newest_session(monkeypatch, tmp_path):
    _db(tmp_path, [(NEWER, CWD)])
    _msg(tmp_path, "m", NEWER, 999, "assistant", "stop")
    _part(tmp_path, "p", "m", NEWER, 999, "text", "NEWER_TRAP")
    monkeypatch.setattr(cli, "validate_target", lambda *a: _up_validation())
    monkeypatch.setattr(cli, "_pane_opencode_pids", lambda pid: [7])
    monkeypatch.setattr(oc, "live_cmdline_session", lambda pid: None)
    monkeypatch.setattr(cli, "_confirm", lambda prompt: True)
    monkeypatch.setattr(oc, "create_session", lambda cwd: NEWBOUND)
    monkeypatch.setattr(cli, "send_keys", lambda target, *keys: None)
    monkeypatch.setattr(cli, "_wait_while", lambda pred, timeout, interval=0.5: True)
    monkeypatch.setattr(cli, "_wait_live_session", lambda sid, pane_pid, timeout=30.0: 8)
    monkeypatch.setattr(oc, "resolve_opencode", lambda *a: _exact(NEWBOUND) if a[3] == NEWBOUND else _fail("unbound"))
    monkeypatch.setattr(cli, "backup_config", lambda: "bak")
    monkeypatch.setattr(cli, "save_config", lambda cfg: None)
    updated, code = cli._bind_opencode({"agents": {"opencode": {"target": "%2"}}})
    assert code == 0
    assert updated["agents"]["opencode"]["session_id"] == NEWBOUND
