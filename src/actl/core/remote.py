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
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable


class ManagedUnsupported(RuntimeError):
    """The selected remote runtime has no managed send capability."""


MANAGED_DISCOVERY_TIMEOUT = 10.0


@dataclass
class ManagedSendDelivery:
    """Authoritative managed-send receipt; cleanup is separate from commit.

    Equality with a pane-id string is preserved for callers/tests that compare
    the historical ``remote_managed_send(...) == "%N"`` return shape.
    """

    target: str
    command_id: str
    runtime_id: str
    reservation_id: str
    lease_token: str
    fence: Any
    socket_path: str
    ssh_target: str
    evidence: dict[str, Any] = field(default_factory=dict)
    cleanup_attempted: bool = False
    cleanup_ok: bool | None = None
    cleanup_error: str = ""
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, str):
            return self.target == other
        if isinstance(other, ManagedSendDelivery):
            return self.target == other.target and self.command_id == other.command_id
        return NotImplemented

    def __hash__(self) -> int:
        return hash((self.target, self.command_id))

    def __str__(self) -> str:
        return self.target

    def cleanup(self, timeout: float = 45.0) -> bool:
        """Best-effort reserve release/reconcile. Never implies SEND failure."""
        with self._lock:
            if self.cleanup_attempted:
                return bool(self.cleanup_ok)
            self.cleanup_attempted = True
        try:
            _runtime_request(
                self.ssh_target,
                "reserve",
                {
                    "socketPath": self.socket_path,
                    "action": "release",
                    "reservationId": self.reservation_id,
                    "leaseToken": self.lease_token,
                    "fence": self.fence,
                    "captureAck": {
                        "kind": "RECONCILE",
                        "commandId": self.command_id,
                        "disposition": "DELIVERY_AMBIGUOUS",
                        "acknowledged": True,
                    },
                },
                timeout,
            )
            with self._lock:
                self.cleanup_ok = True
                self.cleanup_error = ""
            return True
        except Exception as exc:  # noqa: BLE001 — recorded; SEND remains authoritative
            with self._lock:
                self.cleanup_ok = False
                self.cleanup_error = str(exc)
            return False


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
    except subprocess.TimeoutExpired as exc:
        if operation == "discover":
            raise RuntimeError("DISCOVERY_TIMEOUT: retry discovery") from exc
        raise RuntimeError(f"managed runtime {operation} timed out") from exc
    except Exception as exc:
        raise RuntimeError(f"managed runtime ssh failed: {exc}") from exc
    try:
        response = json.loads(proc.stdout)
    except (json.JSONDecodeError, TypeError) as exc:
        detail = (proc.stderr or proc.stdout or f"exit {proc.returncode}").strip()
        raise RuntimeError(f"managed runtime invalid response: {detail[-300:]}") from exc
    if not isinstance(response, dict) or not isinstance(response.get("ok"), bool):
        raise RuntimeError("managed runtime invalid response: malformed envelope")
    if proc.returncode or not response["ok"]:
        error = response.get("error")
        if not isinstance(error, dict):
            raise RuntimeError("managed runtime invalid response: malformed error")
        code = error.get("code")
        detail = error.get("detail")
        if not isinstance(code, str) or not code or not isinstance(detail, str) or not detail:
            raise RuntimeError("managed runtime invalid response: malformed error")
        raise RuntimeError(f"{code}: {detail}")
    return response


def _runtime_data(response: dict, operation: str) -> dict:
    data = response.get("data")
    if not isinstance(data, dict):
        raise RuntimeError(f"managed runtime invalid response: {operation} data is not an object")
    return data


def _runtime_candidates(data: dict, operation: str) -> list[dict]:
    candidates = data.get("candidates")
    if not isinstance(candidates, list) or any(not isinstance(candidate, dict) for candidate in candidates):
        raise RuntimeError(f"managed runtime invalid response: {operation} candidates is not an array of objects")
    return candidates


