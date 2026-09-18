from __future__ import annotations

import json
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from actl.core.models import AgentTarget
from actl.core.registry import AGENTS

CONFIG_PATH = Path(os.environ.get("ACTL_CONFIG_PATH", str(Path.home() / ".config/actl/config.json"))).expanduser()

DEFAULT_CONFIG = {
    "clipboard_backend": "auto",
    "agents": {name: {} for name in AGENTS},
}


def ensure_config() -> Path:
    if not CONFIG_PATH.exists():
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, indent=2) + "\n", encoding="utf-8")
    return CONFIG_PATH


def load_config() -> dict:
    path = ensure_config()
    return json.loads(path.read_text(encoding="utf-8"))


def save_config(config: dict) -> None:
    ensure_config().write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def backup_config() -> Path:
    path = ensure_config()
    backup = path.with_name(f"{path.name}.bak-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(path, backup)
    return backup


def get_target(config: dict, agent: str) -> AgentTarget:
    item = config.get("agents", {}).get(agent)
    if not item:
        raise ValueError(f"Agent is not currently mapped to a tmux pane: {agent}")
    if item.get("target"):
        return AgentTarget(agent, str(item["target"]))
    session = str(config.get("tmux_session", "agents"))
    window = str(config.get("window", "0"))
    pane = item.get("pane")
    if pane is None:
        raise ValueError(f"Agent is not currently mapped to a tmux pane: {agent}")
    return AgentTarget(agent, f"{session}:{window}.{pane}")


def migration_warning(config: dict[str, Any]) -> str | None:
    """Return a non-mutating warning for the former single-Claude configuration."""
    agents = config.get("agents", {})
    if "claude" in agents and not ({"claude-team", "claude-pro"} & set(agents)):
        return (
            "Config migration required: legacy 'claude' was not assigned to Team or Pro. "
            "No target was changed. Run /discover and explicitly configure claude-team and claude-pro."
        )
    return None
