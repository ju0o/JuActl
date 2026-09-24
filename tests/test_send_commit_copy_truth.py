"""Dogfood-05: managed SEND commit boundary + COPY clipboard truth."""
from __future__ import annotations

import threading
import time

from actl.core import remote, send_truth
from actl.core import remote_scheduler as sched


def _reset() -> None:
    sched.close_scheduler(None)
    send_truth.reset_all_correlations()


def _fake_managed_stack(monkeypatch, *, release_delay_s: float = 0.0, release_error: str | None = None):
    from actl.core import tmux as tmux_mod

    monkeypatch.setattr(tmux_mod, "REMOTE_SSH_TARGET", "asus")
    events: list[str] = []
    release_started = threading.Event()
    release_finished = threading.Event()
    committed_at = {"t": None}

    def fake_request(target, operation, body, timeout=30.0):
        if operation == "discover":
            events.append("discover")
            return {
                "ok": True,
                "observedAt": "2026-09-22T00:00:00.000000Z",
                "data": {
                    "candidates": [{
                        "runtimeId": "rt-1",
                        "agentKind": "codex",
                        "issuable": True,
                        "processState": "UP",
                        "capabilities": {"managed.send": True},
                        "identityEvidence": {"paneId": "%0"},
                        "profileRoot": "/tmp/codex",
                        "workspaceRoot": "/tmp/ws",
                    }]
                },
            }
        if operation == "reserve" and body.get("action") == "acquire":
            events.append("acquire")
            return {
                "ok": True,
                "observedAt": "2026-09-22T00:00:00.000000Z",
                "data": {
                    "runtimeId": "rt-1",
                    "reservationId": "rsv-1",
                    "leaseToken": "tok-1",
                    "fence": "1",
                    "context": {"agentKind": "codex", "paneId": "%0"},
                    "currentSnapshotHash": "snap-1",
                    "observationCursor": {"kind": "BOOTSTRAP"},
                },
            }
        if operation == "send":
            events.append("send")
            return {
                "ok": True,
                "observedAt": "2026-09-22T00:00:00.100000Z",
                "data": {"command": {"target": "%0"}},
            }
        if operation == "reserve" and body.get("action") == "release":
            events.append("release-start")
            release_started.set()
            if release_delay_s:
                time.sleep(release_delay_s)
            if release_error:
                events.append("release-fail")
                raise RuntimeError(release_error)
            events.append("release-ok")
            release_finished.set()
            return {"ok": True, "observedAt": "2026-09-22T00:00:01.000000Z", "data": {}}
        raise AssertionError(f"unexpected {operation}")

    monkeypatch.setattr(remote, "_runtime_request", fake_request)
    monkeypatch.setattr(
        remote.subprocess,
        "run",
        lambda *a, **k: type("R", (), {"returncode": 0, "stdout": "/tmp/tmux-1000/default\n", "stderr": ""})(),
    )
    return events, release_started, release_finished, committed_at


def test_managed_send_marks_committed_before_cleanup_completes(monkeypatch):
    events, release_started, release_finished, committed_at = _fake_managed_stack(
        monkeypatch, release_delay_s=0.35
    )
    states: list[str] = []

    def on_committed(delivery):
        committed_at["t"] = time.monotonic()
        states.append("committed")
        assert not release_finished.is_set()
        assert delivery.target == "%0"

    t0 = time.monotonic()
    delivery = remote.remote_managed_send(
        "codex",
        "%0",
        "ACTL_V2_QA05",
        on_committed=on_committed,
        auto_cleanup=True,
    )
    assert "committed" in states
    assert committed_at["t"] is not None
    # Commit happened before delayed release finished.
    assert committed_at["t"] - t0 < 0.35
    assert release_started.wait(timeout=2)
    assert release_finished.wait(timeout=2)
    assert delivery.cleanup_attempted is True
    assert delivery.cleanup_ok is True
    assert events.index("send") < events.index("release-start")
    assert delivery == "%0"


def test_delayed_release_does_not_delay_submitted_callback(monkeypatch):
    _events, _rs, release_finished, committed_at = _fake_managed_stack(
        monkeypatch, release_delay_s=0.5
    )
    submitted = {"ok": False}

    def on_committed(_delivery):
        submitted["ok"] = True
        committed_at["t"] = time.monotonic()

    t0 = time.monotonic()
    remote.remote_managed_send(
        "codex", "%0", "x", on_committed=on_committed, auto_cleanup=True
    )
    assert submitted["ok"] is True
    assert (committed_at["t"] - t0) < 0.25
    assert release_finished.wait(timeout=2)


def test_cleanup_failure_after_success_does_not_fail_send(monkeypatch):
    _events, _rs, _rf, _ = _fake_managed_stack(
        monkeypatch, release_delay_s=0.05, release_error="lease already expired"
    )
    states: list[str] = []

    def on_committed(_d):
        states.append("SUBMITTED")

    delivery = remote.remote_managed_send(
        "codex", "%0", "x", on_committed=on_committed, auto_cleanup=True
    )
    assert states == ["SUBMITTED"]
    assert delivery.cleanup_attempted is True
    assert delivery.cleanup_ok is False
    assert "lease already expired" in delivery.cleanup_error
    # Delivery remains authoritative success.
    assert delivery.target == "%0"


