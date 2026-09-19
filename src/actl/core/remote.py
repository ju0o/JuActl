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
        from actl.core.tmux import _no_window

        proc = subprocess.run(
            ["ssh", "-n", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", REMOTE_SSH_TARGET,
             "~/.local/bin/actl", "extract", agent, target],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            **_no_window(),
        )
    except Exception as exc:
        return CopyResult(None, f"{agent}-remote-unresolved", "none", f"ssh failed: {exc}")
    if proc.returncode == 0 and proc.stdout:
        text = proc.stdout
        return CopyResult(text, f"{agent}-remote:{target}", "exact")
    detail = (proc.stderr or "").strip().splitlines()
    tail = detail[-1] if detail else f"remote exit {proc.returncode}"
    return CopyResult(None, f"{agent}-remote-unresolved", "none", tail[:300])


def remote_send(agent: str, prompt: str, timeout: float = 30.0) -> str:
    """Send a prompt via remote actl (stdin pipe, no temp files on MainPC)."""
    from actl.core.tmux import REMOTE_SSH_TARGET, _no_window

    if not REMOTE_SSH_TARGET:
        raise RuntimeError("not in remote mode")
    try:
        proc = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", REMOTE_SSH_TARGET,
             "~/.local/bin/actl", "send", agent],
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            **_no_window(),
        )
    except Exception as exc:
        raise RuntimeError(f"ssh failed: {exc}") from exc
    if proc.returncode:
        err = (proc.stderr or proc.stdout or f"exit {proc.returncode}").strip().splitlines()
        raise RuntimeError(err[-1][:300] if err else f"exit {proc.returncode}")
    return (proc.stdout or "").strip().splitlines()[-1] if proc.stdout.strip() else agent
