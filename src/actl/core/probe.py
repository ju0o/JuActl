from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from actl.core.registry import AGENTS

SENSITIVE = ("auth", "credential", "token", "secret", "keychain", "control.key", "oauth", "api_key")


def _sensitive(name: str) -> bool:
    lowered = name.lower()
    return lowered == "config.toml" or any(term in lowered for term in SENSITIVE)


def probe_agent(agent: str) -> list[str]:
    spec = AGENTS[agent]
    out: list[str] = []
    for root in spec.data_dirs:
        root = root.expanduser()
        if not root.exists():
            out.append(f"MISSING {root}")
            continue
        out.append(f"DIR     {root}")
        candidates: list[tuple[float, Path]] = []
        for base, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d not in {"node_modules", ".git", "cache", "Cache"} and not _sensitive(d)]
            for name in files:
                if _sensitive(name):
                    continue
                p = Path(base) / name
                try:
                    candidates.append((p.stat().st_mtime, p))
                except OSError:
                    pass
        candidates.sort(reverse=True)
        for _, p in candidates[:12]:
            suffix = p.suffix.lower()
            if suffix == ".db":
                try:
                    con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
                    try:
                        tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
                    finally:
                        con.close()
                    out.append(f"  DB    {p} tables={','.join(tables[:20])}")
                except sqlite3.Error:
                    out.append(f"  DB    {p} tables=<unreadable>")
            else:
                out.append(f"  FILE  {p}")
    return out
