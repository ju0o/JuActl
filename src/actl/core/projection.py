"""Read-only dashboard metadata derived from verified runtime evidence."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

UNKNOWN = "UNKNOWN"


def _string(value: Any) -> str:
    return value.strip() if isinstance(value, str) and value.strip() else UNKNOWN


def _inside(path: str, root: str) -> bool:
    try:
        Path(os.path.realpath(path)).relative_to(Path(os.path.realpath(root)))
        return True
    except (OSError, ValueError):
        return False


def _project_metadata(config: dict, agent: str, pane_path: str) -> tuple[str, dict]:
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
