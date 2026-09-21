"""V18: authorized captureAck / reconcile only; no force takeover."""
from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path

from actl.core import runtime


def _scope(tmp_path: Path):
    return {"hostKey": "hk-test", "uid": "1000", "socketPath": str(tmp_path / "sock")}


def _patch_journal_root(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(runtime, "_journal_root_override", tmp_path)


def _acquire(monkeypatch, tmp_path: Path, runtime_id: str = "rt_v18"):
    _patch_journal_root(monkeypatch, tmp_path)
    ctx = {"agentKind": "codex", "workspaceRoot": str(tmp_path / "ws"), "paneId": "%1"}
    body = {
        "contractVersion": 1,
        "requestId": str(uuid.uuid4()),
        "operation": "reserve",
        "action": "acquire",
        "runtimeId": runtime_id,
        "mode": "MANAGED",
        "expectedContext": ctx,
        "serverScope": _scope(tmp_path),
    }
    resp, code = runtime.handle_runtime_request(body)
    assert code == 0 and resp["ok"] is True
    return resp["data"], ctx


def _release_req(tmp_path: Path, grant, *, capture_ack=None, request_id=None):
    body = {
        "contractVersion": 1,
        "requestId": request_id or str(uuid.uuid4()),
        "operation": "reserve",
        "action": "release",
        "reservationId": grant["reservationId"],
        "leaseToken": grant["leaseToken"],
        "fence": grant["fence"],
        "serverScope": _scope(tmp_path),
    }
    if capture_ack is not None:
        body["captureAck"] = capture_ack
    return body


def _prompt_sha(text: str) -> str:
    return runtime.sha256_hex(text.encode("utf-8"))


def _permit(command_id: str, runtime_id: str, fence: str, *, confirmed_at=None, snapshot="snap-1"):
    return {
        "commandId": command_id,
        "runtimeId": runtime_id,
        "fence": fence,
        "paneMode": "normal",
        "snapshotHash": snapshot,
        "confirmedAt": confirmed_at if confirmed_at is not None else runtime.wall_time_s(),
    }


def _send_ok(monkeypatch, tmp_path: Path, grant, ctx, command_id: str):
    def fake_transport(*a, **k):
        if k.get("pre_send_hook"):
            k["pre_send_hook"]()
        return {
            "ok": True,
            "completedStages": ["verify_target", "load_buffer", "paste_buffer", "enter"],
            "sideEffect": "INPUT_OBSERVED",
            "deliveryDisposition": "TRANSPORT_SENT",
            "stages": [],
        }

    monkeypatch.setattr(runtime, "transport_send_prompt", fake_transport)
    prompt = "v18-wire"
    body = {
        "contractVersion": 1,
        "requestId": str(uuid.uuid4()),
        "operation": "send",
        "runtimeId": grant["runtimeId"],
        "expectedContext": ctx,
        "reservationId": grant["reservationId"],
        "leaseToken": grant["leaseToken"],
        "fence": grant["fence"],
        "commandId": command_id,
        "correlationDigest": "corr-v18",
        "wirePrompt": prompt,
        "promptSha256": _prompt_sha(prompt),
        "observationCursor": {"kind": "BOOTSTRAP"},
        "inputPermit": _permit(command_id, grant["runtimeId"], grant["fence"]),
        "currentSnapshotHash": "snap-1",
        "serverScope": _scope(tmp_path),
    }
    resp, code = runtime.handle_runtime_request(body)
    assert code == 0 and resp["data"]["inputPerformed"] is True
    return resp


def _reservation_state(tmp_path: Path, reservation_id: str) -> str:
    path = runtime.journal_path_for_scope(
        runtime.scope_id_for_socket("hk-test", "1000", str(tmp_path / "sock"))
    )
    conn = sqlite3.connect(str(path))
    try:
        row = conn.execute(
            "SELECT state FROM reservations WHERE reservation_id = ?",
            (reservation_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    return str(row[0])


def _inject_final(tmp_path: Path, command_id: str, result_id: str) -> None:
    path = runtime.journal_path_for_scope(
        runtime.scope_id_for_socket("hk-test", "1000", str(tmp_path / "sock"))
    )
    with runtime.open_journal(path) as journal:
        journal.conn.execute("BEGIN IMMEDIATE")
        journal.update_command(command_id, stage="FINAL", result_id=result_id)
        journal.add_receipt(
            command_id=command_id,
            request_id="inject-final",
            kind="FINAL",
            stage="FINAL",
            side_effect="NONE",
            payload={"resultId": result_id},
        )
        journal.conn.execute("COMMIT")


def test_side_effect_free_release_without_commands(monkeypatch, tmp_path):
    grant, _ = _acquire(monkeypatch, tmp_path, "rt_v18_sefree")
    resp, code = runtime.handle_runtime_request(_release_req(tmp_path, grant))
    assert code == 0 and resp["data"]["state"] == "RELEASED"
    assert _reservation_state(tmp_path, grant["reservationId"]) == "RELEASED"


def test_release_without_ack_fails_when_command_attached(monkeypatch, tmp_path):
    grant, ctx = _acquire(monkeypatch, tmp_path, "rt_v18_noack")
    _send_ok(monkeypatch, tmp_path, grant, ctx, "cmd_v18_noack")
    resp, code = runtime.handle_runtime_request(_release_req(tmp_path, grant))
    assert code == 3
    assert resp["error"]["code"] == "INVALID_ARGUMENT"
    assert "captureAck" in resp["error"]["detail"]
    assert _reservation_state(tmp_path, grant["reservationId"]) == "HELD"


def test_unauthorized_ack_shapes_do_not_release(monkeypatch, tmp_path):
    grant, ctx = _acquire(monkeypatch, tmp_path, "rt_v18_badack")
    _send_ok(monkeypatch, tmp_path, grant, ctx, "cmd_v18_badack")
    bad_acks = [
        {"expired": True},
        {"copySuccess": True},
        {"copy": True},
        {"taskId": "task-1"},
        {"runId": "run-1"},
        {"force": True},
        {},
        "not-an-object",
        {"kind": "FINAL_CAPTURE", "resultId": "res_x", "acknowledged": False},
    ]
    for ack in bad_acks:
        resp, code = runtime.handle_runtime_request(_release_req(tmp_path, grant, capture_ack=ack))
        assert code in {2, 3}, ack
        assert resp["ok"] is False, ack
        assert resp["error"]["code"] in {"INVALID_ARGUMENT", "FORBIDDEN", "BUSY"}, ack
        assert _reservation_state(tmp_path, grant["reservationId"]) == "HELD", ack


def test_final_capture_ack_releases(monkeypatch, tmp_path):
    grant, ctx = _acquire(monkeypatch, tmp_path, "rt_v18_final")
    _send_ok(monkeypatch, tmp_path, grant, ctx, "cmd_v18_final")
    # Still unresolved — FINAL_CAPTURE must fail.
    pending, code_p = runtime.handle_runtime_request(
        _release_req(
            tmp_path,
            grant,
            capture_ack={"kind": "FINAL_CAPTURE", "resultId": "res_v18_final", "acknowledged": True},
        )
    )
    assert code_p == 3
    assert "unresolved" in pending["error"]["detail"]
    assert _reservation_state(tmp_path, grant["reservationId"]) == "HELD"

    _inject_final(tmp_path, "cmd_v18_final", "res_v18_final")
    # Wrong resultId still fails.
    wrong, code_w = runtime.handle_runtime_request(
        _release_req(
            tmp_path,
            grant,
            capture_ack={"kind": "FINAL_CAPTURE", "resultId": "res_other", "acknowledged": True},
        )
    )
    assert code_w == 3
    assert _reservation_state(tmp_path, grant["reservationId"]) == "HELD"

    ok, code = runtime.handle_runtime_request(
        _release_req(
            tmp_path,
            grant,
            capture_ack={
                "kind": "FINAL_CAPTURE",
                "resultId": "res_v18_final",
                "acknowledged": True,
            },
        )
    )
    assert code == 0 and ok["data"]["state"] == "RELEASED"
    assert _reservation_state(tmp_path, grant["reservationId"]) == "RELEASED"


def test_expired_held_unresolved_no_force_takeover(monkeypatch, tmp_path):
    clock = {"ns": 50_000_000_000}
    monkeypatch.setattr(runtime, "boot_time_ns", lambda: clock["ns"])
    grant, ctx = _acquire(monkeypatch, tmp_path, "rt_v18_expire")
    _send_ok(monkeypatch, tmp_path, grant, ctx, "cmd_v18_expire")
    assert _reservation_state(tmp_path, grant["reservationId"]) == "HELD"

    # Send return path must not auto-release.
    assert _reservation_state(tmp_path, grant["reservationId"]) == "HELD"

    clock["ns"] = 50_000_000_000 + runtime.DEFAULT_LEASE_NS + 1
    acquire_again = {
        "contractVersion": 1,
        "requestId": str(uuid.uuid4()),
        "operation": "reserve",
        "action": "acquire",
        "runtimeId": "rt_v18_expire",
        "mode": "MANAGED",
        "expectedContext": ctx,
        "serverScope": _scope(tmp_path),
    }
    busy, code_b = runtime.handle_runtime_request(acquire_again)
    assert code_b == 2 and busy["error"]["code"] == "BUSY"
    assert _reservation_state(tmp_path, grant["reservationId"]) == "EXPIRED_HELD"

    bad, code_bad = runtime.handle_runtime_request(
        _release_req(tmp_path, grant, capture_ack={"expired": True})
    )
    assert code_bad == 3
    assert _reservation_state(tmp_path, grant["reservationId"]) == "EXPIRED_HELD"

    force, code_f = runtime.handle_runtime_request(
        _release_req(tmp_path, grant, capture_ack={"force": True})
    )
    assert code_f == 3
    assert force["error"]["code"] == "FORBIDDEN"
    assert _reservation_state(tmp_path, grant["reservationId"]) == "EXPIRED_HELD"

    # Valid reconcile acknowledgement can release unresolved EXPIRED_HELD.
    ok, code = runtime.handle_runtime_request(
        _release_req(
            tmp_path,
            grant,
            capture_ack={
                "kind": "RECONCILE",
                "commandId": "cmd_v18_expire",
                "disposition": "DELIVERY_AMBIGUOUS",
                "acknowledged": True,
            },
        )
    )
    assert code == 0 and ok["data"]["state"] == "RELEASED"
    assert _reservation_state(tmp_path, grant["reservationId"]) == "RELEASED"


def test_reconcile_requires_matching_command(monkeypatch, tmp_path):
    grant, ctx = _acquire(monkeypatch, tmp_path, "rt_v18_rec")
    _send_ok(monkeypatch, tmp_path, grant, ctx, "cmd_v18_rec")
    resp, code = runtime.handle_runtime_request(
        _release_req(
            tmp_path,
            grant,
            capture_ack={
                "kind": "RECONCILE",
                "commandId": "cmd_other",
                "disposition": "CANCEL_REQUESTED",
                "acknowledged": True,
            },
        )
    )
    assert code == 3
    assert "commandId" in resp["error"]["detail"]
    assert _reservation_state(tmp_path, grant["reservationId"]) == "HELD"


def test_ownerless_expired_recovery_reconciles_without_lease_token(monkeypatch, tmp_path):
    clock = {"ns": 50_000_000_000}
    monkeypatch.setattr(runtime, "boot_time_ns", lambda: clock["ns"])
    grant, ctx = _acquire(monkeypatch, tmp_path, "rt_v18_ownerless")
    _send_ok(monkeypatch, tmp_path, grant, ctx, "cmd_v18_ownerless")
    clock["ns"] += runtime.DEFAULT_LEASE_NS + 1
    runtime.handle_runtime_request({
        "contractVersion": 1,
        "requestId": str(uuid.uuid4()),
        "operation": "reserve",
        "action": "acquire",
        "runtimeId": grant["runtimeId"],
        "mode": "MANAGED",
        "expectedContext": ctx,
        "serverScope": _scope(tmp_path),
    })
    request = {
        "contractVersion": 1,
        "requestId": str(uuid.uuid4()),
        "operation": "reserve",
        "action": "recover",
        "reservationId": grant["reservationId"],
        "fence": grant["fence"],
        "recoveryEvidence": {
            "kind": "OWNERLESS_EXPIRED_RECOVERY",
            "acknowledged": True,
            "reason": "LEASE_CREDENTIAL_IRRECOVERABLE",
            "reservationId": grant["reservationId"],
            "fence": grant["fence"],
            "commands": [{"commandId": "cmd_v18_ownerless", "disposition": "DELIVERY_AMBIGUOUS"}],
        },
        "serverScope": _scope(tmp_path),
    }
    ok, code = runtime.handle_runtime_request(request)
    assert code == 0 and ok["data"]["state"] == "RELEASED"
    assert _reservation_state(tmp_path, grant["reservationId"]) == "RELEASED"
    path = runtime.journal_path_for_scope(
        runtime.scope_id_for_socket("hk-test", "1000", str(tmp_path / "sock"))
    )
    with runtime.open_journal(path) as journal:
        receipt = journal.conn.execute(
            "SELECT kind, stage FROM receipts WHERE command_id = ? ORDER BY created_at DESC LIMIT 1",
            ("cmd_v18_ownerless",),
        ).fetchone()
        assert tuple(receipt) == ("OWNERLESS_EXPIRED_RECOVERY", "DELIVERY_AMBIGUOUS")


def test_ownerless_recovery_does_not_take_over_held(monkeypatch, tmp_path):
    grant, _ = _acquire(monkeypatch, tmp_path, "rt_v18_active")
    response, code = runtime.handle_runtime_request({
        "contractVersion": 1,
        "requestId": str(uuid.uuid4()),
        "operation": "reserve",
        "action": "recover",
        "reservationId": grant["reservationId"],
        "fence": grant["fence"],
        "recoveryEvidence": {
            "kind": "OWNERLESS_EXPIRED_RECOVERY",
            "acknowledged": True,
            "reason": "LEASE_CREDENTIAL_IRRECOVERABLE",
            "reservationId": grant["reservationId"],
            "fence": grant["fence"],
            "commands": [],
        },
        "serverScope": _scope(tmp_path),
    })
    assert code == 2 and response["error"]["code"] == "BUSY"
    assert _reservation_state(tmp_path, grant["reservationId"]) == "HELD"


def test_send_does_not_auto_release(monkeypatch, tmp_path):
    grant, ctx = _acquire(monkeypatch, tmp_path, "rt_v18_hold")
    _send_ok(monkeypatch, tmp_path, grant, ctx, "cmd_v18_hold")
    assert _reservation_state(tmp_path, grant["reservationId"]) == "HELD"
