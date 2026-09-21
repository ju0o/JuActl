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
SENDING = "SENDING"
SUBMITTED = "SUBMITTED"
START_ACKNOWLEDGED = "START_ACKNOWLEDGED"
SEND_FAILED = "SEND_FAILED"

# Result classification for COPY RESULT.
NEW_RESULT = "NEW_RESULT"
STALE_RESULT = "STALE_RESULT"
NO_RESULT = "NO_RESULT"
RESULT_PENDING = "RESULT_PENDING"

SEND_STATE_KO = {
    SEND_QUEUED: "전송 대기",
    SENDING: "전송 중",
    SUBMITTED: "제출 완료",
    START_ACKNOWLEDGED: "Agent 작업 시작 확인",
    SEND_FAILED: "전송 실패",
}

RESULT_CLASS_KO = {
    NEW_RESULT: "새 결과 있음",
    STALE_RESULT: "이전 작업 결과",
    NO_RESULT: "결과 없음",
    RESULT_PENDING: "새 결과 기다리는 중",
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
    correlation_id: str | None = None,
) -> SendCorrelation:
    corr = SendCorrelation(
        agent=agent,
        target=target,
        correlation_id=correlation_id or uuid.uuid4().hex[:12],
        send_ts=time.time(),
        previous_result_hash=previous_result_hash,
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
        if corr.previous_result_hash and current_hash == corr.previous_result_hash:
            corr.result_class = RESULT_PENDING
            return RESULT_PENDING, SendCorrelation(**corr.__dict__)
        if corr.previous_result_hash is None:
            # First send with no prior marker: any non-empty result after ack is new
            # only once observed after send_ts; treat as NEW when hash present post-success.
            corr.result_hash_after = current_hash
            corr.result_class = NEW_RESULT
            return NEW_RESULT, SendCorrelation(**corr.__dict__)
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
