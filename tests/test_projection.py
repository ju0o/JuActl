from actl.core.models import AgentTarget, PaneInfo
from actl.core.projection import filter_project, project_groups, project_metadata, runtime_counts, runtime_state
from actl.core.discovery import Detection
from actl.tui import _rows
import actl.tui as tui_module


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


def test_projection_keeps_duplicate_agent_runtime_instances(monkeypatch):
    config = {
        "projects": {
            "Agent-Relay": {"root": "/work/Agent-Relay", "agents": {"codex": {"role": "PM"}}},
            "actl": {"root": "/work/actl", "agents": {"codex": {"role": "BUILDER"}}},
        },
        "agents": {},
    }
    panes = [
        PaneInfo("%1", "0:0.0", "codex", "/work/Agent-Relay", "", 101),
        PaneInfo("%2", "0:0.1", "codex", "/work/actl", "", 102),
        PaneInfo("%3", "0:0.2", "codex", "/work/actl", "", 103),
        PaneInfo("%4", "0:0.3", "codex", "/work/unknown", "", 104),
    ]
    detections = [Detection(pane, "codex", "high", f"pid {pane.pane_pid}: codex") for pane in panes]
    monkeypatch.setattr(tui_module, "get_target", lambda *_args: (_ for _ in ()).throw(ValueError("unmapped")))
    rows = _rows(config, detections=detections)
    rows = [row for row in rows if row["agent"] == "codex"]
    assert len(rows) == 4
    assert len({row["runtime_key"] for row in rows}) == 4
    assert [row["project"] for row in rows].count("actl") == 2
    assert {row["project"] for row in rows} == {"Agent-Relay", "actl", "UNASSIGNED"}
    assert rows[-1]["role"] == "UNKNOWN"
    assert all(not row["control_ready"] for row in rows)
    assert {row["pane_id"] for row in rows} == {"%1", "%2", "%3", "%4"}
    assert {row["session"] for row in rows} == {"0"}
    assert {row["window"] for row in rows} == {"0"}
    assert {row["pane_index"] for row in rows} == {"0", "1", "2", "3"}
    assert all(row["model_profile"] == "UNKNOWN" for row in rows)


def test_projection_exposes_evidence_backed_claude_profile():
    config = {"agents": {"claude-team": {}}}
    result = project_metadata(config, "claude-team", "/work/Agent-Relay", profile=".claude-team")
    assert result["model_profile"] == ".claude-team"


def test_projection_mismatched_mapping_remains_visible_and_not_actionable(monkeypatch):
    pane = PaneInfo("%1", "0:0.0", "codex", "/work/Agent-Relay", "", 101)
    config = {"project": {"name": "Agent-Relay", "root": "/work/Agent-Relay"},
              "agents": {"codex": {"target": "%2"}}}
    monkeypatch.setattr(tui_module, "get_target", lambda *_args: AgentTarget("codex", "%2"))
    rows = [row for row in _rows(config, detections=[Detection(pane, "codex", "high", "pid 101: codex")])
            if row["agent"] == "codex"]
    assert len(rows) == 1
    assert rows[0]["state"] == "DETECTED"
    assert rows[0]["live_runtime"] is True
    assert rows[0]["control_ready"] is False


def test_fast_projection_keeps_inventory_without_detail_hydration(monkeypatch):
    pane = PaneInfo("%1", "0:0.0", "codex", "/work/Agent-Relay", "", 101)
    config = {"project": {"name": "Agent-Relay", "root": "/work/Agent-Relay"}, "agents": {}}
    monkeypatch.setattr(tui_module, "get_target", lambda *_args: (_ for _ in ()).throw(ValueError("unmapped")))
    rows = _rows(config, detections=[Detection(pane, "codex", "high", "pid 101: codex")], hydrate=False)
    assert len(rows) == 1
    assert rows[0]["live_runtime"] is True
    assert rows[0]["activity_state"] == "UNKNOWN"
    assert rows[0]["result_state"] == "UNKNOWN"
    assert rows[0]["pane_preview"] == ""


def test_runtime_groups_filters_and_counts_use_instances():
    rows = [
        {"project": "actl", "runtime_state": "WORKING"},
        {"project": "actl", "runtime_state": "IDLE"},
        {"project": "UNASSIGNED", "runtime_state": "UNKNOWN"},
    ]
    assert list(map(len, project_groups(rows).values())) == [2, 1]
    assert len(filter_project(rows, "actl")) == 2
    assert len(filter_project(rows, None)) == 3
    assert runtime_counts(rows) == {"WORKING": 1, "IDLE": 1, "BLOCKED": 0, "DONE": 0, "UNKNOWN": 1}
