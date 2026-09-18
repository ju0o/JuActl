"""Small durable operator state; never stores response bodies."""
from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

_LOCK = threading.Lock()


def state_path() -> Path:
    return Path(os.environ.get("ACTL_STATE_PATH", "~/.local/state/actl/state.json")).expanduser()


def load() -> dict[str, Any]:
    try:
        value = json.loads(state_path().read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, TypeError, json.JSONDecodeError):
        return {}


def _seen(value: dict[str, Any]) -> dict[str, str]:
    raw = value.get("seen_results", {})
    return dict(raw) if isinstance(raw, dict) else {}


def save(value: dict[str, Any]) -> None:
    path = state_path()
    with _LOCK:
        _save_unlocked(path, value)


def _save_unlocked(path: Path, value: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.parent.chmod(0o700)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                         prefix=f".{path.name}.", suffix=".tmp", delete=False) as handle:
            temp = Path(handle.name)
            handle.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        temp.replace(path)
        path.chmod(0o600)
    except (OSError, TypeError, ValueError):
        try:
            temp.unlink(missing_ok=True)
        except (OSError, UnboundLocalError):
            pass


def unread(agent: str, result_hash: str) -> bool:
    if not result_hash:
        return False
    return _seen(load()).get(agent) != result_hash


def seen_results() -> dict[str, str]:
    return _seen(load())


def acknowledge(agent: str, result_hash: str) -> None:
    if not result_hash:
        return
    with _LOCK:
        current = load()
        seen = _seen(current)
        seen[agent] = result_hash
        current["seen_results"] = seen
        _save_unlocked(state_path(), current)
