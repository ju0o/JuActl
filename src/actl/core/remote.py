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

import hashlib
import json
import subprocess
import uuid
from datetime import datetime, timezone

from actl.core.models import CopyResult


class ManagedUnsupported(RuntimeError):
    """The selected remote runtime has no managed send capability."""


def _runtime_request(target: str, operation: str, body: dict, timeout: float = 30.0) -> dict:
    from actl.core.tmux import _no_window

    payload = {"contractVersion": 1, "requestId": str(uuid.uuid4()),
               "operation": operation, **body}
    try:
        proc = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", target,
             "~/.local/bin/actl", "runtime", operation, "--request-stdin"],
            input=json.dumps(payload, ensure_ascii=False), capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=timeout,
            check=False, **_no_window(),
        )
    except Exception as exc:
        raise RuntimeError(f"managed runtime ssh failed: {exc}") from exc
    try:
        response = json.loads(proc.stdout)
    except (json.JSONDecodeError, TypeError) as exc:
        detail = (proc.stderr or proc.stdout or f"exit {proc.returncode}").strip()
        raise RuntimeError(f"managed runtime invalid response: {detail[-300:]}") from exc
    if proc.returncode or not response.get("ok"):
        error = response.get("error") or {}
        raise RuntimeError(f"{error.get('code', 'REMOTE_RUNTIME')}: {error.get('detail', 'request failed')}")
    return response


def remote_managed_send(agent: str, target_pane: str, prompt: str, timeout: float = 45.0) -> str:
    """Send to one remote pane through the existing managed runtime contract."""
    from actl.core.tmux import REMOTE_SSH_TARGET
    from actl.core.tmux import _no_window

    if not REMOTE_SSH_TARGET:
        raise ManagedUnsupported("managed remote send requires an SSH target")
    socket_probe = subprocess.run(
        ["ssh", "-n", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", REMOTE_SSH_TARGET,
         "tmux display-message -p '#{socket_path}'"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=10, check=False, **_no_window(),
    )
    socket_path = socket_probe.stdout.strip()
    if socket_probe.returncode or not socket_path.startswith("/"):
        raise RuntimeError("managed runtime socket path unavailable")
    discovered = _runtime_request(REMOTE_SSH_TARGET, "discover", {"socketPath": socket_path}, timeout)
    candidates = [candidate for candidate in discovered.get("data", {}).get("candidates", [])
                  if candidate.get("agentKind") == agent
                  and (candidate.get("identityEvidence") or {}).get("paneId") == target_pane]
    if len(candidates) != 1:
        raise RuntimeError("AMBIGUOUS_INSTANCE: selected pane has no unique managed runtime")
    candidate = candidates[0]
    if not candidate.get("issuable") or candidate.get("processState") != "UP":
        raise RuntimeError("STALE: selected managed runtime is not UP")
    if not (candidate.get("capabilities") or {}).get("managed.send"):
        raise ManagedUnsupported(f"UNSUPPORTED: managed.send is unavailable for {agent}")
    evidence = candidate.get("identityEvidence") or {}
    expected = {key: candidate.get(key) for key in
                ("agentKind", "profileRoot", "workspaceRoot", "expectedSession")
                if candidate.get(key) is not None}
    expected["paneId"] = target_pane
    scope = {"socketPath": socket_path}
    grant = _runtime_request(REMOTE_SSH_TARGET, "reserve", {
        **scope, "action": "acquire", "runtimeId": candidate["runtimeId"],
        "mode": "MANAGED", "ownerRef": "actl-gui", "expectedContext": expected,
    }, timeout)["data"]
    command_id = "cmd_" + uuid.uuid4().hex
    try:
        response = _runtime_request(REMOTE_SSH_TARGET, "send", {
            **scope, "runtimeId": grant["runtimeId"],
            "expectedContext": grant["context"],
            "reservationId": grant["reservationId"], "leaseToken": grant["leaseToken"],
            "fence": grant["fence"], "commandId": command_id,
            "wirePrompt": prompt,
            "promptSha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "observationCursor": grant.get("observationCursor"),
            "currentSnapshotHash": grant.get("currentSnapshotHash"),
            "inputPermit": {
                "commandId": command_id, "runtimeId": grant["runtimeId"],
                "fence": grant["fence"], "paneMode": "normal",
                "confirmedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                "snapshotHash": grant.get("currentSnapshotHash"),
            },
        }, timeout)
        return str(response.get("data", {}).get("command", {}).get("target") or target_pane)
    finally:
        try:
            _runtime_request(REMOTE_SSH_TARGET, "reserve", {
                **scope, "action": "release", "reservationId": grant["reservationId"],
                "leaseToken": grant["leaseToken"], "fence": grant["fence"],
                "captureAck": {
                    "kind": "RECONCILE", "commandId": command_id,
                    "disposition": "DELIVERY_AMBIGUOUS", "acknowledged": True,
                },
            }, timeout)
        except Exception:
            # The managed send result remains authoritative; lease recovery is
            # handled by the existing runtime expiry/reconcile path.
            pass


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
