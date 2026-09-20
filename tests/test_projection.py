from actl.core.projection import project_metadata, runtime_state


def test_projection_uses_local_project_and_role_config_without_mutation():
    config = {
        "project": {"name": "Agent-Relay", "root": "/work/Agent-Relay"},
        "agents": {"codex": {"role": "BUILDER", "profile": "default"}},
    }
    result = project_metadata(config, "codex", "/work/Agent-Relay/src", activity_state="RUNNING")
    assert result == {
        "project": "Agent-Relay", "role": "BUILDER", "model_profile": "default",
        "runtime_state": "WORKING", "current_task": "UNKNOWN", "live_pane": True,
    }
    assert config["agents"]["codex"]["role"] == "BUILDER"


def test_projection_unknowns_out_of_project_and_ambiguous_metadata():
    config = {"project": {"name": "Agent-Relay", "root": "/work/Agent-Relay"}, "agents": {}}
    result = project_metadata(config, "opencode", "/work/Other", activity_state="UNKNOWN")
    assert result["project"] == "UNKNOWN"
    assert result["role"] == "UNKNOWN"
    assert result["model_profile"] == "UNKNOWN"
    assert result["runtime_state"] == "UNKNOWN"
    assert result["current_task"] == "UNKNOWN"
    assert result["live_pane"] is True


def test_done_and_blocked_require_durable_overlay():
    assert runtime_state("UNKNOWN", overlay={"runtimeState": "DONE"}) == "UNKNOWN"
    assert runtime_state("UNKNOWN", overlay={"runtimeState": "BLOCKED", "durable": True}) == "BLOCKED"
    assert runtime_state("UNKNOWN", overlay={"runtimeState": "DONE", "durable": True}) == "DONE"
    assert runtime_state("IDLE", result_state="READY") == "IDLE"
