from actl import gui


def test_send_failure_text_hides_raw_detail():
    details = ("send failed", "paste/enter evidence missing", "TmuxError", "매핑 DOWN")

    for detail in details:
        assert detail not in gui._send_failure_text(detail)
        assert detail not in gui._send_failure_text(detail, contention=True)

    assert gui._send_failure_text("send failed") == "보내지 못했어요 — 잠시 후 다시 보내기를 눌러 주세요"
    assert gui._send_failure_text("TmuxError", contention=True) == (
        "ASUS가 다른 작업 중이라 보내지 못했어요 — 잠시 후 다시 보내 주세요"
    )
