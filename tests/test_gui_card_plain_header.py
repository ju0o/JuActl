from actl import gui


def test_card_header_hides_unknown_role_and_localizes_runtime_state():
    assert gui._card_header({"display": "Codex", "role": "UNKNOWN", "runtime_state": "BLOCKED"}) == "Codex · 연결 끊김"
    assert gui._card_header({"display": "CommandCode", "role": "Builder", "runtime_state": "WORKING"}) == "CommandCode · Builder · 일하는 중"


def test_card_line_uses_safe_fallback_and_send_state():
    row = {
        "runtime_key": "rk",
        "activity_state": "IDLE",
        "result_state": "READY",
        "project": "juactl",
        "detail": "No Codex rollout in this pane directory was active during the th",
        "preview": "no text",
    }
    assert gui._card_line(row, send_inflight=True) == "작업 중 · juactl · 아직 답 없음"
    row["activity_state"] = "RUNNING"
    assert gui._card_line(row, post_send_running_keys={"rk"}) == "작업 중 · juactl · 아직 답 없음"

    row.update(activity_state="IDLE", _result_ready=True)
    assert gui._card_line(row, post_send_running_keys={"rk"}) == "답이 왔어요 · 결과 복사"
