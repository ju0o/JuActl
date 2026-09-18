"""Number-key agent board: preview each live pane, copy with one key.

No curses dependency — plain ANSI + stdin. Works over plain SSH where
curses/OSC52 may be limited. ``--print`` paths avoid the clipboard entirely.
"""
from __future__ import annotations

import sys

from actl.agents.extract import extract_last_response
from actl.core.config import get_target, load_config
from actl.core.discovery import STRONG_CONFIDENCE, discover, mapping_state
from actl.core.registry import AGENTS
from actl.core.tmux import capture_pane
from actl.core.validation import validate_target
from actl.utils.clipboard import copy_text

CLEAR = "\x1b[2J\x1b[H"
BOLD = "\x1b[1m"
DIM = "\x1b[2m"
RESET = "\x1b[0m"


def _rows(config: dict) -> list[dict]:
    """One row per agent: live target, status, and last response preview."""
    detections = {d.agent: d for d in discover() if d.agent}
    rows: list[dict] = []
    for idx, name in enumerate(AGENTS, 1):
        spec = AGENTS[name]
        target = "-"
        state = "UNMAPPED"
        try:
            target = get_target(config, name).target
            state = validate_target(name, target).state
        except ValueError:
            det = detections.get(name)
            if det and det.confidence in STRONG_CONFIDENCE:
                target = f"{det.pane.pane_id}?"
                state = "DETECTED"
        preview = ""
        detail = ""
        if target not in {"-", } and not target.endswith("?"):
            try:
                result = extract_last_response(name, target, config)
                if result.text:
                    first = result.text.strip().splitlines()[0] if result.text.strip() else ""
                    preview = first[:100]
                else:
                    detail = result.detail or "no text"
            except Exception as exc:
                detail = str(exc)[:80]
        rows.append(
            {
                "key": str(idx),
                "agent": name,
                "display": spec.display_name,
                "target": target,
                "state": state,
                "preview": preview,
                "detail": detail,
            }
        )
    return rows


def _render(rows: list[dict], selected: int, message: str = "") -> None:
    sys.stdout.write(CLEAR)
    sys.stdout.write(f"{BOLD}actl — agent board{DIM}  (number = select, c = copy, p = print, r = refresh, q = quit){RESET}\n\n")
    for i, row in enumerate(rows):
        marker = ">" if i == selected else " "
        state_color = "" if row["state"] == "UP" else DIM
        sys.stdout.write(
            f"{marker} [{row['key']}] {state_color}{row['display']:<12} {row['target']:<6} {row['state']:<9}{RESET} {row['preview'] or row['detail']}\n"
        )
    if message:
        sys.stdout.write(f"\n{message}\n")
    sys.stdout.flush()


def _pane_preview(target: str, lines: int = 12) -> str:
    try:
        text = capture_pane(target, history=60)
    except Exception as exc:
        return f"(preview unavailable: {exc})"
    kept = [ln for ln in text.splitlines() if ln.strip()][-lines:]
    return "\n".join(kept) or "(empty pane)"


def run_tui() -> int:
    import termios
    import tty

    config = load_config()
    try:
        from actl.cli import _auto_reconcile

        config = _auto_reconcile(config)
    except Exception:
        pass
    rows = _rows(config)
    selected = 0
    message = ""
    _render(rows, selected)
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        while True:
            import os

            ch = os.read(fd, 1).decode("utf-8", "replace")
            if ch in {"q", "\x03"}:
                sys.stdout.write("\n")
                return 0
            if ch.isdigit():
                idx = int(ch) - 1
                if 0 <= idx < len(rows):
                    selected = idx
                    row = rows[selected]
                    tgt = row["target"]
                    if tgt not in {"-", } and not tgt.endswith("?"):
                        message = f"--- {row['display']} {tgt} live pane ---\n{_pane_preview(tgt)}"
                    else:
                        det_state = row["state"]
                        message = f"{row['display']}: no live pane ({det_state}). Press r after starting the agent."
                    _render(rows, selected, message)
                continue
            if ch == "r":
                config = load_config()
                try:
                    from actl.cli import _auto_reconcile

                    config = _auto_reconcile(config)
                except Exception:
                    pass
                rows = _rows(config)
                message = "refreshed"
                _render(rows, selected, message)
                continue
            if ch in {"c", "p"}:
                row = rows[selected]
                tgt = row["target"]
                if tgt in {"-", } or tgt.endswith("?"):
                    message = f"✗ {row['display']} has no live pane"
                    _render(rows, selected, message)
                    continue
                try:
                    result = extract_last_response(row["agent"], tgt, config)
                except Exception as exc:
                    message = f"✗ {exc}"
                    _render(rows, selected, message)
                    continue
                if not result.text:
                    message = f"✗ No response text ({result.detail or 'empty'})"
                    _render(rows, selected, message)
                    continue
                if ch == "p":
                    message = f"--- {row['display']} last response ---\n{result.text[:2000]}"
                    _render(rows, selected, message)
                    continue
                try:
                    backend = copy_text(result.text, preferred=config.get("clipboard_backend", "auto"))
                    message = f"✓ {row['display']} copied via {backend} ({len(result.text)} chars)"
                except Exception as exc:
                    message = f"✗ Clipboard failed: {exc} — press p to print instead"
                _render(rows, selected, message)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
