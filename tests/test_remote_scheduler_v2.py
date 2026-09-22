"""V2 dogfood regressions: remote scheduler + send/result correlation."""
from __future__ import annotations

import threading
import time

from actl.core import remote_scheduler as sched
from actl.core import send_truth
from actl.core.validation import TargetValidation, validate_target


def _reset() -> None:
    sched.close_scheduler(None)
    send_truth.reset_all_correlations()


def _safe_submit(s, work, **kwargs):
    try:
        return s.submit(work, **kwargs)
    except sched.RemoteOpCancelled as exc:
        return exc


def test_background_preview_yields_to_send_priority():
    """Background preview running + SEND requested -> SEND receives priority."""
    _reset()
    order: list[str] = []
    started = threading.Event()
    release_preview = threading.Event()
    s = sched.scheduler_for("asus-test-priority")

    def preview():
        order.append("preview-start")
        started.set()
        assert release_preview.wait(timeout=2)
        order.append("preview-end")
        return "preview"

    def send():
        order.append("send")
        return "sent"

    results: dict[str, object] = {}

    def run_preview():
        try:
            results["preview"] = s.submit(
                preview,
                priority=sched.P2_BACKGROUND,
                kind=sched.KIND_PREVIEW,
                coalesce_key="preview:codex",
                replaceable=True,
            )
        except sched.RemoteOpCancelled as exc:
            results["preview"] = exc

    def run_send():
        assert started.wait(timeout=2)

        def late_preview():
            order.append("late-preview")
            return "late"

        late = threading.Thread(
            target=lambda: results.setdefault(
                "late",
                _safe_submit(
                    s,
                    late_preview,
                    priority=sched.P2_BACKGROUND,
                    kind=sched.KIND_PREVIEW,
                    coalesce_key="preview:other",
                    replaceable=True,
                ),
            ),
            daemon=True,
        )
        late.start()
        time.sleep(0.05)

        def do_send():
            results["send"] = s.submit(
                send,
                priority=sched.P0_USER,
                kind=sched.KIND_SEND,
                replaceable=False,
            )

        send_thread = threading.Thread(target=do_send, daemon=True)
        send_thread.start()
        time.sleep(0.05)
        # Allow the in-flight replaceable preview to finish; SEND stays queued at P0
        # ahead of any remaining background work.
        release_preview.set()
        send_thread.join(timeout=3)
        late.join(timeout=2)

    t1 = threading.Thread(target=run_preview, daemon=True)
    t2 = threading.Thread(target=run_send, daemon=True)
    t1.start()
    t2.start()
    t1.join(timeout=3)
    t2.join(timeout=3)

    assert results.get("send") == "sent"
    assert "send" in order
    assert isinstance(results.get("late"), (sched.RemoteOpCancelled, type(None))) or results.get("late") == "late"
    assert s.stats()["user_preempts"] >= 1
    _reset()


def test_duplicate_background_refresh_is_coalesced():
    """Background refresh duplicated -> coalesced, not multiplied."""
    _reset()
    s = sched.scheduler_for("asus-test-coalesce")
    runs = {"n": 0}
    gate = threading.Event()
    first_started = threading.Event()

    def refresh():
        runs["n"] += 1
        first_started.set()
        assert gate.wait(timeout=2)
        return f"refresh-{runs['n']}"

    results: list[object] = []

    def launch():
        results.append(
            s.submit(
                refresh,
                priority=sched.P2_BACKGROUND,
                kind=sched.KIND_REFRESH,
                coalesce_key="refresh:asus",
                replaceable=True,
            )
        )

    first = threading.Thread(target=launch, daemon=True)
    first.start()
    assert first_started.wait(timeout=2)
    extras = [threading.Thread(target=launch, daemon=True) for _ in range(2)]
    for thread in extras:
        thread.start()
    time.sleep(0.05)
    gate.set()
    first.join(timeout=2)
    for thread in extras:
        thread.join(timeout=2)

    assert runs["n"] == 1
    assert results == ["refresh-1", "refresh-1", "refresh-1"]
    assert s.stats()["coalesced"] >= 2
    _reset()


def test_transport_busy_is_not_mapping_down(monkeypatch):
    """transport busy -> NOT mapping DOWN."""
    _reset()
    from actl.core import validation

    def boom(_target):
        raise sched.TransportBusyError("remote transport busy: maximum in-flight operations is 1")

    monkeypatch.setattr(validation, "target_exists", boom)
    result = validate_target("codex", "%0")
    assert result.state == "TRANSPORT_BUSY"
    assert result.state != "DOWN"
    assert result.valid is True
    assert "remap not required" in result.detail
    _reset()


