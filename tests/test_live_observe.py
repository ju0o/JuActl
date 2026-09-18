"""PHASE 1A-LIVE: live observe / snapshot / reserve-send binding tests.

Fixtures may fake /proc+tmux helpers. Final live qualification (separate script)
uses a real isolated Codex runtime and must not send a Worker prompt.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from actl.core import live_observe, runtime, tmux
from actl.core.models import PaneInfo
from actl.core.validation import ProcessInfo

REAL_TMUX = "/usr/bin/tmux"


def _patch_journal_root(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(runtime, "_journal_root_override", tmp_path)


def _identity(tmp_path: Path, **over):
    base = {
        "hostKey": "hk-live",
        "uid": "1000",
        "bootId": "boot-live",
        "socketPath": str(tmp_path / "sock"),
        "socketDev": "1",
        "socketInode": "2",
        "serverPid": "10",
        "serverStartTicks": "100",
        "paneId": "%1",
        "panePid": "11",
        "paneStartTicks": "101",
        "agentKind": "codex",
        "agentPid": "12",
        "agentStartTicks": "102",
        "agentExePath": "/usr/bin/codex",
        "agentExeDev": "3",
        "agentExeInode": "4",
    }
    base.update(over)
    return base


def _obs(tmp_path: Path, **over):
    identity = over.pop("identityEvidence", None) or _identity(tmp_path)
    row = {
        "identityEvidence": identity,
        "processState": "UP",
        "profileRoot": str(tmp_path / ".codex"),
        "workspaceRoot": str(tmp_path / "ws"),
        "expectedSession": "BOOTSTRAP",
        "mappingState": "UNMAPPED",
    }
    row.update(over)
    return row


def test_observe_candidates_delegates_to_live(monkeypatch, tmp_path):
    called = {}

    def fake(socket_path, agent_kind=None, *, host_key, uid):
        called["socket_path"] = socket_path
        called["agent_kind"] = agent_kind
        called["host_key"] = host_key
        called["uid"] = uid
        return [_obs(tmp_path)]

    monkeypatch.setattr(live_observe, "observe_socket_candidates", fake)
    monkeypatch.setattr(runtime, "local_host_key", lambda: "hk-x")
    monkeypatch.setattr(runtime, "local_uid", lambda: "42")
    out = runtime.observe_candidates(str(tmp_path / "sock"), "codex")
    assert len(out) == 1
    assert called["agent_kind"] == "codex"
    assert called["host_key"] == "hk-x"
    assert called["uid"] == "42"


def test_valid_live_codex_discovery_assembly(monkeypatch, tmp_path):
    sock = tmp_path / "proof.sock"
    sock.write_text("")  # placeholder; identity monkeypatched
    monkeypatch.setattr(live_observe, "socket_file_identity", lambda p: (str(sock), "10", "20"))
    monkeypatch.setattr(live_observe, "read_boot_id", lambda: "boot-z")
    monkeypatch.setattr(live_observe, "tmux_server_pid", lambda p: 77)
    monkeypatch.setattr(
        live_observe,
        "proc_start_ticks",
        lambda pid: {77: "1000", 88: "1001", 99: "1002"}[pid],
    )
    monkeypatch.setattr(
        live_observe,
        "proc_exe_identity",
        lambda pid: ("/opt/codex", "5", "6") if pid == 99 else None,
    )
    monkeypatch.setattr(
        live_observe,
        "list_panes",
        lambda socket_path=None: [PaneInfo("%3", "s:0.0", "codex", str(tmp_path / "ws"), "t")],
    )
    monkeypatch.setattr(live_observe, "pane_field", lambda pane_id, fmt, socket_path=None: "88")
    monkeypatch.setattr(
        live_observe,
        "pane_processes",
        lambda pane_pid: [
            ProcessInfo(88, 77, "bash"),
            ProcessInfo(99, 88, "/opt/codex --sandbox workspace-write -C " + str(tmp_path / "ws")),
        ],
    )
    monkeypatch.setattr(live_observe, "_codex_profile_root", lambda pid: str(tmp_path / ".codex"))
    monkeypatch.setattr(live_observe, "_expected_session_for_codex", lambda *a, **k: "BOOTSTRAP")
    monkeypatch.setattr(live_observe, "pane_mode_is_normal", lambda *a, **k: True)

    rows = live_observe.observe_socket_candidates(str(sock), "codex", host_key="hk", uid="1000")
    assert len(rows) == 1
    ev = rows[0]["identityEvidence"]
    assert ev["paneId"] == "%3"
    assert ev["agentKind"] == "codex"
    assert ev["agentPid"] == "99"
    assert ev["agentExePath"] == "/opt/codex"
    assert rows[0]["workspaceRoot"] == str((tmp_path / "ws").resolve())
    rid = runtime.runtime_id_from_identity(runtime.build_identity(**ev))
    assert rid.startswith("rt1_")


def test_wrong_agent_process_not_selected_as_codex(monkeypatch, tmp_path):
    sock = tmp_path / "s.sock"
    monkeypatch.setattr(live_observe, "socket_file_identity", lambda p: (str(sock), "1", "2"))
    monkeypatch.setattr(live_observe, "read_boot_id", lambda: "boot")
    monkeypatch.setattr(live_observe, "tmux_server_pid", lambda p: 1)
    monkeypatch.setattr(live_observe, "proc_start_ticks", lambda pid: "9")
    monkeypatch.setattr(
        live_observe,
        "list_panes",
        lambda socket_path=None: [PaneInfo("%1", "s:0.0", "claude", "/tmp", "t")],
    )
    monkeypatch.setattr(live_observe, "pane_field", lambda *a, **k: "2")
    monkeypatch.setattr(
        live_observe,
        "pane_processes",
        lambda pane_pid: [ProcessInfo(2, 1, "/usr/bin/claude")],
    )
    rows = live_observe.observe_socket_candidates(str(sock), "codex", host_key="hk", uid="1")
    assert rows == []


def test_stale_pid_missing_exe_marks_down(monkeypatch, tmp_path):
    sock = tmp_path / "s.sock"
    monkeypatch.setattr(live_observe, "socket_file_identity", lambda p: (str(sock), "1", "2"))
    monkeypatch.setattr(live_observe, "read_boot_id", lambda: "boot")
    monkeypatch.setattr(live_observe, "tmux_server_pid", lambda p: 1)
    monkeypatch.setattr(live_observe, "proc_start_ticks", lambda pid: "9" if pid != 5 else None)
    monkeypatch.setattr(live_observe, "proc_exe_identity", lambda pid: None)
    monkeypatch.setattr(
        live_observe,
        "list_panes",
        lambda socket_path=None: [PaneInfo("%1", "s:0.0", "codex", "/tmp", "t")],
    )
    monkeypatch.setattr(live_observe, "pane_field", lambda *a, **k: "4")
    monkeypatch.setattr(
        live_observe,
        "pane_processes",
        lambda pane_pid: [ProcessInfo(4, 1, "bash"), ProcessInfo(5, 4, "/usr/bin/codex")],
    )
    rows = live_observe.observe_socket_candidates(str(sock), "codex", host_key="hk", uid="1")
    assert len(rows) == 1
    assert rows[0]["processState"] == "DOWN"
    cand = runtime._candidate_from_observation(rows[0], runtime.observed_at_now())
    assert cand["issuable"] is False
    assert cand["runtimeId"] is None


def test_pane_disappearance_returns_empty(monkeypatch, tmp_path):
    sock = tmp_path / "s.sock"
    monkeypatch.setattr(live_observe, "socket_file_identity", lambda p: (str(sock), "1", "2"))
    monkeypatch.setattr(live_observe, "read_boot_id", lambda: "boot")
    monkeypatch.setattr(live_observe, "tmux_server_pid", lambda p: 1)
    monkeypatch.setattr(live_observe, "proc_start_ticks", lambda pid: "1")
    monkeypatch.setattr(live_observe, "list_panes", lambda socket_path=None: [])
    assert live_observe.observe_socket_candidates(str(sock), "codex", host_key="hk", uid="1") == []


def test_wrong_workspace_rejected_on_reserve(monkeypatch, tmp_path):
    _patch_journal_root(monkeypatch, tmp_path)
    identity = _identity(tmp_path)
    rid = runtime.runtime_id_from_identity(identity)
    monkeypatch.setattr(
        runtime,
        "observe_candidates",
        lambda *a, **k: [_obs(tmp_path, identityEvidence=identity, workspaceRoot=str(tmp_path / "ws-a"))],
    )
    monkeypatch.setattr(runtime, "_live_snapshot_for_candidate", lambda *a, **k: "snap")
    resp, code = runtime.handle_runtime_request({
        "contractVersion": 1,
        "requestId": str(uuid.uuid4()),
        "operation": "reserve",
        "action": "acquire",
        "runtimeId": rid,
        "mode": "MANAGED",
        "expectedContext": {"agentKind": "codex", "workspaceRoot": str(tmp_path / "ws-b")},
        "socketPath": str(tmp_path / "sock"),
        "hostKey": "hk-live",
        "uid": "1000",
    })
    assert code == 3 and resp["ok"] is False
    assert resp["error"]["code"] == "INVALID_ARGUMENT"
    assert "workspaceRoot" in resp["error"]["detail"]


def test_snapshot_hash_generation(monkeypatch, tmp_path):
    monkeypatch.setattr(live_observe, "capture_pane", lambda *a, **k: "hello\nworld")
    digest = live_observe.snapshot_hash_for_pane("%1", str(tmp_path / "sock"))
    assert digest == hashlib.sha256(b"hello\nworld").hexdigest()


def test_snapshot_change_before_send_fail_closed(monkeypatch, tmp_path):
    _patch_journal_root(monkeypatch, tmp_path)
    identity = _identity(tmp_path)
    rid = runtime.runtime_id_from_identity(identity)
    obs = _obs(tmp_path, identityEvidence=identity)
    monkeypatch.setattr(runtime, "observe_candidates", lambda *a, **k: [obs])
    monkeypatch.setattr(runtime, "_live_snapshot_for_candidate", lambda *a, **k: "snap-1")
    acq, code = runtime.handle_runtime_request({
        "contractVersion": 1,
        "requestId": str(uuid.uuid4()),
        "operation": "reserve",
        "action": "acquire",
        "runtimeId": rid,
        "mode": "MANAGED",
        "expectedContext": {
            "agentKind": "codex",
            "workspaceRoot": obs["workspaceRoot"],
            "profileRoot": obs["profileRoot"],
            "expectedSession": "BOOTSTRAP",
            "paneId": "%1",
        },
        "socketPath": str(tmp_path / "sock"),
        "hostKey": "hk-live",
        "uid": "1000",
    })
    assert code == 0
    grant = acq["data"]
    # Snapshot changes after reserve.
    monkeypatch.setattr(runtime, "_live_snapshot_for_candidate", lambda *a, **k: "snap-2")
    calls = []

    def no_transport(*a, **k):
        calls.append(1)
        raise AssertionError("transport must not run")

    monkeypatch.setattr(runtime, "transport_send_prompt", no_transport)
    send, code2 = runtime.handle_runtime_request({
        "contractVersion": 1,
        "requestId": str(uuid.uuid4()),
        "operation": "send",
        "runtimeId": rid,
        "expectedContext": grant["context"],
        "reservationId": grant["reservationId"],
        "leaseToken": grant["leaseToken"],
        "fence": grant["fence"],
        "commandId": "cmd1_live_snap",
        "wirePrompt": "hi",
        "promptSha256": runtime.sha256_hex(b"hi"),
        "observationCursor": {"kind": "BOOTSTRAP", "runtimeId": rid},
        "inputPermit": {
            "commandId": "cmd1_live_snap",
            "runtimeId": rid,
            "fence": grant["fence"],
            "paneMode": "normal",
            "confirmedAt": runtime.observed_at_now(),
            "snapshotHash": "snap-1",
        },
        "currentSnapshotHash": "snap-1",
        "socketPath": str(tmp_path / "sock"),
        "hostKey": "hk-live",
        "uid": "1000",
    })
    assert code2 == 2 and send["ok"] is False
    assert send["error"]["code"] == "INPUT_STATE_UNKNOWN"
    assert calls == []


def test_stale_runtime_on_socket_with_other_candidates_rejected(monkeypatch, tmp_path):
    _patch_journal_root(monkeypatch, tmp_path)
    identity = _identity(tmp_path)
    monkeypatch.setattr(runtime, "observe_candidates", lambda *a, **k: [_obs(tmp_path, identityEvidence=identity)])
    monkeypatch.setattr(runtime, "_live_snapshot_for_candidate", lambda *a, **k: "snap")
    resp, code = runtime.handle_runtime_request({
        "contractVersion": 1,
        "requestId": str(uuid.uuid4()),
        "operation": "reserve",
        "action": "acquire",
        "runtimeId": "rt1_stale_other",
        "mode": "MANAGED",
        "expectedContext": {"agentKind": "codex", "workspaceRoot": str(tmp_path / "ws")},
        "socketPath": str(tmp_path / "sock"),
        "hostKey": "hk-live",
        "uid": "1000",
    })
    assert code == 3 and resp["ok"] is False
    assert "not observed" in resp["error"]["detail"]


def test_stale_fence_still_busy(monkeypatch, tmp_path):
    _patch_journal_root(monkeypatch, tmp_path)
    acq, code = runtime.handle_runtime_request({
        "contractVersion": 1,
        "requestId": str(uuid.uuid4()),
        "operation": "reserve",
        "action": "acquire",
        "runtimeId": "rt1_fence_live",
        "mode": "MANAGED",
        "expectedContext": {"agentKind": "codex", "workspaceRoot": "/tmp/ws"},
        "socketPath": str(tmp_path / "sock"),
        "hostKey": "hk-live",
        "uid": "1000",
    })
    assert code == 0
    data = acq["data"]
    bad, code2 = runtime.handle_runtime_request({
        "contractVersion": 1,
        "requestId": str(uuid.uuid4()),
        "operation": "reserve",
        "action": "renew",
        "reservationId": data["reservationId"],
        "leaseToken": data["leaseToken"],
        "fence": "999",
        "socketPath": str(tmp_path / "sock"),
        "hostKey": "hk-live",
        "uid": "1000",
    })
    assert code2 == 2 and bad["error"]["code"] == "BUSY"


def test_direct_managed_conflict_preserved(monkeypatch, tmp_path):
    """Managed hold still blocks Direct writer guard (no weakening)."""
    _patch_journal_root(monkeypatch, tmp_path)
    identity = _identity(tmp_path, paneId="%9")
    rid = runtime.runtime_id_from_identity(identity)
    obs = _obs(tmp_path, identityEvidence=identity)
    monkeypatch.setattr(runtime, "observe_candidates", lambda *a, **k: [obs])
    monkeypatch.setattr(runtime, "_live_snapshot_for_candidate", lambda *a, **k: "snap")
    acq, code = runtime.handle_runtime_request({
        "contractVersion": 1,
        "requestId": str(uuid.uuid4()),
        "operation": "reserve",
        "action": "acquire",
        "runtimeId": rid,
        "mode": "MANAGED",
        "expectedContext": {
            "agentKind": "codex",
            "workspaceRoot": obs["workspaceRoot"],
            "profileRoot": obs["profileRoot"],
            "expectedSession": "BOOTSTRAP",
            "paneId": "%9",
        },
        "socketPath": str(tmp_path / "sock"),
        "hostKey": "hk-live",
        "uid": "1000",
    })
    assert code == 0
    # Touch journal path for guard by ensuring socket resolves to same scope.
    monkeypatch.setattr(runtime, "canonical_tmux_socket_path", lambda socket_path=None: str(tmp_path / "sock"))
    monkeypatch.setattr(runtime, "local_host_key", lambda: "hk-live")
    monkeypatch.setattr(runtime, "local_uid", lambda: "1000")
    try:
        runtime.guard_tmux_writer(target="%9", socket_path=str(tmp_path / "sock"), mode="DIRECT")
        raised = False
    except runtime.WriterDenied as denied:
        raised = True
        assert denied.code == "BUSY"
    assert raised is True


def test_default_production_pane_not_selected_without_explicit_socket(monkeypatch):
    """observe_candidates never invents default-server candidates when socket missing."""
    monkeypatch.setattr(
        live_observe,
        "observe_socket_candidates",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not probe")),
    )
    # Nonexistent absolute socket → empty via socket_file_identity failure inside live path.
    out = runtime.observe_candidates("/tmp/actl-live-does-not-exist-" + uuid.uuid4().hex + ".sock", "codex")
    assert out == []


def test_reserve_returns_real_snapshot_and_frozen_pane(monkeypatch, tmp_path):
    _patch_journal_root(monkeypatch, tmp_path)
    identity = _identity(tmp_path, paneId="%7")
    rid = runtime.runtime_id_from_identity(identity)
    obs = _obs(tmp_path, identityEvidence=identity)
    monkeypatch.setattr(runtime, "observe_candidates", lambda *a, **k: [obs])
    monkeypatch.setattr(runtime, "_live_snapshot_for_candidate", lambda *a, **k: "real-snap-7")
    resp, code = runtime.handle_runtime_request({
        "contractVersion": 1,
        "requestId": str(uuid.uuid4()),
        "operation": "reserve",
        "action": "acquire",
        "runtimeId": rid,
        "mode": "MANAGED",
        "expectedContext": {
            "agentKind": "codex",
            "workspaceRoot": obs["workspaceRoot"],
            "profileRoot": obs["profileRoot"],
            "expectedSession": "BOOTSTRAP",
        },
        "socketPath": str(tmp_path / "sock"),
        "hostKey": "hk-live",
        "uid": "1000",
    })
    assert code == 0 and resp["ok"]
    assert resp["data"]["currentSnapshotHash"] == "real-snap-7"
    assert resp["data"]["context"]["paneId"] == "%7"
    assert resp["data"]["observationCursor"]["kind"] == "BOOTSTRAP"


def test_send_consumes_frozen_pane_binding(monkeypatch, tmp_path):
    _patch_journal_root(monkeypatch, tmp_path)
    identity = _identity(tmp_path, paneId="%4")
    rid = runtime.runtime_id_from_identity(identity)
    obs = _obs(tmp_path, identityEvidence=identity)
    monkeypatch.setattr(runtime, "observe_candidates", lambda *a, **k: [obs])
    monkeypatch.setattr(runtime, "_live_snapshot_for_candidate", lambda *a, **k: "snap-4")
    acq, code = runtime.handle_runtime_request({
        "contractVersion": 1,
        "requestId": str(uuid.uuid4()),
        "operation": "reserve",
        "action": "acquire",
        "runtimeId": rid,
        "mode": "MANAGED",
        "expectedContext": {
            "agentKind": "codex",
            "workspaceRoot": obs["workspaceRoot"],
            "profileRoot": obs["profileRoot"],
            "expectedSession": "BOOTSTRAP",
        },
        "socketPath": str(tmp_path / "sock"),
        "hostKey": "hk-live",
        "uid": "1000",
    })
    assert code == 0
    grant = acq["data"]
    sent = {}

    def fake_transport(pane_id, prompt, **kwargs):
        sent["pane_id"] = pane_id
        sent["socket_path"] = kwargs.get("socket_path")
        return {
            "ok": True,
            "sideEffect": "INPUT_OBSERVED",
            "deliveryDisposition": "TRANSPORT_SENT",
            "stages": [],
            "completedStages": ["enter"],
            "failedStage": None,
        }

    monkeypatch.setattr(runtime, "transport_send_prompt", fake_transport)
    monkeypatch.setattr(runtime, "observe_agent_received", lambda *a, **k: False)
    send, code2 = runtime.handle_runtime_request({
        "contractVersion": 1,
        "requestId": str(uuid.uuid4()),
        "operation": "send",
        "runtimeId": rid,
        "expectedContext": grant["context"],
        "reservationId": grant["reservationId"],
        "leaseToken": grant["leaseToken"],
        "fence": grant["fence"],
        "commandId": "cmd1_live_send",
        "wirePrompt": "ping",
        "promptSha256": runtime.sha256_hex(b"ping"),
        "observationCursor": grant["observationCursor"],
        "inputPermit": {
            "commandId": "cmd1_live_send",
            "runtimeId": rid,
            "fence": grant["fence"],
            "paneMode": "normal",
            "confirmedAt": runtime.observed_at_now(),
            "snapshotHash": "snap-4",
        },
        "currentSnapshotHash": "snap-4",
        "socketPath": str(tmp_path / "sock"),
        "hostKey": "hk-live",
        "uid": "1000",
    })
    assert code2 == 0 and send["ok"]
    assert sent["pane_id"] == "%4"
    assert sent["socket_path"] == str(tmp_path / "sock")


def test_disposable_tmux_fake_codex_discovery(monkeypatch):
    """Real tmux socket + process named codex (not production Codex prompt)."""
    assert Path(REAL_TMUX).is_file()
    work = Path(tempfile.mkdtemp(prefix="actl-live-", dir="/tmp"))
    sock = work / "t.sock"
    fake_bin = work / "codex"
    # Keep argv0 as .../codex (no exec-replace to sleep) so agent detection sees name=codex.
    fake_bin.write_text("#!/usr/bin/env python3\nimport time\ntime.sleep(120)\n", encoding="utf-8")
    fake_bin.chmod(0o755)
    env = {**os.environ, "PATH": f"{work}:{os.environ.get('PATH', '')}", "CODEX_HOME": str(work / ".codex")}
    (work / ".codex").mkdir()
    (work / "ws").mkdir()
    try:
        subprocess.run(
            [REAL_TMUX, "-S", str(sock), "new-session", "-d", "-s", "live", "-c", str(work / "ws"), str(fake_bin)],
            check=True,
            capture_output=True,
            text=True,
            env=env,
        )
        time.sleep(0.25)
        rows = live_observe.observe_socket_candidates(
            str(sock),
            "codex",
            host_key=runtime.local_host_key(),
            uid=runtime.local_uid(),
        )
        assert len(rows) == 1, rows
        assert rows[0]["processState"] == "UP"
        ev = rows[0]["identityEvidence"]
        assert ev["agentKind"] == "codex"
        assert ev["paneId"].startswith("%")
        # Shebang launches may show interpreter as exe (e.g. python3); argv still named codex.
        assert ev["agentExePath"]
        assert ev["agentPid"]
        rid = runtime.runtime_id_from_identity(runtime.build_identity(**ev))
        assert rid.startswith("rt1_")
        snap = live_observe.snapshot_hash_for_pane(ev["paneId"], str(sock))
        assert isinstance(snap, str) and len(snap) == 64
        # Ensure default server inventory was not required / not mutated by this socket.
        default = subprocess.run(
            [REAL_TMUX, "list-panes", "-a", "-F", "#{pane_id}"],
            capture_output=True,
            text=True,
        )
        assert default.returncode in (0, 1)
    finally:
        subprocess.run([REAL_TMUX, "-S", str(sock), "kill-server"], capture_output=True)
        shutil.rmtree(work, ignore_errors=True)
