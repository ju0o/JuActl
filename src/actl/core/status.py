from __future__ import annotations

import shutil
from pathlib import Path

from actl.core.config import get_target
from actl.core.activity import observe_activity
from actl.core.registry import AGENTS
from actl.core.validation import validate_target


def _binary_exists(candidate: str) -> bool:
    expanded = str(Path(candidate).expanduser())
    if "/" in expanded:
        return Path(expanded).exists()
    return shutil.which(expanded) is not None


def agent_status(config: dict, agent: str) -> dict[str, str]:
    spec = AGENTS[agent]
    try:
        target = get_target(config, agent).target
    except ValueError:
        binary = next((b for b in spec.binary_candidates if _binary_exists(b)), "not found")
        return {
            "agent": spec.display_name, "target": "-", "pane": "UNMAPPED",
            "command": "-", "path": "-", "binary": binary,
            "activity": "UNKNOWN", "activity_detail": "unmapped",
        }
    validation = validate_target(agent, target)
    binary = next((b for b in spec.binary_candidates if _binary_exists(b)), "not found")
    activity, activity_detail = observe_activity(target) if validation.valid else ("UNKNOWN", "target-not-up")
    return {
        "agent": spec.display_name,
        "target": target,
        "pane": validation.state,
        "command": validation.command,
        "path": validation.path,
        "binary": binary,
        "detail": validation.detail,
        "activity": activity,
        "activity_detail": activity_detail,
    }