def test_failed_send_does_not_advance_correlation():
    """failed SEND -> no Task/result correlation advancement."""
    _reset()
    corr = send_truth.begin_send("codex", "%0", previous_result_hash="oldhash")
    assert corr.send_state == send_truth.SEND_QUEUED
    failed = send_truth.set_send_state("codex", "%0", send_truth.SEND_FAILED, error="timeout")
    assert failed is not None
    assert failed.send_state == send_truth.SEND_FAILED
    assert failed.send_succeeded is False
    klass, snap = send_truth.classify_result("codex", "%0", "oldhash")
    assert klass == send_truth.STALE_RESULT
    assert snap is not None
    assert snap.result_hash_after is None
    _reset()


def test_old_result_with_failed_send_is_stale():
    """old result exists + new SEND failed -> STALE_RESULT."""
    _reset()
    old = "ACTL_LIVE_E2E_OK_20260921T090221Z_be40ffc7"
    old_hash = send_truth.result_hash(old)
    send_truth.begin_send("codex", "%0", previous_result_hash=old_hash)
    send_truth.set_send_state("codex", "%0", send_truth.SEND_FAILED, error="transport timeout")
    klass, _ = send_truth.classify_result("codex", "%0", old_hash, text=old)
    assert klass == send_truth.STALE_RESULT
    _reset()


def test_successful_send_changed_result_is_new():
    """successful SEND + changed result -> NEW_RESULT."""
    _reset()
    send_truth.begin_send("codex", "%0", previous_result_hash="hash-old")
    send_truth.set_send_state("codex", "%0", send_truth.SUBMITTED)
    send_truth.set_send_state("codex", "%0", send_truth.START_ACKNOWLEDGED)
    klass, snap = send_truth.classify_result("codex", "%0", "hash-new")
    assert klass == send_truth.NEW_RESULT
    assert snap is not None
    assert snap.result_hash_after == "hash-new"
    _reset()


def test_successful_send_unchanged_result_is_pending():
    """successful SEND + result not changed yet -> RESULT_PENDING."""
    _reset()
    send_truth.begin_send("codex", "%0", previous_result_hash="hash-same")
    send_truth.set_send_state("codex", "%0", send_truth.START_ACKNOWLEDGED)
    klass, _ = send_truth.classify_result("codex", "%0", "hash-same")
    assert klass == send_truth.RESULT_PENDING
    _reset()


def test_scheduler_does_not_fan_out_parallel_exclusive_work():
    """no SSH/tmux fan-out regression: exclusive work stays serial."""
    _reset()
    s = sched.scheduler_for("asus-test-serial")
    active = {"n": 0, "max": 0}
    lock = threading.Lock()

    def tick():
        with lock:
            active["n"] += 1
            active["max"] = max(active["max"], active["n"])
        time.sleep(0.05)
        with lock:
            active["n"] -= 1
        return True

    threads = [
        threading.Thread(
            target=lambda: s.submit(tick, priority=sched.P2_BACKGROUND, kind=sched.KIND_GENERIC),
            daemon=True,
        )
        for _ in range(6)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3)
    assert active["max"] == 1
    _reset()


def test_target_validation_valid_includes_transport_busy():
    _reset()
    assert TargetValidation("TRANSPORT_BUSY", "%0").valid is True
    assert TargetValidation("DOWN", "%0").valid is False
    assert TargetValidation("UP", "%0").valid is True


def test_korean_founder_labels_exist():
    assert send_truth.SEND_STATE_KO[send_truth.SENDING] == "전송 중"
    assert send_truth.SEND_STATE_KO[send_truth.START_ACKNOWLEDGED] == "작업 중"
    assert send_truth.RESULT_CLASS_KO[send_truth.NEW_RESULT] == "새 결과 있음"
    assert send_truth.RESULT_CLASS_KO[send_truth.STALE_RESULT] == "이전 작업 결과"
    assert send_truth.RESULT_CLASS_KO[send_truth.RESULT_PENDING] == "새 결과 기다리는 중"
    assert send_truth.TRANSPORT_STATE_KO["TRANSPORT_BUSY"] == "원격 통신 대기 중"
