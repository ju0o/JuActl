"""Conservative, read-only pane liveness classification."""
from __future__ import annotations


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
        import subprocess

        from actl.core.tmux import _no_window, _remote_args, capture_pane, pane_field

        pane_pid = int(pane_field(pane_id, "#{pane_pid}"))
        proc = subprocess.run(
            _remote_args(["ps", "-o", "%cpu=", "-g", str(pane_pid)]),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=5, **_no_window(),
        )
        total = sum(float(value) for value in proc.stdout.split())
        pane_text = capture_pane(pane_id, history=8)
        state = classify_activity(total, pane_text)
        return state, f"cpu={total:.1f}"
    except Exception as exc:
        return "UNKNOWN", type(exc).__name__
