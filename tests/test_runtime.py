"""Slice 1–2: runtime envelope, journal, reserve, identity, discover, status."""
from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path

from actl.core import runtime


def _scope(tmp_path: Path):
    return {"hostKey": "hk-test", "uid": "1000", "socketPath": str(tmp_path / "sock")}


def _complete_identity(tmp_path: Path, **overrides):
    base = {
        "hostKey": "hk-test",
        "uid": "1000",
        "bootId": "boot-aaa",
        "socketPath": str(tmp_path / "sock"),
        "socketDev": "64512",
        "socketInode": "12345",
        "serverPid": "200",
        "serverStartTicks": "1000",
        "paneId": "%1",
        "panePid": "201",
        "paneStartTicks": "1001",
        "agentKind": "codex",
        "agentPid": "202",
        "agentStartTicks": "1002",
        "agentExePath": "/usr/local/bin/codex",
        "agentExeDev": "64512",
        "agentExeInode": "999",
    }
    base.update(overrides)
    return base


def _observation(tmp_path: Path, **overrides):
    identity = overrides.pop("identityEvidence", None)
    if identity is None:
        identity = _complete_identity(tmp_path)
    obs = {
        "identityEvidence": identity,
        "processState": "UP",
        "profileRoot": str(tmp_path / ".codex"),
        "workspaceRoot": str(tmp_path / "ws"),
        "expectedSession": "BOOTSTRAP",
        "mappingState": "UNMAPPED",
    }
    obs.update(overrides)
    return obs


def _acquire_req(tmp_path: Path, request_id: str | None = None, runtime_id: str = "rt1_synthetic", **extra):
    body = {
        "contractVersion": 1,
        "requestId": request_id or str(uuid.uuid4()),
        "operation": "reserve",
        "action": "acquire",
        "runtimeId": runtime_id,
        "mode": "MANAGED",
        "expectedContext": {"agentKind": "codex", "workspaceRoot": "/tmp/ws"},
        "serverScope": _scope(tmp_path),
    }
    body.update(extra)
    return body


