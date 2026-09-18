"""Small, process-local state transition detector for operator surfaces."""
from __future__ import annotations


def detect_events(previous: dict[str, dict], current: list[dict]) -> list[dict[str, str]]:
    """Report only observed activity/result transitions; never infer missing data."""
    events: list[dict[str, str]] = []
    for row in current:
        agent = row.get("agent", "")
        before = previous.get(agent, {})
        activity = row.get("activity_state")
        old_activity = before.get("activity_state")
        if old_activity == "RUNNING" and activity == "IDLE":
            events.append({"agent": agent, "kind": "IDLE", "detail": "작업이 유휴 상태가 됨"})
        result_hash = row.get("result_hash")
        if result_hash and result_hash != before.get("result_hash"):
            events.append({"agent": agent, "kind": "RESULT", "detail": "새 결과가 준비됨"})
    return events
