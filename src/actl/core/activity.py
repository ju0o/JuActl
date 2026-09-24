"""Conservative, read-only pane liveness classification."""
from __future__ import annotations

import subprocess
import time


_CPU_SAMPLES: dict[str, tuple[float, float]] = {}


def classify_activity(cpu_percent: float | None, pane_text: str | None) -> str:
    """Return RUNNING, IDLE, or UNKNOWN; never infer idle from missing evidence."""
    if cpu_percent is None or pane_text is None:
        return "UNKNOWN"
    if cpu_percent > 5.0:
        return "RUNNING"
    lines = [line.strip().lower() for line in pane_text.splitlines() if line.strip()]
    if not lines:
        return "UNKNOWN"
    tail = lines[-1]
    if any(token in tail for token in (
        "working", "thinking", "generating", "running", "esc to interrupt",
        "ctrl-c to interrupt", "ctrl+c to interrupt", "tool call",
    )):
        return "RUNNING"
    if tail.endswith(("❯", "›", ">", "$")):
        return "IDLE"
    return "UNKNOWN"


def observe_activity(pane_id: str) -> tuple[str, str]:
    """Read one pane's activity without sending input or changing config."""
    try:
        from actl.core.tmux import _no_window, _remote_args, capture_pane, pane_field

        pane_pid = int(pane_field(pane_id, "#{pane_pid}"))
        proc = subprocess.run(
            _remote_args(["ps", "-o", "times=", "-g", str(pane_pid)]),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=5, **_no_window(),
        )
        total = sum(
            sum(int(part) * multiplier for part, multiplier in zip(
                reversed(value.split(":")), (1, 60, 3600, 86400), strict=False
            ))
            for value in proc.stdout.split()
        )
        now = time.monotonic()
        pane_text = capture_pane(pane_id, history=8)
        previous = _CPU_SAMPLES.get(pane_id)
        _CPU_SAMPLES[pane_id] = (total, now)
        if previous is None:
            state = classify_activity(0.0, pane_text)
            return state, "cpu=0.0"
        elapsed = now - previous[1]
        cpu_percent = (total - previous[0]) / elapsed * 100.0 if elapsed > 0 else None
        state = classify_activity(cpu_percent, pane_text)
        return state, f"cpu={cpu_percent:.1f}" if cpu_percent is not None else "cpu=unknown"
    except Exception as exc:
        return "UNKNOWN", type(exc).__name__
