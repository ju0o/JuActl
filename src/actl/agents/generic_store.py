from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterable

from actl.core.models import CopyResult

DENY_NAMES = {"config.toml"}
SENSITIVE_TERMS = ("auth", "credential", "token", "secret", "keychain", "control.key", "oauth", "api_key")
EXTS = {".json", ".jsonl", ".ndjson", ".log"}


def _extract_text_content(content: Any) -> str | None:
    if isinstance(content, str) and content.strip():
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                value = item.get("text") or item.get("content")
                if isinstance(value, str):
                    parts.append(value)
        text = "\n".join(p for p in parts if p.strip()).strip()
        return text or None
    if isinstance(content, dict):
        value = content.get("text")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _find_assistant(obj: Any, out: list[str]) -> None:
    if isinstance(obj, dict):
        role = str(obj.get("role", obj.get("type", ""))).lower()
        if role in {"assistant", "assistant_message", "model"}:
            for key in ("content", "message", "text", "output"):
                text = _extract_text_content(obj.get(key))
                if text:
                    out.append(text)
                    break
        for value in obj.values():
            _find_assistant(value, out)
    elif isinstance(obj, list):
        for item in obj:
            _find_assistant(item, out)


def _candidate_files(dirs: Iterable[Path], max_files: int = 80) -> list[Path]:
    files: list[tuple[float, Path]] = []
    for root in dirs:
        root = root.expanduser()
        if not root.exists():
            continue
        for base, subdirs, names in os.walk(root):
            # Avoid obviously sensitive/cache-heavy trees while remaining read-only.
            subdirs[:] = [
                d for d in subdirs
                if d not in {"node_modules", ".git", "cache", "Cache"}
                and not any(term in d.lower() for term in SENSITIVE_TERMS)
            ]
            for name in names:
                lowered = name.lower()
                if lowered in DENY_NAMES or any(term in lowered for term in SENSITIVE_TERMS):
                    continue
                path = Path(base) / name
                if path.suffix.lower() not in EXTS:
                    continue
                try:
                    if path.stat().st_size > 20 * 1024 * 1024:
                        continue
                    files.append((path.stat().st_mtime, path))
                except OSError:
                    continue
    files.sort(reverse=True)
    return [p for _, p in files[:max_files]]


def extract_from_dirs(dirs: Iterable[Path]) -> CopyResult | None:
    for path in _candidate_files(dirs):
        try:
            raw = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        found: list[str] = []
        if path.suffix.lower() in {".jsonl", ".ndjson", ".log"}:
            for line in raw.splitlines()[-3000:]:
                try:
                    _find_assistant(json.loads(line), found)
                except Exception:
                    continue
        else:
            try:
                _find_assistant(json.loads(raw), found)
            except Exception:
                continue
        if found:
            return CopyResult(found[-1], f"session:{path}", "medium")
    return None