def remote_managed_send(
    agent: str,
    target_pane: str,
    prompt: str,
    timeout: float = 45.0,
    *,
    on_committed: Callable[[ManagedSendDelivery], None] | None = None,
    auto_cleanup: bool = True,
) -> ManagedSendDelivery:
    """Send to one remote pane through the existing managed runtime contract.

    Authoritative SUBMITTED commit point is the successful managed ``send``
    response. Lease release/reconcile is separate best-effort cleanup and must
    not delay that commit (and must not convert a successful send into failure).
    """
    from actl.core.tmux import REMOTE_SSH_TARGET
    from actl.core.tmux import _no_window

    if not REMOTE_SSH_TARGET:
        raise ManagedUnsupported("managed remote send requires an SSH target")
    try:
        socket_probe = subprocess.run(
            ["ssh", "-n", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", REMOTE_SSH_TARGET,
             "tmux display-message -p '#{socket_path}'"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=10, check=False, **_no_window(),
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("MANAGED_SOCKET_PROBE_TIMEOUT: retry discovery") from exc
    except Exception as exc:
        raise RuntimeError("MANAGED_SOCKET_PROBE_FAILED: retry discovery") from exc
    socket_path = socket_probe.stdout.strip()
    if socket_probe.returncode or not socket_path.startswith("/"):
        raise RuntimeError("MANAGED_SOCKET_PROBE_FAILED: retry discovery")
    discovered = _runtime_request(
        REMOTE_SSH_TARGET,
        "discover",
        {"socketPath": socket_path},
        min(timeout, MANAGED_DISCOVERY_TIMEOUT),
    )
    candidates = []
    for candidate in _runtime_candidates(_runtime_data(discovered, "discover"), "discover"):
        identity = candidate.get("identityEvidence", {})
        if identity is not None and not isinstance(identity, dict):
            raise RuntimeError("managed runtime invalid response: discover identityEvidence is not an object")
        if candidate.get("agentKind") == agent and (identity or {}).get("paneId") == target_pane:
            candidates.append(candidate)
    if len(candidates) != 1:
        raise RuntimeError("AMBIGUOUS_INSTANCE: selected pane has no unique managed runtime")
    candidate = candidates[0]
    if not candidate.get("issuable") or candidate.get("processState") != "UP":
        raise RuntimeError("STALE: selected managed runtime is not UP")
    capabilities = candidate.get("capabilities", {})
    if capabilities is not None and not isinstance(capabilities, dict):
        raise RuntimeError("managed runtime invalid response: discover capabilities is not an object")
    if not (capabilities or {}).get("managed.send"):
        raise ManagedUnsupported(f"UNSUPPORTED: managed.send is unavailable for {agent}")
    expected = {key: candidate.get(key) for key in
                ("agentKind", "profileRoot", "workspaceRoot", "expectedSession")
                if candidate.get(key) is not None}
    expected["paneId"] = target_pane
    scope = {"socketPath": socket_path}
    grant_envelope = _runtime_request(REMOTE_SSH_TARGET, "reserve", {
        **scope, "action": "acquire", "runtimeId": candidate["runtimeId"],
        "mode": "MANAGED", "ownerRef": "actl-gui", "expectedContext": expected,
    }, timeout)
    grant = _runtime_data(grant_envelope, "reserve")
    # Permit freshness must use the same ASUS/server observation as the snapshot.
    # MainPC wall clock is not an authority for inputPermit.confirmedAt.
    observed_at = grant_envelope.get("observedAt")
    if not isinstance(observed_at, str) or not observed_at.strip():
        raise RuntimeError("managed reserve response missing server observedAt")
    snapshot_hash = grant.get("currentSnapshotHash")
    if not isinstance(snapshot_hash, str) or not snapshot_hash:
        raise RuntimeError("managed reserve response missing currentSnapshotHash")
    command_id = "cmd_" + uuid.uuid4().hex
    delivery: ManagedSendDelivery | None = None
    try:
        response = _runtime_request(REMOTE_SSH_TARGET, "send", {
            **scope, "runtimeId": grant["runtimeId"],
            "expectedContext": grant["context"],
            "reservationId": grant["reservationId"], "leaseToken": grant["leaseToken"],
            "fence": grant["fence"], "commandId": command_id,
            "wirePrompt": prompt,
            "promptSha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "observationCursor": grant.get("observationCursor"),
            "currentSnapshotHash": snapshot_hash,
            "inputPermit": {
                "commandId": command_id, "runtimeId": grant["runtimeId"],
                "fence": grant["fence"], "paneMode": "normal",
                "confirmedAt": observed_at,
                "snapshotHash": snapshot_hash,
            },
        }, timeout)
        send_data = _runtime_data(response, "send")
        command = send_data.get("command", {})
        if not isinstance(command, dict):
            raise RuntimeError("managed runtime invalid response: send command is not an object")
        target = str(command.get("target") or target_pane)
        delivery = ManagedSendDelivery(
            target=target,
            command_id=command_id,
            runtime_id=str(grant["runtimeId"]),
            reservation_id=str(grant["reservationId"]),
            lease_token=str(grant["leaseToken"]),
            fence=grant["fence"],
            socket_path=socket_path,
            ssh_target=REMOTE_SSH_TARGET,
            evidence={
                "path": "managed",
                "managedTarget": target,
                "disposition": "TRANSPORT_SENT",
                "commandId": command_id,
                "commitPoint": "managed_send_response",
            },
        )
        # Authoritative commit: runtime accepted the send. Callers mark SUBMITTED here.
        if on_committed is not None:
            on_committed(delivery)
        return delivery
    finally:
        # Cleanup is best-effort and must not raise into the send path after commit.
        if delivery is None:
            # Send never committed — still try to release a held reservation.
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
                pass
        elif auto_cleanup:
            # Sync auto_cleanup kept for CLI/simple callers; GUI should pass
            # auto_cleanup=False and run delivery.cleanup() off the P0 path.
            delivery.cleanup(timeout=timeout)


def is_remote() -> bool:
    from actl.core.tmux import REMOTE_SSH_TARGET

    return bool(REMOTE_SSH_TARGET)


def remote_extract(agent: str, target: str, timeout: float = 30.0) -> "CopyResult | None":
    """Extract via remote actl. Returns None when not in remote mode."""
    from actl.core.models import CopyResult
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