def _patch_journal_root(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(runtime, "_journal_root_override", tmp_path)


# --- V02-ish: envelope validation ---


def test_reserve_acquire_success(monkeypatch, tmp_path):
    _patch_journal_root(monkeypatch, tmp_path)
    resp, code = runtime.handle_runtime_request(_acquire_req(tmp_path))
    assert code == 0
    assert resp["ok"] is True
    assert resp["contractVersion"] == 1
    assert resp["error"] is None
    data = resp["data"]
    assert data["reservationId"].startswith("rsv_")
    assert isinstance(data["leaseToken"], str) and len(data["leaseToken"]) >= 32
    assert data["fence"] == "1"
    assert int(data["expiresBootNs"]) > 0
    assert data["inputState"] == "INPUT_STATE_UNKNOWN"
    # durable journal stores token hash only
    journal_path = runtime.journal_path_for_scope(
        runtime.scope_id_for_socket("hk-test", "1000", str(tmp_path / "sock"))
    )
    conn = sqlite3.connect(str(journal_path))
    row = conn.execute("SELECT lease_token_hash, state FROM reservations").fetchone()
    conn.close()
    assert row[1] == "HELD"
    assert row[0] == runtime.sha256_hex(data["leaseToken"].encode("utf-8"))
    assert row[0] != data["leaseToken"]


def test_bad_contract_version_rejected(monkeypatch, tmp_path):
    _patch_journal_root(monkeypatch, tmp_path)
    req = _acquire_req(tmp_path)
    req["contractVersion"] = 2
    resp, code = runtime.handle_runtime_request(req)
    assert code == 3
    assert resp["ok"] is False
    assert resp["error"]["code"] == "INVALID_ARGUMENT"
    assert resp["error"]["sideEffect"] == "NONE"


def test_unknown_top_level_field_rejected(monkeypatch, tmp_path):
    _patch_journal_root(monkeypatch, tmp_path)
    req = _acquire_req(tmp_path, unexpected="nope")
    resp, code = runtime.handle_runtime_request(req)
    assert code == 3
    assert resp["error"]["code"] == "INVALID_ARGUMENT"
    assert "unexpected" in resp["error"]["detail"]


def test_missing_request_id_rejected(monkeypatch, tmp_path):
    _patch_journal_root(monkeypatch, tmp_path)
    req = _acquire_req(tmp_path)
    del req["requestId"]
    resp, code = runtime.handle_runtime_request(req)
    assert code == 3
    assert resp["error"]["code"] == "INVALID_ARGUMENT"


def test_unsupported_operation_stub(monkeypatch, tmp_path):
    _patch_journal_root(monkeypatch, tmp_path)
    req = {
        "contractVersion": 1,
        "requestId": str(uuid.uuid4()),
        "operation": "purge",
        "serverScope": _scope(tmp_path),
    }
    resp, code = runtime.handle_runtime_request(req)
    assert code == 3
    assert resp["ok"] is False
    assert resp["error"]["code"] == "INVALID_ARGUMENT"


def test_canonical_json_and_sha256_helpers():
    raw = runtime.canonical_json({"b": 1, "a": 2})
    assert raw == b'{"a":2,"b":1}'
    assert runtime.sha256_hex(raw) == runtime.sha256_hex(b'{"a":2,"b":1}')


def test_boot_time_ns_falls_back_when_boottime_is_unavailable(monkeypatch):
    class WindowsClock:
        monotonic_ns = staticmethod(lambda: 123456789)

    monkeypatch.setattr(runtime, "time", WindowsClock)
    assert runtime.boot_time_ns() == 123456789


# --- V05-ish: concurrent acquire → BUSY ---


def test_second_acquire_busy(monkeypatch, tmp_path):
    _patch_journal_root(monkeypatch, tmp_path)
    first, code1 = runtime.handle_runtime_request(_acquire_req(tmp_path, runtime_id="rt1_shared"))
    assert code1 == 0 and first["ok"] is True
    second, code2 = runtime.handle_runtime_request(_acquire_req(tmp_path, runtime_id="rt1_shared"))
    assert code2 == 2
    assert second["ok"] is False
    assert second["error"]["code"] == "BUSY"
    assert second["error"]["sideEffect"] == "NONE"


# --- V06-ish: renew, stale fence, expiry ---


def test_renew_success(monkeypatch, tmp_path):
    _patch_journal_root(monkeypatch, tmp_path)
    clock = {"ns": 1_000_000_000}
    monkeypatch.setattr(runtime, "boot_time_ns", lambda: clock["ns"])
    acquired, code = runtime.handle_runtime_request(_acquire_req(tmp_path, runtime_id="rt1_renew"))
    assert code == 0
    data = acquired["data"]
    clock["ns"] = 5_000_000_000
    renew_req = {
        "contractVersion": 1,
        "requestId": str(uuid.uuid4()),
        "operation": "reserve",
        "action": "renew",
        "reservationId": data["reservationId"],
        "leaseToken": data["leaseToken"],
        "fence": data["fence"],
        "serverScope": _scope(tmp_path),
    }
    renewed, code2 = runtime.handle_runtime_request(renew_req)
    assert code2 == 0 and renewed["ok"] is True
    assert renewed["data"]["reservationId"] == data["reservationId"]
    assert renewed["data"]["fence"] == data["fence"]
    assert int(renewed["data"]["expiresBootNs"]) == clock["ns"] + runtime.DEFAULT_LEASE_NS


def test_stale_fence_rejected_on_renew(monkeypatch, tmp_path):
    _patch_journal_root(monkeypatch, tmp_path)
    acquired, _ = runtime.handle_runtime_request(_acquire_req(tmp_path, runtime_id="rt1_fence"))
    data = acquired["data"]
    renew_req = {
        "contractVersion": 1,
        "requestId": str(uuid.uuid4()),
        "operation": "reserve",
        "action": "renew",
        "reservationId": data["reservationId"],
        "leaseToken": data["leaseToken"],
        "fence": "999",
        "serverScope": _scope(tmp_path),
    }
    resp, code = runtime.handle_runtime_request(renew_req)
    assert code == 2
    assert resp["error"]["code"] == "BUSY"
    assert "fence" in resp["error"]["detail"]


def test_expired_held_blocks_new_acquire(monkeypatch, tmp_path):
    _patch_journal_root(monkeypatch, tmp_path)
    clock = {"ns": 10_000_000_000}
    monkeypatch.setattr(runtime, "boot_time_ns", lambda: clock["ns"])
    first, code = runtime.handle_runtime_request(_acquire_req(tmp_path, runtime_id="rt1_expire"))
    assert code == 0
    # Advance past 30s lease.
    clock["ns"] = 10_000_000_000 + runtime.DEFAULT_LEASE_NS + 1
    second, code2 = runtime.handle_runtime_request(_acquire_req(tmp_path, runtime_id="rt1_expire"))
    assert code2 == 2
    assert second["error"]["code"] == "BUSY"
    journal_path = runtime.journal_path_for_scope(
        runtime.scope_id_for_socket("hk-test", "1000", str(tmp_path / "sock"))
    )
    conn = sqlite3.connect(str(journal_path))
    state = conn.execute(
        "SELECT state FROM reservations WHERE reservation_id = ?",
        (first["data"]["reservationId"],),
    ).fetchone()[0]
    conn.close()
    assert state == "EXPIRED_HELD"


def test_release_side_effect_free_then_reacquire_increments_fence(monkeypatch, tmp_path):
    _patch_journal_root(monkeypatch, tmp_path)
    first, _ = runtime.handle_runtime_request(_acquire_req(tmp_path, runtime_id="rt1_rel"))
    data = first["data"]
    release_req = {
        "contractVersion": 1,
        "requestId": str(uuid.uuid4()),
        "operation": "reserve",
        "action": "release",
        "reservationId": data["reservationId"],
        "leaseToken": data["leaseToken"],
        "fence": data["fence"],
        "serverScope": _scope(tmp_path),
    }
    released, code = runtime.handle_runtime_request(release_req)
    assert code == 0 and released["data"]["state"] == "RELEASED"
    second, code2 = runtime.handle_runtime_request(_acquire_req(tmp_path, runtime_id="rt1_rel"))
    assert code2 == 0
    assert second["data"]["fence"] == "2"


# --- V09-ish: idempotency ---


def test_idempotent_replay_same_body(monkeypatch, tmp_path):
    _patch_journal_root(monkeypatch, tmp_path)
    req = _acquire_req(tmp_path, request_id="req-idem-1", runtime_id="rt1_idem")
    first, code1 = runtime.handle_runtime_request(req)
    second, code2 = runtime.handle_runtime_request(req)
    assert code1 == 0 and code2 == 0
    assert first == second
    assert first["data"]["reservationId"] == second["data"]["reservationId"]


def test_idempotent_conflict_on_body_change(monkeypatch, tmp_path):
    _patch_journal_root(monkeypatch, tmp_path)
    req = _acquire_req(tmp_path, request_id="req-idem-2", runtime_id="rt1_idem2")
    first, code1 = runtime.handle_runtime_request(req)
    assert code1 == 0
    altered = dict(req)
    altered["ownerRef"] = "other-owner"
    conflict, code2 = runtime.handle_runtime_request(altered)
    assert code2 == 3
    assert conflict["error"]["code"] == "CONFLICT"
    # original grant remains the only HELD row
    third, code3 = runtime.handle_runtime_request(_acquire_req(tmp_path, runtime_id="rt1_idem2"))
    assert code3 == 2 and third["error"]["code"] == "BUSY"
    assert first["data"]["reservationId"]


# --- V20-ish: fail closed ---


def test_wrong_user_version_fails_closed(monkeypatch, tmp_path):
    _patch_journal_root(monkeypatch, tmp_path)
    first, code = runtime.handle_runtime_request(_acquire_req(tmp_path, runtime_id="rt1_ver"))
    assert code == 0
    journal_path = runtime.journal_path_for_scope(
        runtime.scope_id_for_socket("hk-test", "1000", str(tmp_path / "sock"))
    )
    before = journal_path.read_bytes()
    conn = sqlite3.connect(str(journal_path))
    conn.execute("PRAGMA user_version=99")
    conn.close()
    poisoned = journal_path.read_bytes()
    resp, code2 = runtime.handle_runtime_request(_acquire_req(tmp_path, runtime_id="rt1_other"))
    assert code2 == 2
    assert resp["ok"] is False
    after = journal_path.read_bytes()
    assert after == poisoned
    assert after != b""
    # Must not have been replaced with a fresh empty schema DB.
    conn = sqlite3.connect(str(journal_path))
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    rows = conn.execute("SELECT COUNT(*) FROM reservations").fetchone()[0]
    conn.close()
    assert version == 99
    assert rows == 1
    assert len(before) > 0


def test_corrupt_journal_fails_closed_without_wipe(monkeypatch, tmp_path):
    _patch_journal_root(monkeypatch, tmp_path)
    scope_id = runtime.scope_id_for_socket("hk-test", "1000", str(tmp_path / "sock"))
    journal_path = runtime.journal_path_for_scope(scope_id)
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    marker = b"NOT_A_SQLITE_DATABASE_CORRUPT_MARKER_v20"
    journal_path.write_bytes(marker)
    resp, code = runtime.handle_runtime_request(_acquire_req(tmp_path, runtime_id="rt1_corrupt"))
    assert code == 2
    assert resp["ok"] is False
    assert journal_path.read_bytes() == marker


def test_scope_id_stable_for_same_socket(tmp_path):
    a = runtime.scope_id_for_socket("hk", 1000, "/tmp/sock")
    b = runtime.scope_id_for_socket("hk", "1000", "/tmp/sock")
    c = runtime.scope_id_for_socket("hk", 1000, "/tmp/other")
    assert a == b
    assert a != c
    assert len(a) == 64


# --- Slice 2: identity / discover / status (V03/V04/V19 partial) ---


def test_identity_pid_change_new_runtime_id(tmp_path):
    first = runtime.build_identity(**_complete_identity(tmp_path))
    second = runtime.build_identity(**_complete_identity(tmp_path, agentPid="303"))
    assert runtime.runtime_id_from_identity(first) != runtime.runtime_id_from_identity(second)
    # pane rename-only fields are outside identity: title changes do not affect id.
    again = runtime.build_identity(**_complete_identity(tmp_path))
    assert runtime.runtime_id_from_identity(first) == runtime.runtime_id_from_identity(again)
    assert all(isinstance(first[k], str) for k in runtime.IDENTITY_FIELDS)
    assert first["uid"] == "1000"
    assert first["agentPid"] == "202"


def test_incomplete_identity_not_issuable(monkeypatch, tmp_path):
    _patch_journal_root(monkeypatch, tmp_path)
    incomplete = _complete_identity(tmp_path)
    del incomplete["agentExeInode"]

    def fake_observe(socket_path, agent_kind=None):
        return [_observation(tmp_path, identityEvidence=incomplete)]

    monkeypatch.setattr(runtime, "observe_candidates", fake_observe)
    resp, code = runtime.handle_runtime_request({
        "contractVersion": 1,
        "requestId": str(uuid.uuid4()),
        "operation": "discover",
        "socketPath": str(tmp_path / "sock"),
        "hostKey": "hk-test",
        "uid": "1000",
    })
    assert code == 0 and resp["ok"] is True
    cand = resp["data"]["candidates"][0]
    assert cand["issuable"] is False
    assert cand["runtimeId"] is None
    assert "agentExeInode" in cand["missingIdentityFields"]


def test_discover_codex_capabilities_and_no_config_mutation(monkeypatch, tmp_path):
    _patch_journal_root(monkeypatch, tmp_path)
    fake_config = tmp_path / "config.json"
    fake_config.write_text('{"agents": {}}\n', encoding="utf-8")
    before = fake_config.read_bytes()

    def fake_observe(socket_path, agent_kind=None):
        return [
            _observation(tmp_path),
            _observation(
                tmp_path,
                identityEvidence=_complete_identity(tmp_path, agentKind="claude-pro", paneId="%2", agentPid="500"),
                profileRoot=str(tmp_path / ".claude-pro"),
            ),
        ]

    monkeypatch.setattr(runtime, "observe_candidates", fake_observe)
    monkeypatch.setattr(runtime, "local_host_key", lambda: "hk-test")
    monkeypatch.setattr(runtime, "local_uid", lambda: "1000")
    resp, code = runtime.handle_runtime_request({
        "contractVersion": 1,
        "requestId": str(uuid.uuid4()),
        "operation": "discover",
        "socketPath": str(tmp_path / "sock"),
    })
    assert code == 0
    assert resp["data"]["sideEffect"] == "NONE"
    by_kind = {c["agentKind"]: c for c in resp["data"]["candidates"]}
    assert by_kind["codex"]["issuable"] is True
    assert by_kind["codex"]["runtimeId"].startswith("rt1_")
    assert by_kind["codex"]["capabilities"]["managed.send"] is True
    assert by_kind["codex"]["capabilities"]["managed.collect.final"] is True
    assert by_kind["claude-pro"]["capabilities"]["managed.send"] is False
    assert by_kind["claude-pro"]["capabilities"]["managed.collect.final"] is True
    assert fake_config.read_bytes() == before


def test_status_up_is_not_ready(monkeypatch, tmp_path):
    _patch_journal_root(monkeypatch, tmp_path)
    identity = _complete_identity(tmp_path)
    rid = runtime.runtime_id_from_identity(identity)

    monkeypatch.setattr(
        runtime,
        "observe_candidates",
        lambda socket_path, agent_kind=None: [_observation(tmp_path, identityEvidence=identity)],
    )
    resp, code = runtime.handle_runtime_request({
        "contractVersion": 1,
        "requestId": str(uuid.uuid4()),
        "operation": "status",
        "runtimeId": rid,
        "socketPath": str(tmp_path / "sock"),
        "hostKey": "hk-test",
        "uid": "1000",
    })
    assert code == 0 and resp["ok"] is True
    assert resp["data"]["processState"] == "UP"
    assert resp["data"]["inputState"] == "INPUT_STATE_UNKNOWN"
    assert resp["data"]["inputState"] != "READY"


def test_status_hides_lease_token_when_held(monkeypatch, tmp_path):
    _patch_journal_root(monkeypatch, tmp_path)
    identity = _complete_identity(tmp_path)
    rid = runtime.runtime_id_from_identity(identity)
    obs = _observation(tmp_path, identityEvidence=identity)
    monkeypatch.setattr(
        runtime,
        "observe_candidates",
        lambda socket_path, agent_kind=None: [obs],
    )
    monkeypatch.setattr(runtime, "_live_snapshot_for_candidate", lambda *a, **k: "snap-held")
    acquired, code = runtime.handle_runtime_request(
        _acquire_req(
            tmp_path,
            runtime_id=rid,
            expectedContext={
                "agentKind": "codex",
                "workspaceRoot": obs["workspaceRoot"],
                "profileRoot": obs["profileRoot"],
                "expectedSession": obs["expectedSession"],
            },
        )
    )
    assert code == 0
    assert acquired["data"].get("currentSnapshotHash") == "snap-held"
    assert acquired["data"]["context"]["paneId"] == identity["paneId"]
    token = acquired["data"]["leaseToken"]
    status, code2 = runtime.handle_runtime_request({
        "contractVersion": 1,
        "requestId": str(uuid.uuid4()),
        "operation": "status",
        "runtimeId": rid,
        "socketPath": str(tmp_path / "sock"),
        "hostKey": "hk-test",
        "uid": "1000",
    })
    assert code2 == 0
    reservation = status["data"]["reservation"]
    assert reservation is not None
    assert reservation["fence"] == acquired["data"]["fence"]
    assert "leaseToken" not in reservation
    assert token not in json.dumps(status)


def test_status_unmapped_for_unknown_selector(monkeypatch, tmp_path):
    _patch_journal_root(monkeypatch, tmp_path)
    monkeypatch.setattr(runtime, "observe_candidates", lambda *a, **k: [])
    resp, code = runtime.handle_runtime_request({
        "contractVersion": 1,
        "requestId": str(uuid.uuid4()),
        "operation": "status",
        "selector": {"socketPath": str(tmp_path / "sock"), "paneId": "%9"},
        "hostKey": "hk-test",
        "uid": "1000",
    })
    assert code == 2
    assert resp["error"]["code"] == "UNMAPPED"


def test_status_down_for_missing_runtime(monkeypatch, tmp_path):
    _patch_journal_root(monkeypatch, tmp_path)
    monkeypatch.setattr(runtime, "observe_candidates", lambda *a, **k: [])
    resp, code = runtime.handle_runtime_request({
        "contractVersion": 1,
        "requestId": str(uuid.uuid4()),
        "operation": "status",
        "runtimeId": "rt1_missing",
        "socketPath": str(tmp_path / "sock"),
        "hostKey": "hk-test",
        "uid": "1000",
    })
    assert code == 2
    assert resp["error"]["code"] == "DOWN"


def test_status_mismatch_on_expected_context(monkeypatch, tmp_path):
    _patch_journal_root(monkeypatch, tmp_path)
    identity = _complete_identity(tmp_path)
    rid = runtime.runtime_id_from_identity(identity)
    monkeypatch.setattr(
        runtime,
        "observe_candidates",
        lambda socket_path, agent_kind=None: [_observation(tmp_path, identityEvidence=identity)],
    )
    resp, code = runtime.handle_runtime_request({
        "contractVersion": 1,
        "requestId": str(uuid.uuid4()),
        "operation": "status",
        "runtimeId": rid,
        "expectedContext": {"workspaceRoot": "/wrong/workspace"},
        "socketPath": str(tmp_path / "sock"),
        "hostKey": "hk-test",
        "uid": "1000",
    })
    assert code == 2
    assert resp["error"]["code"] == "MISMATCH"


def test_non_codex_managed_capability_unsupported_path(monkeypatch, tmp_path):
    _patch_journal_root(monkeypatch, tmp_path)
    identity = _complete_identity(tmp_path, agentKind="grok", agentExePath="/usr/bin/grok")
    monkeypatch.setattr(
        runtime,
        "observe_candidates",
        lambda socket_path, agent_kind=None: [
            _observation(tmp_path, identityEvidence=identity, profileRoot=str(tmp_path / ".grok"))
        ],
    )
    discovered, code = runtime.handle_runtime_request({
        "contractVersion": 1,
        "requestId": str(uuid.uuid4()),
        "operation": "discover",
        "socketPath": str(tmp_path / "sock"),
        "hostKey": "hk-test",
        "uid": "1000",
        "agentKind": "grok",
    })
    assert code == 0
    caps = discovered["data"]["candidates"][0]["capabilities"]
    assert caps["managed.send"] is False
    assert caps["managed.collect.final"] is True


# --- Slice 3: writer guard (V07/V11-ish) ---


def test_writer_permit_free_when_no_reservation(monkeypatch, tmp_path):
    _patch_journal_root(monkeypatch, tmp_path)
    # Ensure journal exists for scope.
    runtime.handle_runtime_request({
        "contractVersion": 1,
        "requestId": str(uuid.uuid4()),
        "operation": "discover",
        "socketPath": str(tmp_path / "sock"),
        "hostKey": "hk-test",
        "uid": "1000",
    })
    permit = runtime.require_writer_permit(
        runtime_id="rt1_free",
        socket_path=str(tmp_path / "sock"),
        mode="DIRECT",
        host_key="hk-test",
        uid="1000",
    )
    assert permit["state"] == "FREE"


def test_writer_permit_busy_when_managed_held(monkeypatch, tmp_path):
    _patch_journal_root(monkeypatch, tmp_path)
    acquired, code = runtime.handle_runtime_request(_acquire_req(tmp_path, runtime_id="rt1_guard"))
    assert code == 0
    try:
        runtime.require_writer_permit(
            runtime_id="rt1_guard",
            socket_path=str(tmp_path / "sock"),
            mode="DIRECT",
            host_key="hk-test",
            uid="1000",
        )
    except runtime.WriterDenied as denied:
        assert denied.code == "BUSY"
    else:
        raise AssertionError("expected WriterDenied BUSY")


def test_writer_permit_allows_matching_fence(monkeypatch, tmp_path):
    _patch_journal_root(monkeypatch, tmp_path)
    acquired, code = runtime.handle_runtime_request(_acquire_req(tmp_path, runtime_id="rt1_ok"))
    assert code == 0
    data = acquired["data"]
    permit = runtime.require_writer_permit(
        runtime_id="rt1_ok",
        socket_path=str(tmp_path / "sock"),
        mode="MANAGED",
        reservation_id=data["reservationId"],
        lease_token=data["leaseToken"],
        fence=data["fence"],
        host_key="hk-test",
        uid="1000",
    )
    assert permit["reservationId"] == data["reservationId"]
    assert permit["fence"] == data["fence"]


def test_writer_permit_stale_fence_busy(monkeypatch, tmp_path):
    _patch_journal_root(monkeypatch, tmp_path)
    acquired, code = runtime.handle_runtime_request(_acquire_req(tmp_path, runtime_id="rt1_stale"))
    assert code == 0
    data = acquired["data"]
    try:
        runtime.require_writer_permit(
            runtime_id="rt1_stale",
            socket_path=str(tmp_path / "sock"),
            mode="MANAGED",
            reservation_id=data["reservationId"],
            lease_token=data["leaseToken"],
            fence="999",
            host_key="hk-test",
            uid="1000",
        )
    except runtime.WriterDenied as denied:
        assert denied.code == "BUSY"
        assert "fence" in denied.detail
    else:
        raise AssertionError("expected WriterDenied for stale fence")


def test_writer_pre_send_hook_blocks_staged_send(monkeypatch, tmp_path):
    from actl.core import tmux

    _patch_journal_root(monkeypatch, tmp_path)
    acquired, code = runtime.handle_runtime_request(_acquire_req(tmp_path, runtime_id="rt1_hook"))
    assert code == 0
    hook = runtime.make_writer_pre_send_hook(
        runtime_id="rt1_hook",
        socket_path=str(tmp_path / "sock"),
        mode="DIRECT",
        host_key="hk-test",
        uid="1000",
    )
    calls = []
    monkeypatch.setattr(tmux, "target_exists", lambda target, socket_path=None: True)
    monkeypatch.setattr(tmux, "_run", lambda *a, **k: calls.append(a) or type("R", (), {"stdout": ""})())
    result = tmux.send_prompt_staged("%1", "blocked", pre_send_hook=hook)
    assert result["ok"] is False
    assert result["sideEffect"] == "NONE"
    assert calls == []


# --- Slice 4: send / interrupt journal coupling ---


def _prompt_sha(text: str) -> str:
    return runtime.sha256_hex(text.encode("utf-8"))


def _permit(command_id: str, runtime_id: str, fence: str, *, confirmed_at=None, snapshot="snap-1", pane_mode="normal"):
    return {
        "commandId": command_id,
        "runtimeId": runtime_id,
        "fence": fence,
        "paneMode": pane_mode,
        "snapshotHash": snapshot,
        "confirmedAt": confirmed_at if confirmed_at is not None else runtime.wall_time_s(),
    }


def _acquire_managed(monkeypatch, tmp_path, runtime_id="rt1_send", pane_id="%1"):
    _patch_journal_root(monkeypatch, tmp_path)
    ctx = {"agentKind": "codex", "workspaceRoot": str(tmp_path / "ws"), "paneId": pane_id}
    acquired, code = runtime.handle_runtime_request(
        _acquire_req(tmp_path, runtime_id=runtime_id, expectedContext=ctx)
    )
    assert code == 0
    return acquired["data"], ctx


def _send_req(tmp_path, grant, ctx, *, request_id=None, command_id=None, prompt="hello wire", **extra):
    command_id = command_id or ("cmd1_" + uuid.uuid4().hex)
    body = {
        "contractVersion": 1,
        "requestId": request_id or str(uuid.uuid4()),
        "operation": "send",
        "runtimeId": grant["runtimeId"],
        "expectedContext": ctx,
        "reservationId": grant["reservationId"],
        "leaseToken": grant["leaseToken"],
        "fence": grant["fence"],
        "commandId": command_id,
        "correlationDigest": "corr-1",
        "wirePrompt": prompt,
        "promptSha256": _prompt_sha(prompt),
        "observationCursor": {"kind": "BOOTSTRAP"},
        "inputPermit": _permit(command_id, grant["runtimeId"], grant["fence"]),
        "currentSnapshotHash": "snap-1",
        "serverScope": _scope(tmp_path),
    }
    body.update(extra)
    if "inputPermit" in extra:
        body["inputPermit"] = extra["inputPermit"]
    return body


def test_send_success_transport_once(monkeypatch, tmp_path):
    grant, ctx = _acquire_managed(monkeypatch, tmp_path, "rt1_send_ok")
    calls = []

    def fake_transport(target, prompt, **kwargs):
        calls.append({"target": target, "prompt": prompt, "socket": kwargs.get("socket_path")})
        if kwargs.get("pre_send_hook"):
            kwargs["pre_send_hook"]()
        return {
            "ok": True,
            "stages": [{"name": "enter", "ok": True}],
            "completedStages": ["verify_target", "load_buffer", "paste_buffer", "enter"],
            "sideEffect": "INPUT_OBSERVED",
            "deliveryDisposition": "TRANSPORT_SENT",
        }

    monkeypatch.setattr(runtime, "transport_send_prompt", fake_transport)
    req = _send_req(tmp_path, grant, ctx, command_id="cmd1_ok")
    resp, code = runtime.handle_runtime_request(req)
    assert code == 0 and resp["ok"] is True
    assert resp["data"]["command"]["stage"] == "TRANSPORT_SENT"
    assert resp["data"]["inputPerformed"] is True
    assert len(calls) == 1
    assert calls[0]["prompt"] == "hello wire"
    assert calls[0]["socket"] == str(tmp_path / "sock")


def test_send_expired_permit_input_state_unknown(monkeypatch, tmp_path):
    grant, ctx = _acquire_managed(monkeypatch, tmp_path, "rt1_permit")
    calls = []
    monkeypatch.setattr(runtime, "transport_send_prompt", lambda *a, **k: calls.append(1) or {"ok": True})
    monkeypatch.setattr(runtime, "wall_time_s", lambda: 1_000.0)
    cmd = "cmd1_permit"
    req = _send_req(
        tmp_path,
        grant,
        ctx,
        command_id=cmd,
        inputPermit=_permit(cmd, grant["runtimeId"], grant["fence"], confirmed_at=1_000.0 - 11),
    )
    resp, code = runtime.handle_runtime_request(req)
    assert code == 2
    assert resp["error"]["code"] == "INPUT_STATE_UNKNOWN"
    assert resp["error"]["sideEffect"] == "NONE"
    assert calls == []


def test_send_snapshot_mismatch_no_paste(monkeypatch, tmp_path):
    grant, ctx = _acquire_managed(monkeypatch, tmp_path, "rt1_snap")
    calls = []
    monkeypatch.setattr(runtime, "transport_send_prompt", lambda *a, **k: calls.append(1) or {"ok": True})
    cmd = "cmd1_snap"
    req = _send_req(
        tmp_path,
        grant,
        ctx,
        command_id=cmd,
        inputPermit=_permit(cmd, grant["runtimeId"], grant["fence"], snapshot="old"),
        currentSnapshotHash="new",
    )
    resp, code = runtime.handle_runtime_request(req)
    assert code == 2
    assert resp["error"]["code"] == "INPUT_STATE_UNKNOWN"
    assert calls == []


def test_send_idempotent_replay_and_conflict(monkeypatch, tmp_path):
    grant, ctx = _acquire_managed(monkeypatch, tmp_path, "rt1_idem_send")
    calls = []

    def fake_transport(*a, **k):
        calls.append(1)
        if k.get("pre_send_hook"):
            k["pre_send_hook"]()
        return {"ok": True, "completedStages": ["enter"], "sideEffect": "INPUT_OBSERVED", "stages": []}

    monkeypatch.setattr(runtime, "transport_send_prompt", fake_transport)
    req = _send_req(tmp_path, grant, ctx, request_id="req-send-1", command_id="cmd1_idem")
    first, c1 = runtime.handle_runtime_request(req)
    second, c2 = runtime.handle_runtime_request(req)
    assert c1 == 0 and c2 == 0
    assert first == second
    assert len(calls) == 1
    altered = dict(req)
    altered["wirePrompt"] = "changed"
    altered["promptSha256"] = _prompt_sha("changed")
    conflict, c3 = runtime.handle_runtime_request(altered)
    assert c3 == 3
    assert conflict["error"]["code"] == "CONFLICT"
    assert len(calls) == 1


def test_send_after_attempting_no_second_paste(monkeypatch, tmp_path):
    grant, ctx = _acquire_managed(monkeypatch, tmp_path, "rt1_once")
    calls = []

    def fake_transport(*a, **k):
        calls.append(1)
        if k.get("pre_send_hook"):
            k["pre_send_hook"]()
        return {"ok": True, "completedStages": ["enter"], "sideEffect": "INPUT_OBSERVED", "stages": []}

    monkeypatch.setattr(runtime, "transport_send_prompt", fake_transport)
    req = _send_req(tmp_path, grant, ctx, command_id="cmd1_once")
    first, c1 = runtime.handle_runtime_request(req)
    assert c1 == 0
    # New requestId, same immutable payload — must not paste again.
    again = _send_req(tmp_path, grant, ctx, command_id="cmd1_once")
    second, c2 = runtime.handle_runtime_request(again)
    assert c2 == 0
    assert second["data"]["inputPerformed"] is False
    assert second["data"]["command"]["stage"] == "TRANSPORT_SENT"
    assert len(calls) == 1
    assert first["data"]["inputPerformed"] is True


def test_send_failure_after_attempting_ambiguous(monkeypatch, tmp_path):
    grant, ctx = _acquire_managed(monkeypatch, tmp_path, "rt1_amb")
    calls = []

    def boom(*a, **k):
        calls.append(1)
        if k.get("pre_send_hook"):
            k["pre_send_hook"]()
        return {
            "ok": False,
            "error": "paste failed",
            "completedStages": ["load_buffer", "paste_buffer"],
            "failedStage": "enter",
            "sideEffect": "INPUT_OBSERVED",
            "deliveryDisposition": "DELIVERY_AMBIGUOUS",
        }

    monkeypatch.setattr(runtime, "transport_send_prompt", boom)
    req = _send_req(tmp_path, grant, ctx, command_id="cmd1_amb")
    resp, code = runtime.handle_runtime_request(req)
    assert code == 2
    assert resp["error"]["code"] == "DELIVERY_AMBIGUOUS"
    assert resp["data"]["command"]["stage"] == "DELIVERY_AMBIGUOUS"
    # Retry with new requestId must not resend.
    again = _send_req(tmp_path, grant, ctx, command_id="cmd1_amb")
    replay, code2 = runtime.handle_runtime_request(again)
    assert code2 == 0
    assert replay["data"]["inputPerformed"] is False
    assert len(calls) == 1


def _ok_send_transport(*a, **k):
    if k.get("pre_send_hook"):
        k["pre_send_hook"]()
    return {"ok": True, "completedStages": ["enter"], "sideEffect": "INPUT_OBSERVED", "stages": []}


def test_interrupt_after_final_no_ctrl_c(monkeypatch, tmp_path):
    grant, ctx = _acquire_managed(monkeypatch, tmp_path, "rt1_fin")
    monkeypatch.setattr(runtime, "transport_send_prompt", _ok_send_transport)
    send_req = _send_req(tmp_path, grant, ctx, command_id="cmd1_fin")
    assert runtime.handle_runtime_request(send_req)[1] == 0
    # Inject FINAL into journal.
    path = runtime.journal_path_for_scope(
        runtime.scope_id_for_socket("hk-test", "1000", str(tmp_path / "sock"))
    )
    with runtime.open_journal(path) as journal:
        journal.conn.execute("BEGIN IMMEDIATE")
        journal.update_command("cmd1_fin", stage="FINAL", result_id="res1_x")
        journal.add_receipt(
            command_id="cmd1_fin",
            request_id="inject",
            kind="FINAL",
            stage="FINAL",
            side_effect="NONE",
            payload={},
        )
        journal.conn.execute("COMMIT")
    interrupts = []

    def no_interrupt(*a, **k):
        interrupts.append(1)
        return {"ok": True}

    monkeypatch.setattr(runtime, "transport_interrupt", no_interrupt)
    resp, code = runtime.handle_runtime_request({
        "contractVersion": 1,
        "requestId": str(uuid.uuid4()),
        "operation": "interrupt",
        "commandId": "cmd1_fin",
        "runtimeId": grant["runtimeId"],
        "expectedContext": ctx,
        "reservationId": grant["reservationId"],
        "leaseToken": grant["leaseToken"],
        "fence": grant["fence"],
        "interruptRequestId": str(uuid.uuid4()),
        "reason": "stop",
        "serverScope": _scope(tmp_path),
    })
    assert code == 0
    assert resp["data"]["alreadyFinal"] is True
    assert resp["data"]["inputPerformed"] is False
    assert interrupts == []


def test_interrupt_once_then_no_extra_cc(monkeypatch, tmp_path):
    grant, ctx = _acquire_managed(monkeypatch, tmp_path, "rt1_int")
    monkeypatch.setattr(runtime, "transport_send_prompt", _ok_send_transport)
    assert runtime.handle_runtime_request(_send_req(tmp_path, grant, ctx, command_id="cmd1_int"))[1] == 0
    interrupts = []

    def fake_interrupt(*a, **k):
        interrupts.append(1)
        if k.get("pre_send_hook"):
            k["pre_send_hook"]()
        return {"ok": True, "sideEffect": "INPUT_OBSERVED", "completedStages": ["interrupt_ctrl_c"]}

    monkeypatch.setattr(runtime, "transport_interrupt", fake_interrupt)
    body = {
        "contractVersion": 1,
        "requestId": str(uuid.uuid4()),
        "operation": "interrupt",
        "commandId": "cmd1_int",
        "runtimeId": grant["runtimeId"],
        "expectedContext": ctx,
        "reservationId": grant["reservationId"],
        "leaseToken": grant["leaseToken"],
        "fence": grant["fence"],
        "interruptRequestId": "intr-1",
        "reason": "cancel",
        "serverScope": _scope(tmp_path),
    }
    first, c1 = runtime.handle_runtime_request(body)
    assert c1 == 0
    assert first["data"]["command"]["stage"] == "CANCEL_REQUESTED"
    assert first["data"]["inputPerformed"] is True
    second, c2 = runtime.handle_runtime_request({**body, "requestId": str(uuid.uuid4()), "interruptRequestId": "intr-2"})
    assert c2 == 0
    assert second["data"]["interruptAlreadyRecorded"] is True
    assert second["data"]["inputPerformed"] is False
    assert len(interrupts) == 1


def test_send_busy_without_reservation_match(monkeypatch, tmp_path):
    grant, ctx = _acquire_managed(monkeypatch, tmp_path, "rt1_busy_send")
    calls = []
    monkeypatch.setattr(runtime, "transport_send_prompt", lambda *a, **k: calls.append(1) or {"ok": True})
    # Different runtime held; attempt send on same runtime without matching token by forging fence.
    req = _send_req(tmp_path, grant, ctx, command_id="cmd1_busy")
    req["fence"] = "999"
    req["inputPermit"] = _permit("cmd1_busy", grant["runtimeId"], "999")
    resp, code = runtime.handle_runtime_request(req)
    assert code == 2
    assert resp["error"]["code"] == "BUSY"
    assert calls == []


# --- Slice 5: Managed Codex collect ---


def _cursor_for(path: Path, byte_offset: int = 0) -> dict:
    import hashlib

    st = path.stat()
    with path.open("rb") as fh:
        prefix = fh.read(byte_offset)
    return {
        "path": str(path),
        "dev": str(st.st_dev),
        "inode": str(st.st_ino),
        "byteOffset": byte_offset,
        "prefixSha256": hashlib.sha256(prefix).hexdigest(),
    }


def _write_rollout(path: Path, rows: list) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return path


def _user_msg(text: str, turn_id: str, session_id: str):
    return {
        "type": "event_msg",
        "payload": {
            "type": "item_completed",
            "turn_id": turn_id,
            "session_id": session_id,
            "item": {
                "type": "UserMessage",
                "turn_id": turn_id,
                "content": [{"type": "text", "text": text}],
            },
        },
    }


def _final_msg(text: str, turn_id: str, item_id: str = "final-1"):
    return {
        "type": "event_msg",
        "payload": {
            "type": "item_completed",
            "turn_id": turn_id,
            "item": {
                "type": "AgentMessage",
                "phase": "final_answer",
                "id": item_id,
                "turn_id": turn_id,
                "content": [{"type": "Text", "text": text}],
            },
        },
    }


def _commentary(text: str, turn_id: str):
    return {
        "type": "event_msg",
        "payload": {
            "type": "item_completed",
            "turn_id": turn_id,
            "item": {
                "type": "AgentMessage",
                "phase": "commentary",
                "turn_id": turn_id,
                "content": [{"type": "Text", "text": text}],
            },
        },
    }


def _task_complete(text: str, turn_id: str):
    return {
        "type": "event_msg",
        "payload": {
            "type": "task_complete",
            "turn_id": turn_id,
            "last_agent_message": text,
        },
    }


def _prepare_sent_command(
    monkeypatch, tmp_path, *, command_id: str, prompt: str, rollout: Path,
    runtime_id="rt1_collect", pane_id="%1"
):
    grant, ctx = _acquire_managed(monkeypatch, tmp_path, runtime_id, pane_id=pane_id)
    monkeypatch.setattr(runtime, "transport_send_prompt", _ok_send_transport)
    cursor = _cursor_for(rollout, 0)
    req = _send_req(
        tmp_path,
        grant,
        ctx,
        command_id=command_id,
        prompt=prompt,
        observationCursor=cursor,
    )
    resp, code = runtime.handle_runtime_request(req)
    assert code == 0, resp
    return grant, ctx, cursor


def test_collect_final_happy_path_and_refetch(monkeypatch, tmp_path):
    prompt = "[ACTL_MANAGED_V1 commandId=cmd1_final nonce=abcd]\nhello"
    sid, turn = "sess-final", "turn-1"
    rollout = _write_rollout(
        tmp_path / "sessions" / "rollout-sess-final.jsonl",
        [
            {"type": "session_meta", "payload": {"session_id": sid, "id": sid, "cwd": str(tmp_path)}},
            _user_msg(prompt, turn, sid),
            _final_msg("ANSWER", turn),
            _task_complete("ANSWER", turn),
        ],
    )
    grant, ctx, _ = _prepare_sent_command(
        monkeypatch, tmp_path, command_id="cmd1_final", prompt=prompt, rollout=rollout, runtime_id="rt1_final"
    )
    collect_req = {
        "contractVersion": 1,
        "requestId": str(uuid.uuid4()),
        "operation": "collect",
        "commandId": "cmd1_final",
        "runtimeId": grant["runtimeId"],
        "expectedContext": ctx,
        "serverScope": _scope(tmp_path),
    }
    first, c1 = runtime.handle_runtime_request(collect_req)
    assert c1 == 0 and first["ok"] is True
    packet = first["data"]["final"]
    assert packet["completionKind"] == "RESPONSE_COMPLETE"
    assert packet["rawFinalText"] == "ANSWER"
    assert packet["resultId"].startswith("res1_")
    assert packet["sessionId"] == sid
    assert packet["turnId"] == turn
    # V16: re-fetch same resultId even if we pretend runtime is down later.
    second, c2 = runtime.handle_runtime_request({**collect_req, "requestId": str(uuid.uuid4()), "resultId": packet["resultId"]})
    assert c2 == 0
    assert second["data"]["final"]["resultId"] == packet["resultId"]
    assert second["data"]["final"]["rawFinalText"] == "ANSWER"
    assert second["data"]["replayed"] is True


def test_collect_commentary_only_not_final(monkeypatch, tmp_path):
    prompt = "wire-commentary"
    sid, turn = "sess-c", "turn-c"
    rollout = _write_rollout(
        tmp_path / "sessions" / "rollout-sess-c.jsonl",
        [
            {"type": "session_meta", "payload": {"session_id": sid, "id": sid}},
            _user_msg(prompt, turn, sid),
            _commentary("ONLY_COMMENTARY", turn),
        ],
    )
    grant, ctx, _ = _prepare_sent_command(
        monkeypatch, tmp_path, command_id="cmd1_comm", prompt=prompt, rollout=rollout, runtime_id="rt1_comm"
    )
    resp, code = runtime.handle_runtime_request({
        "contractVersion": 1,
        "requestId": str(uuid.uuid4()),
        "operation": "collect",
        "commandId": "cmd1_comm",
        "runtimeId": grant["runtimeId"],
        "expectedContext": ctx,
        "serverScope": _scope(tmp_path),
    })
    assert code == 2
    assert resp["error"]["code"] == "RESULT_NOT_FINAL"


def test_collect_duplicate_user_turns_ambiguous(monkeypatch, tmp_path):
    prompt = "dup-wire"
    sid = "sess-d"
    rollout = _write_rollout(
        tmp_path / "sessions" / "rollout-sess-d.jsonl",
        [
            {"type": "session_meta", "payload": {"session_id": sid, "id": sid}},
            _user_msg(prompt, "t1", sid),
            _user_msg(prompt, "t2", sid),
        ],
    )
    grant, ctx, _ = _prepare_sent_command(
        monkeypatch, tmp_path, command_id="cmd1_dup", prompt=prompt, rollout=rollout, runtime_id="rt1_dup"
    )
    resp, code = runtime.handle_runtime_request({
        "contractVersion": 1,
        "requestId": str(uuid.uuid4()),
        "operation": "collect",
        "commandId": "cmd1_dup",
        "runtimeId": grant["runtimeId"],
        "expectedContext": ctx,
        "serverScope": _scope(tmp_path),
    })
    assert code == 2
    assert resp["error"]["code"] == "AMBIGUOUS_SESSION"


def test_collect_dynamic_pane_recreated_concurrent_and_stale_rollouts(monkeypatch, tmp_path):
    def collect(grant, ctx, command_id):
        response, code = runtime.handle_runtime_request({
            "contractVersion": 1,
            "requestId": str(uuid.uuid4()),
            "operation": "collect",
            "commandId": command_id,
            "runtimeId": grant["runtimeId"],
            "expectedContext": ctx,
            "serverScope": _scope(tmp_path),
        })
        assert code == 0 and response["ok"] is True, response
        return response["data"]["final"]

    # All rollouts are real inputs; only the live pane/process ownership map is
    # allowed to select one.  Collect starts from BOOTSTRAP, so runtime must
    # resolve the path from that identity instead of using a pinned cursor.
    stale = _write_rollout(tmp_path / "sessions" / "rollout-stale.jsonl", [
        {"type": "session_meta", "payload": {"session_id": "stale", "id": "stale"}},
        _user_msg("stale-prompt", "stale-turn", "stale"),
        _final_msg("STALE", "stale-turn"),
        _task_complete("STALE", "stale-turn"),
    ])
    prompt_a = "[ACTL_MANAGED_V1 commandId=cmd-dyn-a]\nalpha"
    current = _write_rollout(tmp_path / "sessions" / "rollout-current.jsonl", [
        {"type": "session_meta", "payload": {"session_id": "current", "id": "current"}},
        _user_msg(prompt_a, "turn-a", "current"),
        _final_msg("CURRENT", "turn-a"),
        _task_complete("CURRENT", "turn-a"),
    ])
    recreated = _write_rollout(tmp_path / "sessions" / "rollout-recreated.jsonl", [
        {"type": "session_meta", "payload": {"session_id": "recreated", "id": "recreated"}},
        _user_msg("[ACTL_MANAGED_V1 commandId=cmd-dyn-b]\nbeta", "turn-b", "recreated"),
        _final_msg("RECREATED", "turn-b"),
        _task_complete("RECREATED", "turn-b"),
    ])
    concurrent = _write_rollout(tmp_path / "sessions" / "rollout-concurrent.jsonl", [
        {"type": "session_meta", "payload": {"session_id": "concurrent", "id": "concurrent"}},
        _user_msg("[ACTL_MANAGED_V1 commandId=cmd-dyn-c]\ngamma", "turn-c", "concurrent"),
        _final_msg("CONCURRENT", "turn-c"),
        _task_complete("CONCURRENT", "turn-c"),
    ])
    pane_pids = {"%17": 71, "%23": 72, "%31": 73}
    locks = tmp_path / "thread-writer-locks"
    locks.mkdir()
    for session_id in ("current", "recreated", "concurrent"):
        (locks / f"{session_id}.lock").touch()
    owned = {
        71: [current, locks / "current.lock"],
        72: [recreated, locks / "recreated.lock"],
        73: [concurrent, locks / "concurrent.lock"],
    }
    from actl.agents import codex as codex_agent
    monkeypatch.setattr(codex_agent, "_codex_process", lambda pid, _target: pid)
    monkeypatch.setattr(codex_agent, "_open_paths", lambda pid: owned[pid])
    monkeypatch.setattr(codex_agent, "pane_field", lambda _pane, field: str(tmp_path) if field == "#{pane_current_path}" else "")

    def prepare(command_id, prompt, runtime_id, pane_id):
        grant, ctx = _acquire_managed(monkeypatch, tmp_path, runtime_id=runtime_id, pane_id=pane_id)
        ctx = {**ctx, "profileRoot": str(tmp_path), "agentPid": pane_pids[pane_id]}
        monkeypatch.setattr(runtime, "transport_send_prompt", _ok_send_transport)
        sent, code = runtime.handle_runtime_request(
            _send_req(tmp_path, grant, ctx, command_id=command_id, prompt=prompt)
        )
        assert code == 0, sent
        return grant, ctx

    grant_a, ctx_a = prepare("cmd-dyn-a", prompt_a, "rt1_dyn_a", "%17")
    assert collect(grant_a, ctx_a, "cmd-dyn-a")["rawFinalText"] == "CURRENT"
    assert Path(stale).read_text(encoding="utf-8") != Path(current).read_text(encoding="utf-8")

    prompt_b = "[ACTL_MANAGED_V1 commandId=cmd-dyn-b]\nbeta"
    grant_b, ctx_b = prepare("cmd-dyn-b", prompt_b, "rt1_dyn_b", "%23")
    assert collect(grant_b, ctx_b, "cmd-dyn-b")["rawFinalText"] == "RECREATED"

    prompt_c = "[ACTL_MANAGED_V1 commandId=cmd-dyn-c]\ngamma"
    grant_c, ctx_c = prepare("cmd-dyn-c", prompt_c, "rt1_dyn_c", "%31")
    assert collect(grant_c, ctx_c, "cmd-dyn-c")["rawFinalText"] == "CONCURRENT"


def test_collect_non_codex_unsupported(monkeypatch, tmp_path):
    _patch_journal_root(monkeypatch, tmp_path)
    # opencode remains outside Managed collect; grok now supports managed.collect.final.
    ctx = {"agentKind": "opencode", "workspaceRoot": str(tmp_path / "ws"), "paneId": "%1"}
    acquired, code = runtime.handle_runtime_request(
        _acquire_req(tmp_path, runtime_id="rt1_oc", expectedContext=ctx, mode="MANAGED")
    )
    assert code == 0
    grant = acquired["data"]
    path = runtime.journal_path_for_scope(runtime.scope_id_for_socket("hk-test", "1000", str(tmp_path / "sock")))
    with runtime.open_journal(path) as journal:
        journal.conn.execute("BEGIN IMMEDIATE")
        journal.insert_command_prepared(
            command_id="cmd1_oc",
            runtime_id=grant["runtimeId"],
            reservation_id=grant["reservationId"],
            request_id="r1",
            request_body_sha256="abc",
            wire_prompt="x",
            wire_prompt_sha256=_prompt_sha("x"),
            cursor_json=json.dumps({"path": str(tmp_path / "x.jsonl"), "byteOffset": 0}),
        )
        journal.update_command("cmd1_oc", stage="TRANSPORT_SENT", delivery_disposition="TRANSPORT_SENT")
        journal.conn.execute("COMMIT")
    resp, code2 = runtime.handle_runtime_request({
        "contractVersion": 1,
        "requestId": str(uuid.uuid4()),
        "operation": "collect",
        "commandId": "cmd1_oc",
        "runtimeId": grant["runtimeId"],
        "expectedContext": ctx,
        "serverScope": _scope(tmp_path),
    })
    assert code2 == 2
    assert resp["error"]["code"] == "UNSUPPORTED"


def test_collect_bootstrap_cursor_missing_path_is_not_final(monkeypatch, tmp_path):
    """Run 3 regression: BOOTSTRAP cursor without path must NOT raise INVALID_ARGUMENT.

    Live reserve freezes {kind:BOOTSTRAP,runtimeId} with no path. Collect must
    return RESULT_NOT_FINAL until a session rollout can be resolved.
    """
    from actl.agents import codex as codex_agent

    _patch_journal_root(monkeypatch, tmp_path)
    grant, ctx = _acquire_managed(monkeypatch, tmp_path, "rt1_bootstrap_path")
    # Simulate send that stored the live BOOTSTRAP cursor (no path).
    path = runtime.journal_path_for_scope(runtime.scope_id_for_socket("hk-test", "1000", str(tmp_path / "sock")))
    with runtime.open_journal(path) as journal:
        journal.conn.execute("BEGIN IMMEDIATE")
        journal.insert_command_prepared(
            command_id="cmd1_bootstrap",
            runtime_id=grant["runtimeId"],
            reservation_id=grant["reservationId"],
            request_id="r-bootstrap",
            request_body_sha256="deadbeef",
            wire_prompt="wire-bootstrap",
            wire_prompt_sha256=_prompt_sha("wire-bootstrap"),
            cursor_json=json.dumps({"kind": "BOOTSTRAP", "runtimeId": grant["runtimeId"]}),
        )
        journal.update_command("cmd1_bootstrap", stage="TRANSPORT_SENT", delivery_disposition="TRANSPORT_SENT")
        journal.conn.execute("COMMIT")

    # Unresolvable: resolve_codex returns no rollout.
    class _NoRollout:
        confidence = "none"
        rollout_path = None
        session_id = None

    monkeypatch.setattr(codex_agent, "resolve_codex", lambda *a, **k: _NoRollout())
    resp, code = runtime.handle_runtime_request({
        "contractVersion": 1,
        "requestId": str(uuid.uuid4()),
        "operation": "collect",
        "commandId": "cmd1_bootstrap",
        "runtimeId": grant["runtimeId"],
        "expectedContext": {**ctx, "profileRoot": str(tmp_path / ".codex"), "paneId": "%0"},
        "serverScope": _scope(tmp_path),
    })
    assert code == 2, resp
    assert resp["error"]["code"] == "RESULT_NOT_FINAL"
    assert resp["error"]["code"] != "INVALID_ARGUMENT"


def test_collect_bootstrap_cursor_resolves_path_then_final(monkeypatch, tmp_path):
    """BOOTSTRAP without path: once resolve_codex finds a rollout, collect proceeds."""
    from actl.agents import codex as codex_agent

    prompt = "[ACTL_MANAGED_V1 commandId=cmd1_bs_ok nonce=zz]\nhello"
    sid, turn = "sess-bs", "turn-bs"
    rollout = _write_rollout(
        tmp_path / "sessions" / "rollout-sess-bs.jsonl",
        [
            {"type": "session_meta", "payload": {"session_id": sid, "id": sid, "cwd": str(tmp_path)}},
            _user_msg(prompt, turn, sid),
            _final_msg("BOOTSTRAP_OK", turn),
            _task_complete("BOOTSTRAP_OK", turn),
        ],
    )
    grant, ctx = _acquire_managed(monkeypatch, tmp_path, "rt1_bs_ok")
    jpath = runtime.journal_path_for_scope(runtime.scope_id_for_socket("hk-test", "1000", str(tmp_path / "sock")))
    with runtime.open_journal(jpath) as journal:
        journal.conn.execute("BEGIN IMMEDIATE")
        journal.insert_command_prepared(
            command_id="cmd1_bs_ok",
            runtime_id=grant["runtimeId"],
            reservation_id=grant["reservationId"],
            request_id="r-bs-ok",
            request_body_sha256="cafe",
            wire_prompt=prompt,
            wire_prompt_sha256=_prompt_sha(prompt),
            cursor_json=json.dumps({"kind": "BOOTSTRAP", "runtimeId": grant["runtimeId"]}),
        )
        journal.update_command("cmd1_bs_ok", stage="TRANSPORT_SENT", delivery_disposition="TRANSPORT_SENT")
        journal.conn.execute("COMMIT")

    class _Resolved:
        confidence = "exact"
        rollout_path = rollout
        session_id = sid

    monkeypatch.setattr(codex_agent, "resolve_codex", lambda *a, **k: _Resolved())
    resp, code = runtime.handle_runtime_request({
        "contractVersion": 1,
        "requestId": str(uuid.uuid4()),
        "operation": "collect",
        "commandId": "cmd1_bs_ok",
        "runtimeId": grant["runtimeId"],
        "expectedContext": {**ctx, "profileRoot": str(tmp_path / ".codex"), "paneId": "%0"},
        "serverScope": _scope(tmp_path),
    })
    assert code == 0 and resp["ok"] is True, resp
    assert resp["data"]["final"]["rawFinalText"] == "BOOTSTRAP_OK"
    # Enriched cursor persisted for subsequent collects
    with runtime.open_journal(jpath) as journal:
        row = journal.get_command("cmd1_bs_ok")
        stored = json.loads(row["cursor_json"])
        assert stored.get("path") == str(rollout)
