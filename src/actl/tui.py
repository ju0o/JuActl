"""Agent board for MainPC: select, preview, copy, remap, send.

No curses dependency — plain ANSI + stdin. Works over plain SSH where
curses/OSC52 may be limited. ``--print`` paths avoid the clipboard entirely.

Keys:
  number      select agent, show live pane preview
  c / p       copy to clipboard / print last response
  m           remap selected agent to another live pane (visual list)
  s           send a message to the selected agent's pane
  r           refresh (auto-reconcile + rescan)
  q           quit
"""
from __future__ import annotations

import sys

from actl.agents.extract import extract_last_response
from actl.core.config import backup_config, get_target, load_config, save_config
from actl.core.discovery import STRONG_CONFIDENCE, discover, manual_map, mapping_state
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
        if target != "-" and not target.endswith("?"):
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
    sys.stdout.write(
        f"{BOLD}actl — agent board{DIM}  (number=select+preview, c=copy, p=print, "
        f"m=remap, s=send, r=refresh, q=quit){RESET}\n\n"
    )
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


def _unmapped_panes(config: dict, agent: str) -> list:
    """Live strong detections for agent on panes not mapped to another agent."""
    from actl.core.discovery import target_matches

    dets = [d for d in discover() if d.agent == agent and d.confidence in STRONG_CONFIDENCE]
    free = []
    for det in dets:
        occupied = False
        for other in AGENTS:
            if other == agent:
                continue
            try:
                other_target = get_target(config, other).target
            except ValueError:
                continue
            if target_matches(det.pane, other_target):
                occupied = True
                break
        if not occupied:
            free.append(det)
    return free


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
                    if tgt != "-" and not tgt.endswith("?"):
                        message = f"--- {row['display']} {tgt} live pane ---\n{_pane_preview(tgt)}"
                    else:
                        message = (
                            f"{row['display']}: no live pane ({row['state']}). "
                            "Press m to pick a pane, r to refresh."
                        )
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
            if ch == "m":
                row = rows[selected]
                termios.tcsetattr(fd, termios.TCSADRAIN, old)
                try:
                    sys.stdout.write(f"\nLive {row['display']} panes (0 = cancel):\n")
                    candidates = _unmapped_panes(config, row["agent"])
                    if not candidates:
                        message = f"No free live {row['display']} panes"
                    else:
                        for i, det in enumerate(candidates, 1):
                            sys.stdout.write(
                                f"  [{i}] {det.pane.pane_id} {det.pane.current_path} — {det.evidence}\n"
                            )
                        sys.stdout.write("Pick pane: ")
                        sys.stdout.flush()
                        choice = sys.stdin.readline().strip()
                        if choice == "0" or not choice:
                            message = "remap cancelled"
                        else:
                            det = None
                            if choice.startswith("%"):
                                det = next((d for d in candidates if d.pane.pane_id == choice), None)
                            else:
                                try:
                                    det = candidates[int(choice) - 1]
                                except (ValueError, IndexError):
                                    det = None
                            if det is None:
                                message = "✗ Invalid pane selection"
                            else:
                                try:
                                    updated = manual_map(config, row["agent"], det)
                                except ValueError as exc:
                                    message = f"✗ {exc}"
                                else:
                                    backup = backup_config()
                                    save_config(updated)
                                    config = updated
                                    rows = _rows(config)
                                    message = (
                                        f"✓ {row['display']} mapped to {det.pane.pane_id} "
                                        f"(backup {backup.name})"
                                    )
                finally:
                    tty.setcbreak(fd)
                _render(rows, selected, message)
                continue
            if ch == "s":
                row = rows[selected]
                tgt = row["target"]
                if tgt == "-" or tgt.endswith("?"):
                    message = f"✗ {row['display']} has no live pane (press m first)"
                    _render(rows, selected, message)
                    continue
                termios.tcsetattr(fd, termios.TCSADRAIN, old)
                send_message = ""
                try:
                    sys.stdout.write(f"\nMessage to {row['display']} ({tgt}), end with a line '::send':\n")
                    sys.stdout.flush()
                    lines: list[str] = []
                    cancelled = False
                    while True:
                        line = sys.stdin.readline()
                        if not line:
                            send_message = "✗ Send cancelled (EOF)"
                            cancelled = True
                            break
                        line = line.rstrip("\n")
                        if line == "::send":
                            break
                        if line == "::cancel":
                            send_message = "✗ Send cancelled"
                            cancelled = True
                            break
                        lines.append(line)
                    if not cancelled:
                        prompt = "\n".join(lines)
                        if not prompt:
                            send_message = "✗ Empty message"
                        else:
                            from actl.cli import _send_to_selected

                            try:
                                _send_to_selected(config, row["agent"], prompt)
                                send_message = f"✓ Sent to {row['display']}"
                            except Exception as exc:
                                send_message = f"✗ {exc}"
                finally:
                    tty.setcbreak(fd)
                message = send_message
                _render(rows, selected, message)
                continue
            if ch in {"c", "p"}:
                row = rows[selected]
                tgt = row["target"]
                if tgt == "-" or tgt.endswith("?"):
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
