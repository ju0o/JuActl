from actl.core.projection import board_counts


def test_board_counts_treats_hydrated_states_as_healthy_and_separates_errors():
    rows = [
        {"state": "WORKING", "runtime_state": "WORKING"},
        {"state": "IDLE", "runtime_state": "IDLE"},
        {"state": "UP", "runtime_state": "DONE"},
        {"state": "DOWN", "runtime_state": "UNKNOWN"},
        {"state": "UNKNOWN", "runtime_state": "UNKNOWN"},
        {"state": "UP", "runtime_state": "BLOCKED"},
    ]

    assert board_counts(rows) == {"healthy": 3, "error": 2, "unknown": 1}
