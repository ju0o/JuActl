from actl import gui


def test_card_line_is_plain_and_bounded():
    line = gui._card_line({
        "state": "UP",
        "activity_state": "RUNNING",
        "project": "UNASSIGNED",
        "preview": "RUNNING UNKNOWN % " + "x" * 100,
    })
    assert "UNKNOWN" not in line
    assert "RUNNING" not in line
    assert "%" not in line
    assert len(line.rsplit(" · ", 1)[-1]) <= 60


def test_inspector_detail_is_korean_and_hides_machine_fields():
    truth = gui.inspector_truth({
        "agent": "codex",
        "display": "Codex",
        "state": "UP",
        "activity_state": "IDLE",
        "project": "juactl",
        "role": "UNKNOWN",
        "pane_id": "%33",
        "pane_pid": "123",
        "pane_command": "codex",
    })
    assert len(truth["detail"].splitlines()) == 2
    assert "PID" not in truth["detail"]
    assert "Machine" not in truth["detail"]
    assert "역할: UNKNOWN" not in truth["detail"]


def test_board_has_no_undefined_card_move_method():
    assert not hasattr(gui.Board, "_move_card")
