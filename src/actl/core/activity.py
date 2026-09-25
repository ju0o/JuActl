"""Conservative, read-only pane liveness classification."""
from __future__ import annotations

import subprocess
import time


_CPU_SAMPLES: dict[str, tuple[float, float]] = {}


def classify_activity(cpu_percent: float | None, pane_text: str | None) -> str:
    """Return observed activity; never infer idle from missing evidence."""
    if pane_text is None:
        return "UNKNOWN"
    lines = [line.strip().lower() for line in pane_text.splitlines() if line.strip()]
    if not lines:
        return "UNKNOWN"
    if any(phrase in line for line in lines[-8:] for phrase in (
        "would you like to run", "do you want to", "proceed?",
        "press enter to confirm", "(y/n)", "allow this",
    )):
        return "WAITING_INPUT"
    if cpu_percent is None:
        return "UNKNOWN"
    tail = lines[-1]
    if any(token in tail for token in (
        "working", "thinking", "generating", "running", "esc to interrupt",
        "ctrl-c to interrupt", "ctrl+c to interrupt", "tool call",
    )):
        return "RUNNING"
    if tail.endswith(("❯", "›", ">", "$")):
        return "IDLE"
    if cpu_percent > 5.0:
        return "RUNNING"
    return "UNKNOWN"


def observe_activity(pane_id: str) -> tuple[str, str]:
    """Read one pane's activity without sending input or changing config."""
    try:
        from actl.core.tmux import _no_window, _remote_args, capture_pane, pane_field

        pane_pid = int(pane_field(pane_id, "#{pane_pid}"))
        def cpu_time() -> int:
            proc = subprocess.run(
                _remote_args(["ps", "-o", "times=", "-g", str(pane_pid)]),
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=5, **_no_window(),
            )
            return sum(
                sum(int(part) * multiplier for part, multiplier in zip(
                    reversed(value.split(":")), (1, 60, 3600, 86400), strict=False
                ))
                for value in proc.stdout.split()
            )

        total = cpu_time()
        now = time.monotonic()
        pane_text = capture_pane(pane_id, history=8)
        previous = _CPU_SAMPLES.get(pane_id)
        if previous is None or now - previous[1] > 1.5:
            previous = (total, now)
            time.sleep(0.4)
            total = cpu_time()
            now = time.monotonic()
        _CPU_SAMPLES[pane_id] = (total, now)
        elapsed = now - previous[1]
        cpu_percent = (total - previous[0]) / elapsed * 100.0 if elapsed > 0 else None
        state = classify_activity(cpu_percent, pane_text)
        return state, f"cpu={cpu_percent:.1f}" if cpu_percent is not None else "cpu=unknown"
    except Exception as exc:
        return "UNKNOWN", type(exc).__name__


def send_blocked_reason(target: str) -> str | None:
    """Return a user-facing reason when sending would accept an approval prompt."""
    state, _ = observe_activity(target)
    if state != "WAITING_INPUT":
        return None
    name = target
    try:
        from actl.core.tmux import pane_field

        name = pane_field(target, "#{pane_title}") or target
    except Exception:
        pass
    return f"{name}가 승인을 기다리고 있어요. 보내면 승인 창에 들어가요 — ASUS 화면에서 직접 확인하세요"
