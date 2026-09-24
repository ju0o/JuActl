"""Send truth states and result correlation for Board / remote control.

Tracks evidence that a prompt was actually submitted and correlates COPY RESULT
against the most recent successful send — never treating a prior pane marker as
a new result for a failed task.
"""
from __future__ import annotations

import hashlib
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

# Send lifecycle (evidence-gated).
SEND_QUEUED = "SEND_QUEUED"
CLEARING_BACKGROUND = "CLEARING_BACKGROUND"
SENDING = "SENDING"
SUBMITTED = "SUBMITTED"
START_ACKNOWLEDGED = "START_ACKNOWLEDGED"
SEND_FAILED = "SEND_FAILED"
FOREGROUND_ACQUIRE_TIMEOUT = "FOREGROUND_ACQUIRE_TIMEOUT"
DELIVERY_AMBIGUOUS = "DELIVERY_AMBIGUOUS"
DELIVERY_AMBIGUOUS_MESSAGE = "보냈는지 확실하지 않아요 — ASUS 화면 전환으로 확인하세요"

# Result classification for COPY RESULT.
NEW_RESULT = "NEW_RESULT"
STALE_RESULT = "STALE_RESULT"
NO_RESULT = "NO_RESULT"
RESULT_PENDING = "RESULT_PENDING"

# COPY progress phases (Founder-facing; independent of result class).
COPY_QUEUED = "COPY_QUEUED"
COPY_CLEARING = "COPY_CLEARING"
COPY_READING = "COPY_READING"
COPY_ACQUIRE_TIMEOUT = "COPY_ACQUIRE_TIMEOUT"

SEND_STATE_KO = {
    SEND_QUEUED: "전송 대기",
    CLEARING_BACKGROUND: "백그라운드 작업 정리 중",
    SENDING: "전송 중",
    SUBMITTED: "제출 완료",
    START_ACKNOWLEDGED: "작업 중",
    SEND_FAILED: "전송 실패",
    FOREGROUND_ACQUIRE_TIMEOUT: "전송 지연: 백그라운드 정리 초과",
}

# Founder-facing loop phases (selected-runtime interaction).
LOOP_READY = "READY"
LOOP_SENDING = "SENDING"
LOOP_SUBMITTED = "SUBMITTED"
LOOP_WORKING = "WORKING"
LOOP_RESULT_READY = "RESULT_READY"
LOOP_COPIED = "COPIED"

LOOP_STATE_KO = {
    LOOP_READY: "준비",
    LOOP_SENDING: "전송 중",
    LOOP_SUBMITTED: "제출 완료",
    LOOP_WORKING: "작업 중",
    LOOP_RESULT_READY: "결과 준비됨",
    LOOP_COPIED: "복사 완료",
}

COPY_STATE_KO = {
    COPY_QUEUED: "복사 대기",
    COPY_CLEARING: "백그라운드 작업 정리 중",
    COPY_READING: "결과 읽는 중",
    COPY_ACQUIRE_TIMEOUT: "복사 지연: 백그라운드 정리 초과",
}

RESULT_CLASS_KO = {
    NEW_RESULT: "새 결과 있음",
    STALE_RESULT: "이전 작업 결과",
    NO_RESULT: "결과 없음",
    RESULT_PENDING: "새 결과 기다리는 중",
}


def clipboard_write_allowed(result_class: str) -> bool:
    """Clipboard mutation is authorized only for NEW_RESULT."""
    return result_class == NEW_RESULT


def delivery_ambiguous(evidence: dict[str, Any] | None) -> bool:
    """Return true only when input may already have reached the target."""
    if not evidence:
        return False
    disposition = evidence.get("deliveryDisposition") or evidence.get("disposition")
    side_effect = evidence.get("sideEffect")
    return disposition == DELIVERY_AMBIGUOUS and side_effect in {
        "POSSIBLE_INPUT", "POSSIBLE", "INPUT_OBSERVED",
    }


TRANSPORT_STATE_KO = {
    "UP": "정상",
    "WORKING": "작업 중",
    "IDLE": "대기",
    "TRANSPORT_BUSY": "원격 통신 대기 중",
    "DEGRADED": "통신 저하",
    "DOWN": "꺼짐",
    "UNKNOWN": "미확인",
    "MISMATCH": "불일치",
    "UNMAPPED": "미매핑",
    "DETECTED": "감지됨",
}


@dataclass
class SendCorrelation:
    agent: str
    target: str
    correlation_id: str
    send_ts: float
    previous_result_hash: str | None = None
    result_hash_after: str | None = None
    busy_at_send: bool = False
    busy_transition_hash: str | None = None
    send_state: str = SEND_QUEUED
    ack_state: str = "NONE"
    result_class: str = RESULT_PENDING
    last_error: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def send_succeeded(self) -> bool:
        return self.send_state in {SUBMITTED, START_ACKNOWLEDGED}


_LOCK = threading.Lock()
# Keyed by agent|target so Board instances share correlation for the same pane.
_CORRELATIONS: dict[str, SendCorrelation] = {}


