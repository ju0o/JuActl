from actl.core.events import detect_events


def test_detect_events_reports_only_observed_transitions():
    previous = {"codex": {"activity_state": "RUNNING", "result_hash": "old"}}
    current = [{"agent": "codex", "activity_state": "IDLE", "result_hash": "new"}]
    assert detect_events(previous, current) == [
        {"agent": "codex", "kind": "IDLE", "detail": "작업이 유휴 상태가 됨"},
        {"agent": "codex", "kind": "RESULT", "detail": "새 결과가 준비됨"},
    ]


def test_detect_events_does_not_promote_unknown_or_empty_results():
    assert detect_events({"codex": {"activity_state": "UNKNOWN", "result_hash": ""}},
                         [{"agent": "codex", "activity_state": "UNKNOWN", "result_hash": ""}]) == []
