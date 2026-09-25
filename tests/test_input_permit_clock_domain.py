"""Dogfood-03: inputPermit clock-domain + selected-runtime inspector truth."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from actl.core import remote, runtime
from actl.gui import inspector_truth


def test_remote_managed_send_uses_server_observed_at_not_mainpc_clock(monkeypatch):
    """MainPC/server skew must not mint confirmedAt from MainPC wall clock."""
    from actl.core import tmux as tmux_mod

    monkeypatch.setattr(tmux_mod, "REMOTE_SSH_TARGET", "asus")

    server_observed = "2026-09-21T15:00:00.000000Z"
    # MainPC is 2.494s ahead of ASUS — regenerating confirmedAt here would fail age checks.
    mainpc_stamp = "2026-09-21T15:00:02.494000Z"

    calls = []

    def fake_request(target, operation, body, timeout=30.0):
        calls.append((operation, body))
        if operation == "discover":
            return {
                "ok": True,
                "observedAt": server_observed,
                "data": {
                    "candidates": [{
                        "runtimeId": "rt-codex-1",
                        "agentKind": "codex",
                        "issuable": True,
                        "processState": "UP",
                        "capabilities": {"managed.send": True},
                        "identityEvidence": {"paneId": "%36"},
                        "profileRoot": "/tmp/codex",
                        "workspaceRoot": "/tmp/ws",
                    }]
                },
            }
        if operation == "reserve" and body.get("action") == "acquire":
            return {
                "ok": True,
                "observedAt": server_observed,
                "data": {
                    "runtimeId": "rt-codex-1",
                    "reservationId": "rsv-1",
                    "leaseToken": "tok-1",
                    "fence": "1",
                    "context": {"agentKind": "codex", "paneId": "%36"},
                    "currentSnapshotHash": "snap-server-1",
                    "observationCursor": {"kind": "BOOTSTRAP"},
                },
            }
        if operation == "send":
            permit = body["inputPermit"]
            assert permit["confirmedAt"] == server_observed
            assert permit["snapshotHash"] == "snap-server-1"
            assert permit["confirmedAt"] != mainpc_stamp
            assert "datetime.now" not in str(permit)
            return {
                "ok": True,
                "observedAt": server_observed,
                "data": {"command": {"target": "%36"}},
            }
        if operation == "reserve" and body.get("action") == "release":
            return {"ok": True, "observedAt": server_observed, "data": {}}
        raise AssertionError(f"unexpected {operation} {body}")

    monkeypatch.setattr(remote, "_runtime_request", fake_request)
    monkeypatch.setattr(
        remote.subprocess,
        "run",
        lambda *a, **k: type("R", (), {"returncode": 0, "stdout": "/tmp/tmux-1000/default\n", "stderr": ""})(),
    )

    assert remote.remote_managed_send("codex", "%36", "ACTL_V2_QA03") == "%36"
    assert [op for op, _ in calls] == ["discover", "reserve", "send", "reserve"]
    # Source-level guarantee: MainPC wall clock must not author confirmedAt.
    source = (remote.__file__ and open(remote.__file__, encoding="utf-8").read()) or ""
    assert "datetime.now" not in source.split("def remote_managed_send", 1)[1].split("def is_remote", 1)[0]


def test_client_clock_skew_does_not_affect_server_permit_validity(monkeypatch):
    """Simulated MainPC clock skew must not change validation when confirmedAt is server-issued."""
    server_now = datetime(2026, 9, 21, 15, 0, 1, tzinfo=timezone.utc).timestamp()
    monkeypatch.setattr(runtime, "wall_time_s", lambda: server_now)
    observed = datetime(2026, 9, 21, 15, 0, 0, tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    permit = {
        "commandId": "cmd1",
        "runtimeId": "rt1",
        "fence": "1",
        "paneMode": "normal",
        "confirmedAt": observed,
        "snapshotHash": "snap-1",
    }
    assert runtime._validate_input_permit(
        permit, command_id="cmd1", runtime_id="rt1", fence="1", current_snapshot_hash="snap-1"
    ) is None


def test_server_issued_permit_under_10s_accepted(monkeypatch):
    base = datetime(2026, 9, 21, 15, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(runtime, "wall_time_s", lambda: (base + timedelta(seconds=9.5)).timestamp())
    permit = {
        "commandId": "cmd1",
        "runtimeId": "rt1",
        "fence": "1",
        "paneMode": "normal",
        "confirmedAt": base.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "snapshotHash": "snap-1",
    }
    assert runtime._validate_input_permit(
        permit, command_id="cmd1", runtime_id="rt1", fence="1", current_snapshot_hash="snap-1"
    ) is None


def test_server_issued_permit_over_10s_input_state_unknown(monkeypatch):
    base = datetime(2026, 9, 21, 15, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(runtime, "wall_time_s", lambda: (base + timedelta(seconds=10.1)).timestamp())
    permit = {
        "commandId": "cmd1",
        "runtimeId": "rt1",
        "fence": "1",
        "paneMode": "normal",
        "confirmedAt": base.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "snapshotHash": "snap-1",
    }
    detail = runtime._validate_input_permit(
        permit, command_id="cmd1", runtime_id="rt1", fence="1", current_snapshot_hash="snap-1"
    )
    assert detail == "inputPermit expired or not yet valid"


def test_snapshot_hash_changed_after_observation_fails_closed(monkeypatch):
    base = datetime(2026, 9, 21, 15, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(runtime, "wall_time_s", lambda: (base + timedelta(seconds=1)).timestamp())
    permit = {
        "commandId": "cmd1",
        "runtimeId": "rt1",
        "fence": "1",
        "paneMode": "normal",
        "confirmedAt": base.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "snapshotHash": "snap-old",
    }
    detail = runtime._validate_input_permit(
        permit, command_id="cmd1", runtime_id="rt1", fence="1", current_snapshot_hash="snap-new"
    )
    assert detail == "snapshot hash mismatch"


def test_failed_permit_validation_has_no_tmux_side_effect(monkeypatch, tmp_path):
    from actl.core import tmux

    calls = []
    monkeypatch.setattr(tmux, "target_exists", lambda *a, **k: True)
    monkeypatch.setattr(tmux, "_run", lambda *a, **k: calls.append(a) or type("R", (), {"stdout": ""})())

    # Exercise the public staged path with a hook that rejects before input.
    def deny():
        raise RuntimeError("INPUT_STATE_UNKNOWN: inputPermit expired or not yet valid")

    result = tmux.send_prompt_staged("%36", "must-not-send", pre_send_hook=deny)
    assert result["ok"] is False
    assert result["sideEffect"] == "NONE"
    assert calls == []


def test_mainpc_ahead_2_5s_with_server_observed_at_is_not_false_expiry(monkeypatch):
    """Founder-measured +2.494s skew: server-issued observedAt must still validate."""
    server_observed_dt = datetime(2026, 9, 21, 12, 0, 0, tzinfo=timezone.utc)
    # ASUS validates with its own wall clock ~0.3s after observation + RTT.
    asus_validate_at = server_observed_dt + timedelta(seconds=0.3)
    monkeypatch.setattr(runtime, "wall_time_s", lambda: asus_validate_at.timestamp())
    # If MainPC stamped confirmedAt as now()+2.494s ahead, ASUS would see age < 0.
    wrong_mainpc_stamp = (server_observed_dt + timedelta(seconds=2.494)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    wrong = {
        "commandId": "cmd1",
        "runtimeId": "rt1",
        "fence": "1",
        "paneMode": "normal",
        "confirmedAt": wrong_mainpc_stamp,
        "snapshotHash": "snap-1",
    }
    assert runtime._validate_input_permit(
        wrong, command_id="cmd1", runtime_id="rt1", fence="1", current_snapshot_hash="snap-1"
    ) == "inputPermit expired or not yet valid"

    correct = dict(wrong)
    correct["confirmedAt"] = server_observed_dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    assert runtime._validate_input_permit(
        correct, command_id="cmd1", runtime_id="rt1", fence="1", current_snapshot_hash="snap-1"
    ) is None


def test_inspector_truth_syncs_title_and_action_target_across_selection():
    claude = {
        "runtime_key": "asus|claude-pro|%33|%33|1|/a",
        "agent": "claude-pro",
        "display": "Claude Pro",
        "target": "%33",
        "pane_id": "%33",
        "session": "jucontrol",
        "result_state": "UNKNOWN",
        "project": "P1",
        "role": "BUILDER",
        "runtime_state": "IDLE",
        "model_profile": "pro",
        "window": "1",
        "pane_index": "0",
        "pane_command": "claude",
        "pane_pid": "100",
        "current_task": "UNKNOWN",
        "state": "UP",
        "machine": "asus",
        "control_reason": "READY",
        "control_detail": "ok",
    }
    codex = {
        **claude,
        "runtime_key": "asus|codex|%36|%36|2|/b",
        "agent": "codex",
        "display": "Codex",
        "target": "%36",
        "pane_id": "%36",
        "session": "actl-v2-qa2",
        "pane_command": "codex",
        "pane_pid": "200",
    }
    first = inspector_truth(claude)
    second = inspector_truth(codex)
    assert "Claude Pro" in first["title"] and "%33" in first["title"]
    assert first["target"] == "%33" and first["agent"] == "claude-pro"
    assert "Codex" in second["title"] and "%36" in second["title"]
    assert second["target"] == "%36" and second["agent"] == "codex"
    assert second["session"] == "actl-v2-qa2"
    # After switching selection, title and action target must both be Codex %36.
    assert "Claude" not in second["title"]
    assert "%33" not in second["title"]
    assert "%33" not in second["detail"]