def _key(agent: str, target: str) -> str:
    return f"{agent}|{target}"


def result_hash(text: str | None) -> str | None:
    if not text:
        return None
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def get_correlation(agent: str, target: str) -> SendCorrelation | None:
    with _LOCK:
        item = _CORRELATIONS.get(_key(agent, target))
        return None if item is None else SendCorrelation(**item.__dict__)


def begin_send(
    agent: str,
    target: str,
    *,
    previous_result_hash: str | None,
    busy_at_send: bool = False,
    correlation_id: str | None = None,
) -> SendCorrelation:
    corr = SendCorrelation(
        agent=agent,
        target=target,
        correlation_id=correlation_id or uuid.uuid4().hex[:12],
        send_ts=time.time(),
        previous_result_hash=previous_result_hash,
        busy_at_send=busy_at_send,
        send_state=SEND_QUEUED,
        ack_state="NONE",
        result_class=RESULT_PENDING,
    )
    with _LOCK:
        _CORRELATIONS[_key(agent, target)] = corr
    return SendCorrelation(**corr.__dict__)


def set_send_state(
    agent: str,
    target: str,
    state: str,
    *,
    error: str = "",
    evidence: dict[str, Any] | None = None,
) -> SendCorrelation | None:
    with _LOCK:
        corr = _CORRELATIONS.get(_key(agent, target))
        if corr is None:
            return None
        corr.send_state = state
        if error:
            corr.last_error = error
        if evidence:
            corr.evidence.update(evidence)
        if state == SEND_FAILED:
            # Failed send must not advance task/result correlation.
            corr.result_class = (
                STALE_RESULT if corr.previous_result_hash else NO_RESULT
            )
            corr.ack_state = "NONE"
        elif state == START_ACKNOWLEDGED:
            corr.ack_state = "ACKED"
            corr.result_class = RESULT_PENDING
        elif state == SUBMITTED:
            corr.ack_state = "SUBMITTED"
            corr.result_class = RESULT_PENDING
        return SendCorrelation(**corr.__dict__)


def classify_result(
    agent: str,
    target: str,
    current_hash: str | None,
    *,
    text: str | None = None,
) -> tuple[str, SendCorrelation | None]:
    """Return (result_class, correlation snapshot) for COPY RESULT."""
    del text  # reserved for future content-aware checks
    with _LOCK:
        corr = _CORRELATIONS.get(_key(agent, target))
        if corr is None:
            if not current_hash:
                return NO_RESULT, None
            # No successful send tracked — any pane text is prior work.
            return STALE_RESULT, None
        if not corr.send_succeeded:
            if not current_hash:
                corr.result_class = NO_RESULT
            else:
                corr.result_class = STALE_RESULT
            return corr.result_class, SendCorrelation(**corr.__dict__)
        if not current_hash:
            corr.result_class = NO_RESULT
            return NO_RESULT, SendCorrelation(**corr.__dict__)
        # Already observed/copied as NEW for this send — duplicate COPY is not NEW.
        if corr.result_hash_after is not None and current_hash == corr.result_hash_after:
            corr.result_class = STALE_RESULT
            return STALE_RESULT, SendCorrelation(**corr.__dict__)
        if corr.previous_result_hash and current_hash == corr.previous_result_hash:
            corr.result_class = RESULT_PENDING
            return RESULT_PENDING, SendCorrelation(**corr.__dict__)
        if corr.previous_result_hash is None:
            # First send with no prior marker: any non-empty result after ack is new
            # only once observed after send_ts; treat as NEW when hash present post-success.
            corr.result_hash_after = current_hash
            corr.result_class = NEW_RESULT
            return NEW_RESULT, SendCorrelation(**corr.__dict__)
        if corr.busy_at_send:
            if current_hash == corr.previous_result_hash:
                corr.result_class = RESULT_PENDING
                return RESULT_PENDING, SendCorrelation(**corr.__dict__)
            if corr.busy_transition_hash is None:
                # A busy send can first observe the previous job's late output.
                corr.busy_transition_hash = current_hash
                corr.result_class = RESULT_PENDING
                return RESULT_PENDING, SendCorrelation(**corr.__dict__)
            if current_hash == corr.busy_transition_hash:
                corr.result_class = RESULT_PENDING
                return RESULT_PENDING, SendCorrelation(**corr.__dict__)
            corr.busy_at_send = False
        if current_hash != corr.previous_result_hash:
            corr.result_hash_after = current_hash
            corr.result_class = NEW_RESULT
            return NEW_RESULT, SendCorrelation(**corr.__dict__)
        corr.result_class = RESULT_PENDING
        return RESULT_PENDING, SendCorrelation(**corr.__dict__)


def clear_correlation(agent: str, target: str) -> None:
    with _LOCK:
        _CORRELATIONS.pop(_key(agent, target), None)


def reset_all_correlations() -> None:
    with _LOCK:
        _CORRELATIONS.clear()
