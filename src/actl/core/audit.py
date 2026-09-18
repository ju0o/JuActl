"""Small, local, content-free audit trail for operator actions."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

_PRIVATE_FIELDS = {"prompt", "text", "response", "content", "body", "raw"}
_MAX_BYTES = 5 * 1024 * 1024

def audit_path() -> Path:
    return Path(os.environ.get("ACTL_AUDIT_PATH", "~/.local/state/actl/audit.jsonl")).expanduser()


def record(event: str, **fields: Any) -> None:
    """Append metadata only; never persist prompts or response bodies."""
    row = {"ts": time.time(), "event": event}
    for key, value in fields.items():
        if key in _PRIVATE_FIELDS:
            continue
        if key == "source" and isinstance(value, str):
            value = value.split(":", 1)[0]
        row[key] = value
    path = audit_path()
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            path.parent.chmod(0o700)
        except OSError:
            pass
        if path.exists() and path.stat().st_size >= _MAX_BYTES:
            rotated = path.with_name(path.name + ".1")
            try:
                rotated.unlink(missing_ok=True)
                path.replace(rotated)
            except OSError:
                pass
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
        try:
            path.chmod(0o600)
        except OSError:
            pass
    except OSError:
        # Audit failure must not break a safe copy/send operation.
        return


def read(limit: int = 50) -> list[dict[str, Any]]:
    if limit < 1:
        return []
    try:
        lines = audit_path().read_text(encoding="utf-8").splitlines()[-limit:]
    except OSError:
        return []
    rows: list[dict[str, Any]] = []
    for line in lines:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows
