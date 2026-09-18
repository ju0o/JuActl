from __future__ import annotations

import re

ANSI_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text).replace("\r", "")


def visible_tail(text: str, max_lines: int = 120) -> str:
    lines = [line.rstrip() for line in strip_ansi(text).splitlines()]
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines[-max_lines:]).strip()
