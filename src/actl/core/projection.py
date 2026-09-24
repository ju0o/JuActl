"""Read-only dashboard metadata derived from verified runtime evidence."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

UNKNOWN = "UNKNOWN"
RUNTIME_STATES = ("WORKING", "IDLE", "BLOCKED", "DONE", UNKNOWN)
HEALTHY_STATES = frozenset(("UP", "WORKING", "IDLE"))
ERROR_STATES = frozenset(("DOWN", "MISMATCH", "BLOCKED"))


def _string(value: Any) -> str:
    return value.strip() if isinstance(value, str) and value.strip() else UNKNOWN


def _inside(path: str, root: str) -> bool:
    try:
        Path(os.path.realpath(path)).relative_to(Path(os.path.realpath(root)))
        return True
    except (OSError, ValueError):
        return False


def _project_metadata(config: dict, agent: str, pane_path: str) -> tuple[str, dict]:
    projects = config.get("projects")
    if isinstance(projects, dict):
        for name, value in projects.items():
            if not isinstance(value, dict):
                continue
            root = value.get("root")
            if root and _inside(pane_path, str(root)):
                agents = value.get("agents")
                details = agents.get(agent, {}) if isinstance(agents, dict) else {}
                return _string(name), details if isinstance(details, dict) else {}
    project = config.get("project")
    if isinstance(project, str):
        return _string(project), {}
    if not isinstance(project, dict):
        return UNKNOWN, {}
    root = project.get("root")
    if root and not _inside(pane_path, str(root)):
        return UNKNOWN, {}
    name = _string(project.get("name"))
    agents = project.get("agents")
    details = agents.get(agent, {}) if isinstance(agents, dict) else {}
    return name, details if isinstance(details, dict) else {}


def runtime_state(activity_state: str, *, result_state: str = UNKNOWN,
                  overlay: dict | None = None) -> str:
    """Return only states supported by trustworthy evidence."""
    if isinstance(overlay, dict) and overlay.get("durable") is True:
        candidate = overlay.get("runtimeState")
        if candidate in {"BLOCKED", "DONE"}:
            return candidate
    if activity_state == "RUNNING":
        return "WORKING"
    if activity_state == "WAITING_INPUT":
        return "BLOCKED"
    if activity_state == "IDLE":
        return "IDLE"
    return UNKNOWN


def project_metadata(config: dict, agent: str, pane_path: str = "-",
                     *, activity_state: str = UNKNOWN,
                     result_state: str = UNKNOWN,
                     profile: str | None = None,
                     overlay: dict | None = None) -> dict[str, str | bool]:
    """Build dashboard metadata without writing config or contacting tmux."""
    agents = config.get("agents", {})
    agent_config = agents.get(agent, {}) if isinstance(agents, dict) else {}
    if not isinstance(agent_config, dict):
        agent_config = {}
    project, project_agent = _project_metadata(config, agent, pane_path)
    roles = config.get("roles", {})
    role = project_agent.get("role") or agent_config.get("role")
    if not role and isinstance(roles, dict):
        role = roles.get(agent)
    model = (project_agent.get("model") or project_agent.get("profile") or
             agent_config.get("model") or agent_config.get("profile") or profile)
    task_id = overlay.get("taskId") if isinstance(overlay, dict) else None
    return {
        "project": project,
        "role": _string(role),
        "model_profile": _string(model),
        "runtime_state": runtime_state(activity_state, result_state=result_state, overlay=overlay),
        "current_task": _string(task_id),
        "live_pane": pane_path not in {"", "-", UNKNOWN},
    }


def runtime_counts(rows: list[dict]) -> dict[str, int]:
    """Count projected runtime instances, never canonical Agent types."""
    return {state: sum(row.get("runtime_state") == state for row in rows) for state in RUNTIME_STATES}


def board_counts(rows: list[dict]) -> dict[str, int]:
    """Classify board rows using the shared TUI/GUI healthy state mapping."""
    def is_error(row: dict) -> bool:
        return row.get("state") in ERROR_STATES or row.get("runtime_state") == "BLOCKED"

    error = sum(is_error(row) for row in rows)
    healthy = sum(not is_error(row) and
                  (row.get("state") in HEALTHY_STATES or
                   row.get("runtime_state") in {"WORKING", "IDLE", "DONE"}) for row in rows)
    return {"healthy": healthy, "error": error, "unknown": len(rows) - healthy - error}


def project_groups(rows: list[dict]) -> dict[str, list[dict]]:
    """Group the already projected rows without deduplicating them."""
    groups: dict[str, list[dict]] = {}
    for row in rows:
        groups.setdefault(row.get("project") or "UNASSIGNED", []).append(row)
    return groups


def filter_project(rows: list[dict], project: str | None) -> list[dict]:
    """Return all runtime rows for one project, or all rows for None."""
    if project is None:
        return list(rows)
    return [row for row in rows if row.get("project") == project]


def attention_rows(rows: list[dict]) -> list[dict]:
    """Return conditions requiring operator action; UNKNOWN alone is informational."""
    reasons = {"UNMAPPED", "AMBIGUOUS", "MISMATCH", "UNSUPPORTED", "TRANSPORT"}
    return [row for row in rows if row.get("runtime_state") == "BLOCKED" or
            row.get("state") in {"DOWN", "MISMATCH"} or row.get("control_reason") in reasons]
