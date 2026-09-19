from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class AgentSpec:
    name: str
    display_name: str
    aliases: tuple[str, ...]
    binary_candidates: tuple[str, ...]
    data_dirs: tuple[Path, ...] = ()
    official_copy_command: str | None = None


@dataclass(frozen=True)
class AgentTarget:
    name: str
    target: str


@dataclass(frozen=True)
class CopyResult:
    text: str | None
    source: str
    confidence: str
    detail: str = ""


@dataclass(frozen=True)
class PaneInfo:
    pane_id: str
    target: str
    current_command: str
    current_path: str
    title: str
    pane_pid: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "pane_id": self.pane_id,
            "target": self.target,
            "current_command": self.current_command,
            "current_path": self.current_path,
            "title": self.title,
            "pane_pid": self.pane_pid,
        }
