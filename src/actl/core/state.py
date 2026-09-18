"""Small durable operator state; never stores response bodies."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def state_path() -> Path:
    return Path(os.environ.get("ACTL_STATE_PATH", "~/.local/state/actl/state.json")).expanduser()


def load() -> dict[str, Any]:
    try:
        value = json.loads(state_path().read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save(value: dict[str, Any]) -> None:
    path = state_path()
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.parent.chmod(0o700)
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
        temp.replace(path)
        path.chmod(0o600)
    except OSError:
        return


def unread(agent: str, result_hash: str) -> bool:
    if not result_hash:
        return False
    return load().get("seen_results", {}).get(agent) != result_hash


def seen_results() -> dict[str, str]:
    value = load().get("seen_results", {})
    return dict(value) if isinstance(value, dict) else {}


def acknowledge(agent: str, result_hash: str) -> None:
    if not result_hash:
        return
    current = load()
    seen = dict(current.get("seen_results", {}))
    seen[agent] = result_hash
    current["seen_results"] = seen
    save(current)
