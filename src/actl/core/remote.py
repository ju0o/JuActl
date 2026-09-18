"""Remote (--ssh) helpers: run extraction on the remote host.

Problem: session storage (~/.grok, ~/.claude-*, opencode.db, rollouts)
lives on the remote host (asus), while MainPC only sees tmux over ssh.
Reading those files locally on MainPC always fails. Solution: delegate
the whole extraction to the remote actl (which runs locally on asus):

    ssh <target> actl extract <agent> <pane>

No SSHFS, no file sync. Requires actl installed on the remote host
(~/.local/bin/actl, same repo).
"""
from __future__ import annotations

import subprocess

from actl.core.models import CopyResult


def is_remote() -> bool:
    from actl.core.tmux import REMOTE_SSH_TARGET

    return bool(REMOTE_SSH_TARGET)


def remote_extract(agent: str, target: str, timeout: float = 30.0) -> CopyResult | None:
    """Extract via remote actl. Returns None when not in remote mode."""
    from actl.core.tmux import REMOTE_SSH_TARGET

    if not REMOTE_SSH_TARGET:
        return None
    try:
        proc = subprocess.run(
            ["ssh", REMOTE_SSH_TARGET, "~/.local/bin/actl", "extract", agent, target],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except Exception as exc:
        return CopyResult(None, f"{agent}-remote-unresolved", "none", f"ssh failed: {exc}")
    text = proc.stdout
    if text:
        return CopyResult(text, f"{agent}-remote:{target}", "exact")
    detail = (proc.stderr or "").strip().splitlines()
    tail = detail[-1] if detail else f"remote exit {proc.returncode}"
    return CopyResult(None, f"{agent}-remote-unresolved", "none", tail[:300])