def test_deferred_cleanup_does_not_block_copy_acquire(monkeypatch):
    """COPY P0 can start while cleanup still runs (cleanup uses separate SSH)."""
    _reset()
    events, release_started, release_finished, _ = _fake_managed_stack(
        monkeypatch, release_delay_s=0.6
    )
    s = sched.scheduler_for("asus-commit-copy")
    copy_started = threading.Event()
    t0 = {"t": 0.0}

    def send_work():
        delivery = remote.remote_managed_send(
            "codex", "%0", "x", auto_cleanup=False
        )

        def cleanup():
            delivery.cleanup()

        threading.Thread(target=cleanup, daemon=True).start()
        return delivery.target

    def copy_work():
        copy_started.set()
        return "copied"

    results: dict[str, object] = {}

    def run_send():
        results["send"] = s.submit(
            send_work, priority=sched.P0_USER, kind=sched.KIND_SEND, replaceable=False
        )

    def run_copy():
        # Wait until send committed and cleanup started, then COPY must acquire quickly.
        assert release_started.wait(timeout=2)
        t0["t"] = time.monotonic()
        results["copy"] = s.submit(
            copy_work, priority=sched.P0_USER, kind=sched.KIND_COPY, replaceable=False
        )

    t_send = threading.Thread(target=run_send, daemon=True)
    t_copy = threading.Thread(target=run_copy, daemon=True)
    t_send.start()
    time.sleep(0.05)
    t_copy.start()
    t_send.join(timeout=3)
    t_copy.join(timeout=3)
    assert results.get("send") == "%0"
    assert results.get("copy") == "copied"
    assert copy_started.is_set()
    assert (time.monotonic() - t0["t"]) <= sched.FOREGROUND_ACQUIRE_MAX_S + 0.35
    assert release_finished.wait(timeout=2)
    assert "release-ok" in events
    _reset()


def test_cleanup_eventually_attempted_when_deferred(monkeypatch):
    _events, _rs, release_finished, _ = _fake_managed_stack(monkeypatch, release_delay_s=0.05)
    delivery = remote.remote_managed_send("codex", "%0", "x", auto_cleanup=False)
    assert delivery.cleanup_attempted is False
    assert delivery.cleanup() is True
    assert delivery.cleanup_attempted is True
    assert release_finished.is_set()


def test_result_pending_stale_no_result_do_not_allow_clipboard():
    assert send_truth.clipboard_write_allowed(send_truth.NEW_RESULT) is True
    assert send_truth.clipboard_write_allowed(send_truth.RESULT_PENDING) is False
    assert send_truth.clipboard_write_allowed(send_truth.STALE_RESULT) is False
    assert send_truth.clipboard_write_allowed(send_truth.NO_RESULT) is False


def test_failed_send_cannot_copy_prior_result_as_new():
    _reset()
    old = "ACTL_V2_QA04_20260922_K7M4Q9"
    old_hash = send_truth.result_hash(old)
    send_truth.begin_send("codex", "%0", previous_result_hash=old_hash)
    send_truth.set_send_state("codex", "%0", send_truth.SEND_FAILED, error="timeout")
    klass, _ = send_truth.classify_result("codex", "%0", old_hash, text=old)
    assert klass == send_truth.STALE_RESULT
    assert send_truth.clipboard_write_allowed(klass) is False
    _reset()


def test_new_result_allows_clipboard_once_then_duplicate_not_new():
    _reset()
    marker = "ACTL_V2_QA05_MARKER"
    prev = send_truth.result_hash("OLD")
    new_hash = send_truth.result_hash(marker)
    send_truth.begin_send("codex", "%0", previous_result_hash=prev)
    send_truth.set_send_state("codex", "%0", send_truth.SUBMITTED)
    send_truth.set_send_state("codex", "%0", send_truth.START_ACKNOWLEDGED)
    klass1, _ = send_truth.classify_result("codex", "%0", new_hash, text=marker)
    assert klass1 == send_truth.NEW_RESULT
    assert send_truth.clipboard_write_allowed(klass1) is True
    # Duplicate COPY after first NEW observation must not be NEW again.
    klass2, _ = send_truth.classify_result("codex", "%0", new_hash, text=marker)
    assert klass2 == send_truth.STALE_RESULT
    assert send_truth.clipboard_write_allowed(klass2) is False
    _reset()


def test_no_result_does_not_allow_clipboard():
    _reset()
    send_truth.begin_send("codex", "%0", previous_result_hash="h")
    send_truth.set_send_state("codex", "%0", send_truth.SUBMITTED)
    klass, _ = send_truth.classify_result("codex", "%0", None)
    assert klass == send_truth.NO_RESULT
    assert send_truth.clipboard_write_allowed(klass) is False
    _reset()


def test_result_pending_does_not_allow_clipboard():
    _reset()
    same = send_truth.result_hash("same")
    send_truth.begin_send("codex", "%0", previous_result_hash=same)
    send_truth.set_send_state("codex", "%0", send_truth.START_ACKNOWLEDGED)
    klass, _ = send_truth.classify_result("codex", "%0", same)
    assert klass == send_truth.RESULT_PENDING
    assert send_truth.clipboard_write_allowed(klass) is False
    _reset()


def test_busy_send_requires_a_second_result_change():
    _reset()
    send_truth.begin_send(
        "codex", "%0", previous_result_hash=send_truth.result_hash("RESULT::JOB1"),
        busy_at_send=True,
    )
    send_truth.set_send_state("codex", "%0", send_truth.START_ACKNOWLEDGED)
    first, _ = send_truth.classify_result("codex", "%0", send_truth.result_hash("NEW_RESULT"))
    assert first != send_truth.NEW_RESULT
    second, _ = send_truth.classify_result("codex", "%0", send_truth.result_hash("RESULT::JOB2"))
    assert second == send_truth.NEW_RESULT
    _reset()
