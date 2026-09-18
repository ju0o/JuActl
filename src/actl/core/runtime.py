"""Managed runtime JSON contract v1 — journal, reserve, identity, discover, status."""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CONTRACT_VERSION = 1
DEFAULT_LEASE_NS = 30_000_000_000  # 30s
USER_VERSION = 1
SIDE_EFFECT_NONE = "NONE"
SIDE_EFFECT_POSSIBLE = "POSSIBLE_INPUT"
SIDE_EFFECT_OBSERVED = "INPUT_OBSERVED"

IDENTITY_FIELDS = (
    "hostKey",
    "uid",
    "bootId",
    "socketPath",
    "socketDev",
    "socketInode",
    "serverPid",
    "serverStartTicks",
    "paneId",
    "panePid",
    "paneStartTicks",
    "agentKind",
    "agentPid",
    "agentStartTicks",
    "agentExePath",
    "agentExeDev",
    "agentExeInode",
)
_DECIMAL_IDENTITY_FIELDS = frozenset({
    "uid",
    "socketDev",
    "socketInode",
    "serverPid",
    "serverStartTicks",
    "panePid",
    "paneStartTicks",
    "agentPid",
    "agentStartTicks",
    "agentExeDev",
    "agentExeInode",
})

# Tests may set this Path; never exposed as a CLI flag.
_journal_root_override: Path | None = None

_COMMON_FIELDS = frozenset({"contractVersion", "requestId", "operation"})
_SCOPE_FIELDS = frozenset({"serverScope", "socketPath", "hostKey", "uid"})
_RESERVE_FIELDS = frozenset({
    "action",
    "runtimeId",
    "expectedContext",
    "mode",
    "ownerRef",
    "correlationDigest",
    "reservationId",
    "leaseToken",
    "fence",
    "captureAck",
}) | _SCOPE_FIELDS
_ALLOWED_BY_OPERATION = {
    "reserve": _COMMON_FIELDS | _RESERVE_FIELDS,
    "discover": _COMMON_FIELDS | _SCOPE_FIELDS | frozenset({"agentKind"}),
    "status": _COMMON_FIELDS | _SCOPE_FIELDS | frozenset({"runtimeId", "expectedContext", "selector"}),
    "send": _COMMON_FIELDS | frozenset({
        "runtimeId",
        "expectedContext",
        "reservationId",
        "leaseToken",
        "fence",
        "commandId",
        "correlationDigest",
        "wirePrompt",
        "promptSha256",
        "observationCursor",
        "inputPermit",
        "currentSnapshotHash",
        "serverScope",
        "socketPath",
        "hostKey",
        "uid",
    }),
    "collect": _COMMON_FIELDS | frozenset({
        "commandId",
        "runtimeId",
        "expectedContext",
        "resultId",
        "serverScope",
        "socketPath",
        "hostKey",
        "uid",
    }),
    "interrupt": _COMMON_FIELDS | frozenset({
        "commandId",
        "runtimeId",
        "expectedContext",
        "reservationId",
        "leaseToken",
        "fence",
        "interruptRequestId",
        "reason",
        "serverScope",
        "socketPath",
        "hostKey",
        "uid",
    }),
}
_RPC_EXIT = {"INVALID_ARGUMENT", "CONFLICT", "FORBIDDEN"}
_UNSUPPORTED_OPS: frozenset[str] = frozenset()
_JOURNAL_OPS = frozenset({"reserve", "discover", "status", "send", "interrupt", "collect"})
MAX_PROMPT_BYTES = 64 * 1024
INPUT_PERMIT_MAX_AGE_S = 10.0
_POST_PREPARED_STAGES = frozenset({
    "ATTEMPTING",
    "TRANSPORT_SENT",
    "DELIVERY_AMBIGUOUS",
    "AGENT_RECEIVED",
    "FINAL",
    "CANCEL_REQUESTED",
})


class JournalError(Exception):
    """Durable journal cannot be opened or used safely."""


def canonical_json(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def boot_time_ns() -> int:
    return int(time.clock_gettime(time.CLOCK_BOOTTIME) * 1_000_000_000)


def wall_time_s() -> float:
    """Wall clock seconds; tests may monkeypatch for inputPermit freshness."""
    return time.time()


def observed_at_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# Patchable transport entry points (avoid importing cycles at call sites in tests).
def _default_transport_send_prompt(*args: Any, **kwargs: Any) -> dict[str, Any]:
    from actl.core.tmux import send_prompt_staged

    return send_prompt_staged(*args, **kwargs)


def _default_transport_interrupt(*args: Any, **kwargs: Any) -> dict[str, Any]:
    from actl.core.tmux import interrupt_ctrl_c

    return interrupt_ctrl_c(*args, **kwargs)


transport_send_prompt = _default_transport_send_prompt
transport_interrupt = _default_transport_interrupt


def observe_agent_received(command_id: str, **kwargs: Any) -> bool:
    """True when Managed bind finds exactly one matching post-cursor UserMessage."""
    wire_prompt = kwargs.get("wire_prompt")
    cursor = kwargs.get("cursor")
    path = kwargs.get("path")
    session_id = kwargs.get("session_id")
    if not isinstance(wire_prompt, str) or not isinstance(cursor, dict) or path is None:
        return False
    try:
        from actl.agents import codex as codex_agent

        forward = codex_agent.read_jsonl_forward(Path(path), cursor)
        if forward.code != "OK" or not forward.events:
            return False
        binding = codex_agent.bind_user_turn(
            cursor,
            wire_prompt,
            forward.events,
            session_id=session_id if isinstance(session_id, str) else None,
        )
        return binding.code == "OK"
    except Exception:
        return False


def local_host_key() -> str:
    """SHA-256 of local machine-id; tests may monkeypatch."""
    for path in (Path("/etc/machine-id"), Path("/var/lib/dbus/machine-id")):
        if path.is_file():
            raw = path.read_text(encoding="utf-8").strip()
            if raw:
                return sha256_hex(raw.encode("utf-8"))
    return sha256_hex(b"unknown-machine-id")


def local_uid() -> str:
    return str(os.getuid())


def scope_id_for_socket(host_key: str, uid: int | str, socket_path: str) -> str:
    return sha256_hex(
        canonical_json({
            "hostKey": str(host_key),
            "socketPath": str(socket_path),
            "uid": str(uid),
        })
    )


def journal_path_for_scope(scope_id: str) -> Path:
    root = _journal_root_override if _journal_root_override is not None else (Path.home() / ".local/state/actl")
    return Path(root) / "servers" / scope_id / "runtime-v1.sqlite3"


class IdentityIncomplete(Exception):
    def __init__(self, missing: list[str]):
        self.missing = missing
        super().__init__(f"incomplete identity; missing: {', '.join(missing)}")


def _decimal_string(field: str, value: Any) -> str:
    if isinstance(value, bool) or value is None:
        raise IdentityIncomplete([field])
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str) and value != "":
        try:
            return str(int(value, 10))
        except ValueError as exc:
            raise IdentityIncomplete([field]) from exc
    raise IdentityIncomplete([field])


def identity_missing_fields(evidence: dict[str, Any] | None) -> list[str]:
    if not isinstance(evidence, dict):
        return list(IDENTITY_FIELDS)
    missing: list[str] = []
    for field in IDENTITY_FIELDS:
        if field not in evidence or evidence[field] is None or evidence[field] == "":
            missing.append(field)
            continue
        if field in _DECIMAL_IDENTITY_FIELDS:
            try:
                _decimal_string(field, evidence[field])
            except IdentityIncomplete:
                missing.append(field)
    return missing


def build_identity(**fields: Any) -> dict[str, str]:
    missing = [name for name in IDENTITY_FIELDS if name not in fields or fields[name] is None or fields[name] == ""]
    if missing:
        raise IdentityIncomplete(missing)
    identity: dict[str, str] = {}
    for name in IDENTITY_FIELDS:
        value = fields[name]
        if name in _DECIMAL_IDENTITY_FIELDS or name == "uid":
            identity[name] = _decimal_string(name, value)
        else:
            if not isinstance(value, str) or not value:
                raise IdentityIncomplete([name])
            identity[name] = value
    return identity


def runtime_id_from_identity(identity: dict[str, Any]) -> str:
    complete = build_identity(**identity)
    return "rt1_" + sha256_hex(canonical_json(complete))


def managed_capabilities(agent_kind: str | None) -> dict[str, bool]:
    collect = agent_kind in {"codex", "claude-pro", "claude-team", "grok"}
    send = agent_kind == "codex"
    return {
        "managed.send": send,
        "managed.collect.final": collect,
    }


def observe_candidates(socket_path: str, agent_kind: str | None = None) -> list[dict[str, Any]]:
    """Live observation for an explicit socket (PHASE 1A-LIVE).

    Tests may monkeypatch this. Each candidate dict may include:
      identityEvidence (dict), processState, profileRoot, workspaceRoot,
      mappingState, expectedSession, observedAt
    """
    from actl.core.live_observe import observe_socket_candidates

    try:
        return observe_socket_candidates(
            socket_path,
            agent_kind,
            host_key=local_host_key(),
            uid=local_uid(),
        )
    except Exception:
        # Fail closed to empty rather than inventing identity.
        return []


def _live_snapshot_for_candidate(candidate: dict[str, Any], socket_path: str) -> str | None:
    from actl.core.live_observe import snapshot_hash_for_pane

    evidence = candidate.get("identityEvidence") or {}
    pane_id = evidence.get("paneId") if isinstance(evidence, dict) else None
    if not isinstance(pane_id, str) or not pane_id:
        return None
    return snapshot_hash_for_pane(pane_id, socket_path)


def _freeze_context_from_live(
    expected: dict[str, Any],
    candidate: dict[str, Any],
) -> dict[str, Any]:
    """Merge caller expectedContext with live paneId; reject mismatches."""
    evidence = candidate.get("identityEvidence") or {}
    if not isinstance(evidence, dict):
        raise ReserveInvalid("live identity evidence missing")
    pane_id = evidence.get("paneId")
    if not isinstance(pane_id, str) or not pane_id:
        raise ReserveInvalid("live paneId missing")
    observed = {
        "agentKind": candidate.get("agentKind"),
        "profileRoot": candidate.get("profileRoot"),
        "workspaceRoot": candidate.get("workspaceRoot"),
        "expectedSession": candidate.get("expectedSession"),
    }
    mismatched = _context_mismatch(expected, observed)
    if mismatched:
        raise ReserveInvalid(f"expectedContext.{mismatched} does not match observed context")
    if "paneId" in expected and expected["paneId"] not in (None, pane_id):
        raise ReserveInvalid("expectedContext.paneId does not match live paneId")
    frozen = dict(expected)
    frozen["paneId"] = pane_id
    for key in ("agentKind", "profileRoot", "workspaceRoot", "expectedSession"):
        if frozen.get(key) is None and observed.get(key) is not None:
            frozen[key] = observed[key]
    return frozen


def _revalidate_live_send_target(
    *,
    socket_path: str,
    runtime_id: str,
    expected: dict[str, Any],
    reservation_context: dict[str, Any] | None,
    current_snapshot: str | None,
    permit_snapshot: str | None,
) -> str:
    """Re-check live identity/pane/snapshot before any input. Returns live snapshot hash."""
    candidate = _find_observation(socket_path, runtime_id=runtime_id, selector=None)
    if candidate is None:
        raise SendRejected("DOWN", f"runtime {runtime_id} not observed on socket", retry_action="rediscover")
    if str(candidate.get("processState") or "DOWN") == "DOWN":
        raise SendRejected("DOWN", "observed processState is DOWN", retry_action="rediscover")
    if candidate.get("runtimeId") != runtime_id:
        raise SendRejected("MISMATCH", "live runtimeId does not match request", retry_action="rediscover")
    evidence = candidate.get("identityEvidence") or {}
    live_pane = evidence.get("paneId") if isinstance(evidence, dict) else None
    req_pane = expected.get("paneId")
    if not isinstance(req_pane, str) or not req_pane:
        raise SendRejected("INVALID_ARGUMENT", "expectedContext.paneId is required for send")
    if live_pane != req_pane:
        raise SendRejected("MISMATCH", "live paneId does not match expectedContext.paneId", retry_action="rediscover")
    if isinstance(reservation_context, dict):
        frozen_pane = reservation_context.get("paneId")
        if isinstance(frozen_pane, str) and frozen_pane and frozen_pane != req_pane:
            raise SendRejected("MISMATCH", "expectedContext.paneId does not match reservation frozen paneId")
        for key in ("agentKind", "profileRoot", "workspaceRoot", "expectedSession"):
            if key in reservation_context and reservation_context[key] is not None:
                if expected.get(key) is not None and expected.get(key) != reservation_context[key]:
                    raise SendRejected("MISMATCH", f"expectedContext.{key} drifted from reservation context")
    compare_against = {
        "agentKind": candidate.get("agentKind"),
        "profileRoot": candidate.get("profileRoot"),
        "workspaceRoot": candidate.get("workspaceRoot"),
        "expectedSession": candidate.get("expectedSession"),
    }
    mismatched = _context_mismatch(expected, compare_against)
    if mismatched:
        raise SendRejected(
            "MISMATCH",
            f"expectedContext.{mismatched} does not match observed context",
            retry_action="rediscover",
        )
    live_snap = _live_snapshot_for_candidate(candidate, socket_path)
    if not live_snap:
        raise SendRejected("INPUT_STATE_UNKNOWN", "unable to capture live pane snapshot", retry_action="refresh_input_permit")
    if current_snapshot is not None and current_snapshot != live_snap:
        raise SendRejected(
            "INPUT_STATE_UNKNOWN",
            "pane snapshot changed before send",
            retry_action="refresh_input_permit",
        )
    if permit_snapshot is not None and permit_snapshot != live_snap:
        raise SendRejected(
            "INPUT_STATE_UNKNOWN",
            "inputPermit.snapshotHash does not match live pane snapshot",
            retry_action="refresh_input_permit",
        )
    return live_snap


def build_response(
    request_id: str | None,
    ok: bool,
    data: dict[str, Any] | None = None,
    error: dict[str, Any] | None = None,
    observed_at: str | None = None,
) -> dict[str, Any]:
    return {
        "contractVersion": CONTRACT_VERSION,
        "requestId": request_id,
        "ok": ok,
        "observedAt": observed_at or observed_at_now(),
        "data": data,
        "error": error,
    }


def build_error(
    code: str,
    detail: str,
    side_effect: str = SIDE_EFFECT_NONE,
    retry_action: str = "",
) -> dict[str, Any]:
    return {
        "code": code,
        "detail": detail,
        "sideEffect": side_effect,
        "retryAction": retry_action,
    }


def exit_code_for_error(code: str) -> int:
    return 3 if code in _RPC_EXIT else 2


def _token_hash(token: str) -> str:
    return sha256_hex(token.encode("utf-8"))


_SCHEMA_SQL = """
CREATE TABLE meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE reservations (
  reservation_id TEXT PRIMARY KEY,
  runtime_id TEXT NOT NULL,
  mode TEXT NOT NULL,
  state TEXT NOT NULL,
  owner_ref TEXT,
  correlation_digest TEXT,
  lease_token_hash TEXT NOT NULL,
  fence TEXT NOT NULL,
  context_json TEXT NOT NULL,
  observation_cursor_json TEXT,
  expires_boot_ns INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  release_ack_json TEXT
);
CREATE TABLE commands (
  command_id TEXT PRIMARY KEY,
  runtime_id TEXT NOT NULL,
  reservation_id TEXT,
  request_id TEXT,
  request_body_sha256 TEXT NOT NULL,
  stage TEXT NOT NULL,
  delivery_disposition TEXT,
  wire_prompt_sha256 TEXT,
  wire_prompt TEXT,
  cursor_json TEXT,
  session_id TEXT,
  turn_id TEXT,
  interrupt_intent_at TEXT,
  result_id TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE receipts (
  receipt_id TEXT PRIMARY KEY,
  command_id TEXT,
  request_id TEXT,
  kind TEXT NOT NULL,
  stage TEXT,
  side_effect TEXT,
  payload_json TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE request_idempotency (
  request_id TEXT PRIMARY KEY,
  body_sha256 TEXT NOT NULL,
  response_json TEXT NOT NULL,
  exit_code INTEGER NOT NULL
);
CREATE INDEX idx_reservations_runtime_state ON reservations(runtime_id, state);
CREATE INDEX idx_commands_reservation ON commands(reservation_id);
"""

_REQUIRED_TABLES = frozenset({
    "meta",
    "reservations",
    "commands",
    "receipts",
    "request_idempotency",
})


class Journal:
    def __init__(self, path: Path, conn: sqlite3.Connection):
        self.path = path
        self.conn = conn

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Journal:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def get_idempotency(self, request_id: str) -> tuple[str, str, int] | None:
        row = self.conn.execute(
            "SELECT body_sha256, response_json, exit_code FROM request_idempotency WHERE request_id = ?",
            (request_id,),
        ).fetchone()
        if row is None:
            return None
        return str(row[0]), str(row[1]), int(row[2])

    def put_idempotency(self, request_id: str, body_sha256: str, response: dict[str, Any], exit_code: int) -> None:
        self.conn.execute(
            "INSERT INTO request_idempotency(request_id, body_sha256, response_json, exit_code) VALUES (?, ?, ?, ?)",
            (request_id, body_sha256, json.dumps(response, ensure_ascii=False, separators=(",", ":")), exit_code),
        )

    def _active_reservation(self, runtime_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM reservations WHERE runtime_id = ? AND state IN ('HELD', 'EXPIRED_HELD') "
            "ORDER BY CASE state WHEN 'HELD' THEN 0 ELSE 1 END, updated_at DESC LIMIT 1",
            (runtime_id,),
        ).fetchone()

    def _mark_expired_if_needed(self, row: sqlite3.Row, now_ns: int) -> sqlite3.Row:
        if row["state"] == "HELD" and int(row["expires_boot_ns"]) <= now_ns:
            updated = observed_at_now()
            self.conn.execute(
                "UPDATE reservations SET state = 'EXPIRED_HELD', updated_at = ? WHERE reservation_id = ?",
                (updated, row["reservation_id"]),
            )
            return self.conn.execute(
                "SELECT * FROM reservations WHERE reservation_id = ?",
                (row["reservation_id"],),
            ).fetchone()
        return row

    def _next_fence(self, runtime_id: str) -> str:
        row = self.conn.execute(
            "SELECT fence FROM reservations WHERE runtime_id = ? ORDER BY CAST(fence AS INTEGER) DESC LIMIT 1",
            (runtime_id,),
        ).fetchone()
        if row is None:
            return "1"
        return str(int(str(row["fence"])) + 1)

    def _command_count(self, reservation_id: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) FROM commands WHERE reservation_id = ?",
            (reservation_id,),
        ).fetchone()
        return int(row[0]) if row else 0

    def reservation_summary(self, runtime_id: str) -> dict[str, Any] | None:
        now_ns = boot_time_ns()
        row = self._active_reservation(runtime_id)
        if row is None:
            return None
        row = self._mark_expired_if_needed(row, now_ns)
        return {
            "reservationId": row["reservation_id"],
            "runtimeId": row["runtime_id"],
            "mode": row["mode"],
            "state": row["state"],
            "fence": str(row["fence"]),
            "expiresBootNs": str(row["expires_boot_ns"]),
            "context": json.loads(row["context_json"]),
        }

    def latest_command_summary(self, runtime_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT command_id, stage, delivery_disposition, reservation_id, updated_at "
            "FROM commands WHERE runtime_id = ? ORDER BY updated_at DESC LIMIT 1",
            (runtime_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "commandId": row["command_id"],
            "stage": row["stage"],
            "deliveryDisposition": row["delivery_disposition"],
            "reservationId": row["reservation_id"],
            "updatedAt": row["updated_at"],
        }

    def get_command(self, command_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM commands WHERE command_id = ?",
            (command_id,),
        ).fetchone()

    def command_public_view(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "commandId": row["command_id"],
            "runtimeId": row["runtime_id"],
            "reservationId": row["reservation_id"],
            "requestId": row["request_id"],
            "stage": row["stage"],
            "deliveryDisposition": row["delivery_disposition"],
            "wirePromptSha256": row["wire_prompt_sha256"],
            "interruptIntentAt": row["interrupt_intent_at"],
            "resultId": row["result_id"],
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
        }

    def insert_command_prepared(
        self,
        *,
        command_id: str,
        runtime_id: str,
        reservation_id: str,
        request_id: str,
        request_body_sha256: str,
        wire_prompt: str,
        wire_prompt_sha256: str,
        cursor_json: str | None,
    ) -> sqlite3.Row:
        stamp = observed_at_now()
        self.conn.execute(
            "INSERT INTO commands("
            "command_id, runtime_id, reservation_id, request_id, request_body_sha256, stage, "
            "delivery_disposition, wire_prompt_sha256, wire_prompt, cursor_json, "
            "session_id, turn_id, interrupt_intent_at, result_id, created_at, updated_at"
            ") VALUES (?, ?, ?, ?, ?, 'PREPARED', NULL, ?, ?, ?, NULL, NULL, NULL, NULL, ?, ?)",
            (
                command_id,
                runtime_id,
                reservation_id,
                request_id,
                request_body_sha256,
                wire_prompt_sha256,
                wire_prompt,
                cursor_json,
                stamp,
                stamp,
            ),
        )
        row = self.get_command(command_id)
        assert row is not None
        return row

    def update_command(
        self,
        command_id: str,
        *,
        stage: str | None = None,
        delivery_disposition: str | None = None,
        interrupt_intent_at: str | None = None,
        result_id: str | None = None,
        request_id: str | None = None,
    ) -> sqlite3.Row:
        row = self.get_command(command_id)
        if row is None:
            raise ReserveInvalid(f"command not found: {command_id}")
        stamp = observed_at_now()
        self.conn.execute(
            "UPDATE commands SET "
            "stage = COALESCE(?, stage), "
            "delivery_disposition = COALESCE(?, delivery_disposition), "
            "interrupt_intent_at = COALESCE(?, interrupt_intent_at), "
            "result_id = COALESCE(?, result_id), "
            "request_id = COALESCE(?, request_id), "
            "updated_at = ? "
            "WHERE command_id = ?",
            (stage, delivery_disposition, interrupt_intent_at, result_id, request_id, stamp, command_id),
        )
        updated = self.get_command(command_id)
        assert updated is not None
        return updated

    def add_receipt(
        self,
        *,
        command_id: str | None,
        request_id: str | None,
        kind: str,
        stage: str | None,
        side_effect: str,
        payload: dict[str, Any] | None = None,
    ) -> str:
        receipt_id = "rcpt_" + uuid.uuid4().hex
        self.conn.execute(
            "INSERT INTO receipts(receipt_id, command_id, request_id, kind, stage, side_effect, payload_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                receipt_id,
                command_id,
                request_id,
                kind,
                stage,
                side_effect,
                json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True) if payload is not None else None,
                observed_at_now(),
            ),
        )
        return receipt_id

    def has_final(self, command_id: str) -> bool:
        row = self.get_command(command_id)
        if row is None:
            return False
        if row["stage"] == "FINAL" or row["result_id"]:
            return True
        found = self.conn.execute(
            "SELECT 1 FROM receipts WHERE command_id = ? AND kind = 'FINAL' LIMIT 1",
            (command_id,),
        ).fetchone()
        return found is not None

    def get_final_packet(self, command_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT payload_json FROM receipts WHERE command_id = ? AND kind = 'FINAL' "
            "ORDER BY created_at ASC LIMIT 1",
            (command_id,),
        ).fetchone()
        if row is None or not row["payload_json"]:
            return None
        try:
            payload = json.loads(row["payload_json"])
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None

    def acquire(
        self,
        *,
        runtime_id: str,
        mode: str,
        expected_context: dict[str, Any],
        owner_ref: str | None,
        correlation_digest: str | None,
    ) -> dict[str, Any]:
        now_ns = boot_time_ns()
        active = self._active_reservation(runtime_id)
        if active is not None:
            active = self._mark_expired_if_needed(active, now_ns)
            raise ReserveBusy(f"runtime {runtime_id} is {active['state']}")

        reservation_id = "rsv_" + uuid.uuid4().hex
        lease_token = secrets.token_hex(32)
        fence = self._next_fence(runtime_id)
        expires = now_ns + DEFAULT_LEASE_NS
        stamp = observed_at_now()
        context_json = json.dumps(expected_context, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        self.conn.execute(
            "INSERT INTO reservations("
            "reservation_id, runtime_id, mode, state, owner_ref, correlation_digest, "
            "lease_token_hash, fence, context_json, observation_cursor_json, "
            "expires_boot_ns, created_at, updated_at, release_ack_json"
            ") VALUES (?, ?, ?, 'HELD', ?, ?, ?, ?, ?, NULL, ?, ?, ?, NULL)",
            (
                reservation_id,
                runtime_id,
                mode,
                owner_ref,
                correlation_digest,
                _token_hash(lease_token),
                fence,
                context_json,
                expires,
                stamp,
                stamp,
            ),
        )
        return {
            "reservationId": reservation_id,
            "leaseToken": lease_token,
            "fence": fence,
            "expiresBootNs": str(expires),
            "runtimeId": runtime_id,
            "mode": mode,
            "context": expected_context,
            "inputState": "INPUT_STATE_UNKNOWN",
            "observationCursor": None,
        }

    def renew(
        self,
        *,
        reservation_id: str,
        lease_token: str,
        fence: str,
    ) -> dict[str, Any]:
        now_ns = boot_time_ns()
        row = self.conn.execute(
            "SELECT * FROM reservations WHERE reservation_id = ?",
            (reservation_id,),
        ).fetchone()
        if row is None:
            raise ReserveInvalid("reservation not found")
        if row["state"] == "RELEASED":
            raise ReserveBusy("reservation already released")
        row = self._mark_expired_if_needed(row, now_ns)
        if row["state"] == "EXPIRED_HELD":
            raise ReserveBusy("reservation expired-held")
        if str(row["fence"]) != str(fence):
            raise ReserveBusy("stale fence")
        if row["lease_token_hash"] != _token_hash(lease_token):
            raise ReserveForbidden("lease token mismatch")
        expires = now_ns + DEFAULT_LEASE_NS
        stamp = observed_at_now()
        self.conn.execute(
            "UPDATE reservations SET expires_boot_ns = ?, updated_at = ? WHERE reservation_id = ?",
            (expires, stamp, reservation_id),
        )
        context = json.loads(row["context_json"])
        return {
            "reservationId": reservation_id,
            "leaseToken": lease_token,
            "fence": str(row["fence"]),
            "expiresBootNs": str(expires),
            "runtimeId": row["runtime_id"],
            "mode": row["mode"],
            "context": context,
            "inputState": "INPUT_STATE_UNKNOWN",
            "observationCursor": json.loads(row["observation_cursor_json"]) if row["observation_cursor_json"] else None,
        }

    def release(
        self,
        *,
        reservation_id: str,
        lease_token: str,
        fence: str,
        capture_ack: Any | None,
    ) -> dict[str, Any]:
        now_ns = boot_time_ns()
        row = self.conn.execute(
            "SELECT * FROM reservations WHERE reservation_id = ?",
            (reservation_id,),
        ).fetchone()
        if row is None:
            raise ReserveInvalid("reservation not found")
        if row["state"] == "RELEASED":
            raise ReserveInvalid("reservation already released")
        # Expiry still allows holder release with matching credentials.
        if row["state"] == "HELD" and int(row["expires_boot_ns"]) <= now_ns:
            row = self._mark_expired_if_needed(row, now_ns)
        if str(row["fence"]) != str(fence):
            raise ReserveBusy("stale fence")
        if row["lease_token_hash"] != _token_hash(lease_token):
            raise ReserveForbidden("lease token mismatch")
        cmd_count = self._command_count(reservation_id)
        if cmd_count > 0:
            if capture_ack is None:
                raise ReserveInvalid("captureAck required when commands are attached")
            validate_capture_ack(self, reservation_id, capture_ack)
        stamp = observed_at_now()
        ack_json = (
            json.dumps(capture_ack, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
            if capture_ack is not None
            else json.dumps({"sideEffectFreeCancel": True}, separators=(",", ":"))
        )
        self.conn.execute(
            "UPDATE reservations SET state = 'RELEASED', updated_at = ?, release_ack_json = ? WHERE reservation_id = ?",
            (stamp, ack_json, reservation_id),
        )
        return {
            "reservationId": reservation_id,
            "runtimeId": row["runtime_id"],
            "state": "RELEASED",
            "releasedAt": stamp,
        }


class ReserveBusy(Exception):
    pass


class ReserveInvalid(Exception):
    pass


class ReserveForbidden(Exception):
    pass


_RECONCILE_DISPOSITIONS = frozenset({"FAILED", "CANCEL_REQUESTED", "DELIVERY_AMBIGUOUS"})
_IN_FLIGHT_COMMAND_STAGES = frozenset({
    "PREPARED",
    "ATTEMPTING",
    "TRANSPORT_SENT",
    "AGENT_RECEIVED",
})


def _reservation_commands(journal: Journal, reservation_id: str) -> list[sqlite3.Row]:
    return list(
        journal.conn.execute(
            "SELECT * FROM commands WHERE reservation_id = ? ORDER BY created_at ASC",
            (reservation_id,),
        ).fetchall()
    )


def _known_result_ids_for_command(journal: Journal, command: sqlite3.Row) -> set[str]:
    found: set[str] = set()
    if command["result_id"]:
        found.add(str(command["result_id"]))
    packet = journal.get_final_packet(str(command["command_id"]))
    if isinstance(packet, dict):
        rid = packet.get("resultId")
        if isinstance(rid, str) and rid:
            found.add(rid)
    return found


def _command_is_finalized(journal: Journal, command: sqlite3.Row) -> bool:
    if str(command["stage"]) == "FINAL" or command["result_id"]:
        return True
    return journal.has_final(str(command["command_id"]))


def _reconcile_disposition_allowed(command: sqlite3.Row, disposition: str) -> bool:
    stage = str(command["stage"] or "")
    delivery = str(command["delivery_disposition"] or "") if command["delivery_disposition"] else ""
    if disposition == stage or (delivery and disposition == delivery):
        return True
    # Holder may acknowledge terminal failure/ambiguity for in-flight commands (no force takeover).
    if disposition in _RECONCILE_DISPOSITIONS and stage in _IN_FLIGHT_COMMAND_STAGES:
        return True
    return False


def validate_capture_ack(journal: Journal, reservation_id: str, capture_ack: Any) -> None:
    """Accept only authorized capture/reconcile acknowledgements when commands are attached."""
    if not isinstance(capture_ack, dict):
        raise ReserveInvalid("captureAck must be an object")
    if capture_ack.get("force") is True:
        raise ReserveForbidden("force takeover is not supported in v1")
    if capture_ack.get("acknowledged") is False:
        raise ReserveInvalid("captureAck.acknowledged must be true")

    kind = capture_ack.get("kind")
    if kind == "FINAL_CAPTURE":
        result_id = capture_ack.get("resultId")
        if not isinstance(result_id, str) or not result_id:
            raise ReserveInvalid("FINAL_CAPTURE requires non-empty resultId")
        if capture_ack.get("acknowledged") is not True:
            raise ReserveInvalid("FINAL_CAPTURE requires acknowledged=true")
        commands = _reservation_commands(journal, reservation_id)
        if not commands:
            raise ReserveInvalid("FINAL_CAPTURE requires commands on the reservation")
        finalized = [c for c in commands if _command_is_finalized(journal, c)]
        if not finalized:
            raise ReserveInvalid("FINAL_CAPTURE rejected: command unresolved (not FINAL)")
        known: set[str] = set()
        for command in finalized:
            known |= _known_result_ids_for_command(journal, command)
        if result_id not in known:
            raise ReserveInvalid("FINAL_CAPTURE resultId does not match stored result")
        return

    if kind == "RECONCILE":
        command_id = capture_ack.get("commandId")
        disposition = capture_ack.get("disposition")
        if not isinstance(command_id, str) or not command_id:
            raise ReserveInvalid("RECONCILE requires commandId")
        if disposition not in _RECONCILE_DISPOSITIONS:
            raise ReserveInvalid(
                "RECONCILE disposition must be FAILED|CANCEL_REQUESTED|DELIVERY_AMBIGUOUS"
            )
        if capture_ack.get("acknowledged") is not True:
            raise ReserveInvalid("RECONCILE requires acknowledged=true")
        command = journal.get_command(command_id)
        if command is None or str(command["reservation_id"] or "") != str(reservation_id):
            raise ReserveInvalid("RECONCILE commandId does not belong to reservation")
        if not _reconcile_disposition_allowed(command, str(disposition)):
            raise ReserveInvalid(
                f"RECONCILE disposition {disposition} does not match command stage "
                f"{command['stage']}"
            )
        return

    # Unauthorized shapes — must not release even when fields are present.
    if capture_ack.get("expired") is True:
        raise ReserveInvalid("expiry alone cannot release reservation")
    if capture_ack.get("copySuccess") is True or capture_ack.get("copy") is True:
        raise ReserveInvalid("copy success alone cannot release reservation")
    if "taskId" in capture_ack or "runId" in capture_ack:
        raise ReserveInvalid("taskId/runId alone cannot release reservation")
    if not capture_ack:
        raise ReserveInvalid("captureAck must include kind FINAL_CAPTURE|RECONCILE")
    raise ReserveInvalid("captureAck.kind must be FINAL_CAPTURE or RECONCILE")


class WriterDenied(Exception):
    """Writer guard rejection; carries frozen failure fields for callers/hooks."""

    def __init__(self, code: str, detail: str, *, side_effect: str = SIDE_EFFECT_NONE, retry_action: str = ""):
        self.code = code
        self.detail = detail
        self.side_effect = side_effect
        self.retry_action = retry_action
        super().__init__(detail)

    def as_error(self) -> dict[str, Any]:
        return build_error(self.code, self.detail, self.side_effect, self.retry_action)


def _writer_permit_on_journal(
    journal: Journal,
    *,
    runtime_id: str,
    socket_path: str,
    mode: str,
    reservation_id: str | None = None,
    lease_token: str | None = None,
    fence: str | None = None,
) -> dict[str, Any]:
    """Evaluate writer ownership using an already-open journal connection."""
    now_ns = boot_time_ns()
    active = journal._active_reservation(runtime_id)
    if active is None:
        return {
            "state": "FREE",
            "runtimeId": runtime_id,
            "mode": mode,
            "socketPath": str(socket_path),
        }

    active = journal._mark_expired_if_needed(active, now_ns)
    state = str(active["state"])
    if state in {"HELD", "EXPIRED_HELD"}:
        if reservation_id is None or lease_token is None or fence is None:
            raise WriterDenied(
                "BUSY",
                f"runtime {runtime_id} is {state}",
                retry_action="wait_for_release_or_query",
            )
        if str(active["reservation_id"]) != str(reservation_id):
            raise WriterDenied(
                "BUSY",
                "reservation does not match active holder",
                retry_action="wait_for_release_or_query",
            )
        if str(active["fence"]) != str(fence):
            raise WriterDenied(
                "BUSY",
                "stale fence",
                retry_action="wait_for_release_or_query",
            )
        if active["lease_token_hash"] != _token_hash(lease_token):
            raise WriterDenied("FORBIDDEN", "lease token mismatch")
        if str(active["mode"]) != mode:
            raise WriterDenied(
                "BUSY",
                f"reservation mode {active['mode']} does not match requested {mode}",
                retry_action="wait_for_release_or_query",
            )
        return {
            "state": state,
            "runtimeId": runtime_id,
            "mode": mode,
            "reservationId": str(active["reservation_id"]),
            "fence": str(active["fence"]),
            "socketPath": str(socket_path),
        }
    return {
        "state": state,
        "runtimeId": runtime_id,
        "mode": mode,
        "socketPath": str(socket_path),
    }


def require_writer_permit(
    *,
    runtime_id: str,
    socket_path: str,
    mode: str,
    reservation_id: str | None = None,
    lease_token: str | None = None,
    fence: str | None = None,
    host_key: str | None = None,
    uid: str | int | None = None,
) -> dict[str, Any]:
    """Check journal writer ownership before mutating terminal input.

    FREE (no active reservation): permit granted for Slice 3 plumbing tests.
    HELD / EXPIRED_HELD without matching grant+fence: BUSY.
    Matching reservationId + leaseToken + fence: permit.
    """
    if not runtime_id:
        raise WriterDenied("INVALID_ARGUMENT", "runtime_id is required")
    if not socket_path or not str(socket_path).startswith("/"):
        raise WriterDenied("INVALID_ARGUMENT", "socket_path must be an absolute path")
    if mode not in {"MANAGED", "DIRECT"}:
        raise WriterDenied("INVALID_ARGUMENT", "mode must be MANAGED|DIRECT")

    scope_host = str(host_key) if host_key is not None else local_host_key()
    scope_uid = str(uid) if uid is not None else local_uid()
    path = journal_path_for_scope(scope_id_for_socket(scope_host, scope_uid, str(socket_path)))
    try:
        with open_journal(path) as journal:
            journal.conn.execute("BEGIN IMMEDIATE")
            try:
                permit = _writer_permit_on_journal(
                    journal,
                    runtime_id=runtime_id,
                    socket_path=str(socket_path),
                    mode=mode,
                    reservation_id=reservation_id,
                    lease_token=lease_token,
                    fence=fence,
                )
                journal.conn.execute("COMMIT")
                return permit
            except WriterDenied:
                try:
                    journal.conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
            except Exception:
                try:
                    journal.conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
    except JournalError as exc:
        raise WriterDenied("UNSUPPORTED", f"journal unavailable: {exc}", retry_action="repair_journal") from exc


def make_writer_pre_send_hook(
    *,
    runtime_id: str,
    socket_path: str,
    mode: str,
    reservation_id: str | None = None,
    lease_token: str | None = None,
    fence: str | None = None,
    host_key: str | None = None,
    uid: str | int | None = None,
) -> Any:
    """Return a pre_send_hook that raises WriterDenied when input is not permitted."""

    def _hook() -> dict[str, Any]:
        return require_writer_permit(
            runtime_id=runtime_id,
            socket_path=socket_path,
            mode=mode,
            reservation_id=reservation_id,
            lease_token=lease_token,
            fence=fence,
            host_key=host_key,
            uid=uid,
        )

    return _hook


def canonical_tmux_socket_path(socket_path: str | None = None) -> str:
    """Absolute socket path. If None, resolve default via TMUX env or /tmp/tmux-<uid>/default."""
    if socket_path:
        path = Path(socket_path)
        return str(path if path.is_absolute() else path.resolve())
    tmux_env = os.environ.get("TMUX")
    if tmux_env:
        sock = tmux_env.split(",", 1)[0].strip()
        if sock:
            path = Path(sock)
            return str(path if path.is_absolute() else path.resolve())
    return f"/tmp/tmux-{os.getuid()}/default"


def _context_blocks_direct_target(context: Any, target: str) -> bool:
    if not isinstance(context, dict):
        return False
    if context.get("blockAllDirectOnSocket") is True:
        return True
    for key in ("paneId", "tmuxTarget", "target", "pane"):
        value = context.get(key)
        if value is not None and str(value) == str(target):
            return True
    return False


def guard_tmux_writer(*, target: str, socket_path: str | None = None, mode: str = "DIRECT") -> None:
    """Deny Direct-style input when an active reservation blocks the physical target.

    FREE (no active blocker) allows current Direct UX without acquire-on-every-send.
    Matching DIRECT credentials are not required here; this helper is pre-send denial only.
    """
    del mode  # caller mode documented for API symmetry; Direct auto-guard always denies blockers
    resolved = canonical_tmux_socket_path(socket_path)
    if not resolved.startswith("/"):
        raise WriterDenied("INVALID_ARGUMENT", "socket_path must resolve to an absolute path")
    path = journal_path_for_scope(scope_id_for_socket(local_host_key(), local_uid(), resolved))
    if not path.exists():
        return
    blocked_by: tuple[str, str] | None = None
    try:
        with open_journal(path) as journal:
            journal.conn.execute("BEGIN IMMEDIATE")
            try:
                now_ns = boot_time_ns()
                rows = journal.conn.execute(
                    "SELECT * FROM reservations WHERE state IN ('HELD', 'EXPIRED_HELD') "
                    "ORDER BY updated_at DESC"
                ).fetchall()
                for row in rows:
                    row = journal._mark_expired_if_needed(row, now_ns)
                    if str(row["state"]) not in {"HELD", "EXPIRED_HELD"}:
                        continue
                    try:
                        context = json.loads(row["context_json"])
                    except (TypeError, json.JSONDecodeError):
                        context = None
                    if _context_blocks_direct_target(context, target):
                        blocked_by = (str(row["mode"]), str(row["runtime_id"]))
                        break
                journal.conn.execute("COMMIT")
            except Exception:
                try:
                    journal.conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
    except JournalError as exc:
        raise WriterDenied("UNSUPPORTED", f"journal unavailable: {exc}", retry_action="repair_journal") from exc
    if blocked_by is not None:
        holder_mode, runtime_id = blocked_by
        raise WriterDenied(
            "BUSY",
            f"{holder_mode} reservation holds target {target} on runtime {runtime_id}",
            retry_action="wait_for_release_or_query",
        )


def open_journal(path: Path) -> Journal:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        conn = sqlite3.connect(str(path), isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA synchronous=FULL")
        row = conn.execute("PRAGMA user_version").fetchone()
        version = int(row[0]) if row is not None else 0
        tables = {
            str(r[0])
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }
        if version == 0 and not tables:
            # executescript issues its own COMMIT first; do not wrap in BEGIN/ROLLBACK.
            try:
                conn.executescript(_SCHEMA_SQL)
                conn.execute(f"PRAGMA user_version={USER_VERSION}")
                conn.execute(
                    "INSERT INTO meta(key, value) VALUES ('schema', ?)",
                    ("runtime-v1",),
                )
            except Exception as exc:
                conn.close()
                raise JournalError(f"journal init failed: {exc}") from exc
        elif version == USER_VERSION:
            missing = _REQUIRED_TABLES - tables
            if missing:
                conn.close()
                raise JournalError(f"journal schema incomplete, missing tables: {sorted(missing)}")
        else:
            conn.close()
            raise JournalError(f"journal user_version mismatch: got {version}, want {USER_VERSION}")
        # Probe readability without mutating durable content.
        conn.execute("SELECT COUNT(*) FROM meta").fetchone()
        return Journal(path, conn)
    except JournalError:
        raise
    except sqlite3.Error as exc:
        raise JournalError(f"journal open failed: {exc}") from exc


def _validate_envelope(request: Any) -> tuple[dict[str, Any] | None, dict[str, Any] | None, int]:
    if not isinstance(request, dict):
        err = build_error("INVALID_ARGUMENT", "request must be a JSON object")
        return None, build_response(None, False, error=err), 3
    request_id = request.get("requestId")
    if not isinstance(request_id, str) or not request_id:
        err = build_error("INVALID_ARGUMENT", "requestId is required")
        return None, build_response(request_id if isinstance(request_id, str) else None, False, error=err), 3
    if request.get("contractVersion") != CONTRACT_VERSION:
        err = build_error("INVALID_ARGUMENT", "contractVersion must be 1")
        return None, build_response(request_id, False, error=err), 3
    operation = request.get("operation")
    if not isinstance(operation, str) or not operation:
        err = build_error("INVALID_ARGUMENT", "operation is required")
        return None, build_response(request_id, False, error=err), 3
    allowed = _ALLOWED_BY_OPERATION.get(operation)
    if allowed is None:
        err = build_error("INVALID_ARGUMENT", f"unknown operation: {operation}")
        return None, build_response(request_id, False, error=err), 3
    unknown = sorted(set(request) - allowed)
    if unknown:
        err = build_error("INVALID_ARGUMENT", f"unknown fields: {', '.join(unknown)}")
        return None, build_response(request_id, False, error=err), 3
    return request, None, 0


def _resolve_scope_parts(request: dict[str, Any]) -> tuple[str, str, str] | None:
    scope = request.get("serverScope")
    host_key = request.get("hostKey")
    uid = request.get("uid")
    socket_path = request.get("socketPath")
    if isinstance(scope, dict):
        host_key = scope.get("hostKey", host_key)
        uid = scope.get("uid", uid)
        socket_path = scope.get("socketPath", socket_path)
    if not socket_path:
        return None
    if host_key is None:
        host_key = local_host_key()
    if uid is None:
        uid = local_uid()
    return str(host_key), str(uid), str(socket_path)


def _journal_path_from_request(request: dict[str, Any]) -> Path:
    parts = _resolve_scope_parts(request)
    if parts is None:
        return journal_path_for_scope("synthetic")
    host_key, uid, socket_path = parts
    return journal_path_for_scope(scope_id_for_socket(host_key, uid, socket_path))


def _require_socket_path(request: dict[str, Any]) -> str:
    parts = _resolve_scope_parts(request)
    if parts is None:
        raise ReserveInvalid("socketPath is required")
    socket_path = parts[2]
    if not isinstance(socket_path, str) or not socket_path.startswith("/"):
        raise ReserveInvalid("socketPath must be an absolute path")
    return socket_path


def _candidate_from_observation(raw: dict[str, Any], stamp: str) -> dict[str, Any]:
    evidence = raw.get("identityEvidence")
    if not isinstance(evidence, dict):
        evidence = {}
    missing = identity_missing_fields(evidence)
    issuable = not missing
    runtime_id = None
    identity_out: dict[str, Any] | None
    if issuable:
        identity_out = build_identity(**evidence)
        runtime_id = runtime_id_from_identity(identity_out)
    else:
        identity_out = dict(evidence)
    agent_kind = evidence.get("agentKind") if isinstance(evidence.get("agentKind"), str) else raw.get("agentKind")
    process_state = raw.get("processState", "UP" if issuable else "DOWN")
    # Bare "claude" (unknown/missing CLAUDE_CONFIG_DIR) is never Managed-issuable.
    if agent_kind == "claude" or process_state == "DOWN":
        issuable = False
        runtime_id = None
    return {
        "runtimeId": runtime_id,
        "identityEvidence": identity_out,
        "agentKind": agent_kind,
        "profileRoot": raw.get("profileRoot"),
        "workspaceRoot": raw.get("workspaceRoot"),
        "expectedSession": raw.get("expectedSession"),
        "mappingState": raw.get("mappingState", "UNMAPPED"),
        "capabilities": managed_capabilities(agent_kind if isinstance(agent_kind, str) else None),
        "issuable": issuable,
        "missingIdentityFields": missing,
        "processState": process_state,
        "observedAt": raw.get("observedAt") or stamp,
    }


def _handle_discover(request: dict[str, Any], journal: Journal) -> tuple[dict[str, Any], int]:
    del journal  # discover is read-only aside from idempotency persistence
    request_id = str(request["requestId"])
    try:
        socket_path = _require_socket_path(request)
    except ReserveInvalid as exc:
        err = build_error("INVALID_ARGUMENT", str(exc))
        return build_response(request_id, False, error=err), 3
    agent_kind = request.get("agentKind")
    if agent_kind is not None and not isinstance(agent_kind, str):
        err = build_error("INVALID_ARGUMENT", "agentKind must be a string")
        return build_response(request_id, False, error=err), 3
    stamp = observed_at_now()
    observed = observe_candidates(socket_path, agent_kind)
    candidates = []
    for raw in observed:
        if not isinstance(raw, dict):
            continue
        candidate = _candidate_from_observation(raw, stamp)
        if agent_kind and candidate.get("agentKind") != agent_kind:
            continue
        candidates.append(candidate)
    data = {
        "socketPath": socket_path,
        "candidates": candidates,
        "sideEffect": SIDE_EFFECT_NONE,
    }
    return build_response(request_id, True, data=data, observed_at=stamp), 0


def _context_values_equal(key: str, expected_value: Any, observed_value: Any) -> bool:
    if expected_value == observed_value:
        return True
    if key in {"profileRoot", "workspaceRoot"} and expected_value is not None and observed_value is not None:
        try:
            return (
                Path(str(expected_value)).expanduser().resolve(strict=False)
                == Path(str(observed_value)).expanduser().resolve(strict=False)
            )
        except OSError:
            return str(expected_value) == str(observed_value)
    return False


def _context_mismatch(expected: dict[str, Any], observed: dict[str, Any]) -> str | None:
    for key in ("agentKind", "profileRoot", "workspaceRoot", "expectedSession"):
        if key in expected and expected[key] is not None:
            if not _context_values_equal(key, expected[key], observed.get(key)):
                return key
    return None


def _find_observation(
    socket_path: str,
    *,
    runtime_id: str | None,
    selector: dict[str, Any] | None,
) -> dict[str, Any] | None:
    stamp = observed_at_now()
    for raw in observe_candidates(socket_path, None):
        if not isinstance(raw, dict):
            continue
        candidate = _candidate_from_observation(raw, stamp)
        if runtime_id and candidate.get("runtimeId") == runtime_id:
            return candidate
        if selector and isinstance(selector, dict):
            evidence = candidate.get("identityEvidence") or {}
            matched = True
            for key, value in selector.items():
                if key == "socketPath":
                    continue
                if evidence.get(key) != value and candidate.get(key) != value:
                    matched = False
                    break
            if matched:
                return candidate
    return None


def _handle_status(request: dict[str, Any], journal: Journal) -> tuple[dict[str, Any], int]:
    request_id = str(request["requestId"])
    runtime_id = request.get("runtimeId")
    selector = request.get("selector")
    expected = request.get("expectedContext")
    if runtime_id is not None and not isinstance(runtime_id, str):
        err = build_error("INVALID_ARGUMENT", "runtimeId must be a string")
        return build_response(request_id, False, error=err), 3
    if selector is not None and not isinstance(selector, dict):
        err = build_error("INVALID_ARGUMENT", "selector must be an object")
        return build_response(request_id, False, error=err), 3
    if expected is not None and not isinstance(expected, dict):
        err = build_error("INVALID_ARGUMENT", "expectedContext must be an object")
        return build_response(request_id, False, error=err), 3
    if not runtime_id and not selector:
        err = build_error("INVALID_ARGUMENT", "runtimeId or selector is required")
        return build_response(request_id, False, error=err), 3
    try:
        if selector and isinstance(selector.get("socketPath"), str):
            socket_path = selector["socketPath"]
            if not socket_path.startswith("/"):
                raise ReserveInvalid("selector.socketPath must be absolute")
        else:
            socket_path = _require_socket_path(request)
    except ReserveInvalid as exc:
        err = build_error("INVALID_ARGUMENT", str(exc))
        return build_response(request_id, False, error=err), 3

    candidate = _find_observation(socket_path, runtime_id=runtime_id, selector=selector)
    reservation = None
    command = None
    if runtime_id:
        reservation = journal.reservation_summary(runtime_id)
        command = journal.latest_command_summary(runtime_id)
    elif candidate and candidate.get("runtimeId"):
        reservation = journal.reservation_summary(str(candidate["runtimeId"]))
        command = journal.latest_command_summary(str(candidate["runtimeId"]))

    if candidate is None:
        if runtime_id:
            err = build_error("DOWN", f"runtime {runtime_id} not observed on socket", retry_action="rediscover")
            return build_response(request_id, False, error=err), 2
        err = build_error("UNMAPPED", "selector did not match any runtime", retry_action="discover")
        return build_response(request_id, False, error=err), 2

    process_state = str(candidate.get("processState") or "DOWN")
    if process_state == "DOWN":
        err = build_error("DOWN", "observed processState is DOWN", retry_action="rediscover")
        data = {
            "runtimeId": candidate.get("runtimeId") or runtime_id,
            "processState": "DOWN",
            "inputState": "INPUT_STATE_UNKNOWN",
            "capabilities": candidate.get("capabilities"),
            "reservation": reservation,
            "command": command,
        }
        if isinstance(data.get("reservation"), dict):
            data["reservation"].pop("leaseToken", None)
        return build_response(request_id, False, data=data, error=err), 2

    compare_against = {
        "agentKind": candidate.get("agentKind"),
        "profileRoot": candidate.get("profileRoot"),
        "workspaceRoot": candidate.get("workspaceRoot"),
        "expectedSession": candidate.get("expectedSession"),
    }
    frozen = (reservation or {}).get("context") if reservation else None
    if isinstance(frozen, dict):
        for key in ("profileRoot", "workspaceRoot", "agentKind", "expectedSession"):
            if compare_against.get(key) is None and key in frozen:
                compare_against[key] = frozen[key]
    if expected:
        mismatched = _context_mismatch(expected, compare_against)
        if mismatched:
            err = build_error(
                "MISMATCH",
                f"expectedContext.{mismatched} does not match observed context",
                retry_action="rediscover",
            )
            return build_response(request_id, False, error=err), 2

    agent_kind = candidate.get("agentKind")
    data = {
        "runtimeId": candidate.get("runtimeId") or runtime_id,
        "processState": process_state,
        "inputState": "INPUT_STATE_UNKNOWN",
        "context": {
            "agentKind": agent_kind,
            "profileRoot": candidate.get("profileRoot"),
            "workspaceRoot": candidate.get("workspaceRoot"),
            "expectedSession": candidate.get("expectedSession"),
            "paneId": (candidate.get("identityEvidence") or {}).get("paneId")
            if isinstance(candidate.get("identityEvidence"), dict)
            else None,
        },
        "identityEvidence": candidate.get("identityEvidence"),
        "reservation": reservation,
        "command": command,
        "capabilities": candidate.get("capabilities") or managed_capabilities(
            agent_kind if isinstance(agent_kind, str) else None
        ),
        "mappingState": candidate.get("mappingState"),
    }
    snap = _live_snapshot_for_candidate(candidate, socket_path)
    if snap:
        data["currentSnapshotHash"] = snap
    if isinstance(data.get("reservation"), dict):
        data["reservation"].pop("leaseToken", None)
    return build_response(request_id, True, data=data), 0


def _handle_reserve(request: dict[str, Any], journal: Journal) -> tuple[dict[str, Any], int]:
    request_id = str(request["requestId"])
    action = request.get("action")
    if action not in {"acquire", "renew", "release"}:
        err = build_error("INVALID_ARGUMENT", "action must be acquire|renew|release")
        return build_response(request_id, False, error=err), 3

    try:
        if action == "acquire":
            runtime_id = request.get("runtimeId")
            mode = request.get("mode")
            expected = request.get("expectedContext")
            if not isinstance(runtime_id, str) or not runtime_id:
                raise ReserveInvalid("runtimeId is required")
            if mode not in {"MANAGED", "DIRECT"}:
                raise ReserveInvalid("mode must be MANAGED|DIRECT")
            if not isinstance(expected, dict):
                raise ReserveInvalid("expectedContext must be an object")
            owner_ref = request.get("ownerRef")
            if owner_ref is not None and not isinstance(owner_ref, str):
                raise ReserveInvalid("ownerRef must be a string")
            correlation = request.get("correlationDigest")
            if correlation is not None and not isinstance(correlation, str):
                raise ReserveInvalid("correlationDigest must be a string")

            socket_path: str | None = None
            try:
                socket_path = _require_socket_path(request)
            except ReserveInvalid:
                socket_path = None

            frozen_context = dict(expected)
            snapshot_hash: str | None = None
            observation_cursor: dict[str, Any] | None = None
            if socket_path:
                live_candidates = observe_candidates(socket_path, expected.get("agentKind") if isinstance(expected.get("agentKind"), str) else None)
                matched = None
                for raw in live_candidates:
                    cand = _candidate_from_observation(raw, observed_at_now())
                    if cand.get("runtimeId") == runtime_id:
                        matched = cand
                        break
                if matched is not None:
                    if str(matched.get("processState") or "DOWN") == "DOWN" or not matched.get("issuable"):
                        raise ReserveInvalid(f"runtime {runtime_id} is not an issuable UP Managed candidate")
                    frozen_context = _freeze_context_from_live(expected, matched)
                    snapshot_hash = _live_snapshot_for_candidate(matched, socket_path)
                    if not snapshot_hash:
                        raise ReserveInvalid("unable to capture currentSnapshotHash for live pane")
                    observation_cursor = {"kind": "BOOTSTRAP", "runtimeId": runtime_id}
                elif live_candidates:
                    # Socket has other live agents but not this runtimeId — fail closed.
                    raise ReserveInvalid(
                        f"runtime {runtime_id} not observed on socket (stale or wrong incarnation)"
                    )
                # else: empty observation (unit tests / no panes) — journal-only acquire, no synthetic hash.

            data = journal.acquire(
                runtime_id=runtime_id,
                mode=mode,
                expected_context=frozen_context,
                owner_ref=owner_ref,
                correlation_digest=correlation,
            )
            if observation_cursor is not None:
                # Persist BOOTSTRAP cursor on the reservation row when live-bound.
                journal.conn.execute(
                    "UPDATE reservations SET observation_cursor_json = ?, updated_at = ? "
                    "WHERE reservation_id = ?",
                    (
                        json.dumps(observation_cursor, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
                        observed_at_now(),
                        data["reservationId"],
                    ),
                )
                data["observationCursor"] = observation_cursor
            if snapshot_hash is not None:
                data["currentSnapshotHash"] = snapshot_hash
            data["context"] = frozen_context
            return build_response(request_id, True, data=data), 0

        reservation_id = request.get("reservationId")
        lease_token = request.get("leaseToken")
        fence = request.get("fence")
        if not isinstance(reservation_id, str) or not reservation_id:
            raise ReserveInvalid("reservationId is required")
        if not isinstance(lease_token, str) or not lease_token:
            raise ReserveInvalid("leaseToken is required")
        if not isinstance(fence, str) or not fence:
            raise ReserveInvalid("fence is required")

        if action == "renew":
            data = journal.renew(reservation_id=reservation_id, lease_token=lease_token, fence=fence)
            try:
                socket_path = _require_socket_path(request)
            except ReserveInvalid:
                socket_path = None
            if socket_path:
                runtime_id = str(data.get("runtimeId") or "")
                candidate = _find_observation(socket_path, runtime_id=runtime_id, selector=None) if runtime_id else None
                if candidate is not None:
                    snap = _live_snapshot_for_candidate(candidate, socket_path)
                    if snap:
                        data["currentSnapshotHash"] = snap
            return build_response(request_id, True, data=data), 0

        capture_ack = request.get("captureAck")
        data = journal.release(
            reservation_id=reservation_id,
            lease_token=lease_token,
            fence=fence,
            capture_ack=capture_ack,
        )
        return build_response(request_id, True, data=data), 0
    except ReserveBusy as exc:
        err = build_error("BUSY", str(exc), retry_action="wait_for_release_or_query")
        return build_response(request_id, False, error=err), 2
    except ReserveForbidden as exc:
        err = build_error("FORBIDDEN", str(exc))
        return build_response(request_id, False, error=err), 3
    except ReserveInvalid as exc:
        err = build_error("INVALID_ARGUMENT", str(exc))
        return build_response(request_id, False, error=err), 3


def _immutable_send_payload(request: dict[str, Any]) -> dict[str, Any]:
    return {
        "commandId": request.get("commandId"),
        "runtimeId": request.get("runtimeId"),
        "expectedContext": request.get("expectedContext"),
        "correlationDigest": request.get("correlationDigest"),
        "wirePrompt": request.get("wirePrompt"),
        "promptSha256": request.get("promptSha256"),
        "observationCursor": request.get("observationCursor"),
    }


def _parse_confirmed_at(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value:
        try:
            return float(value)
        except ValueError:
            pass
        try:
            text = value.replace("Z", "+00:00")
            return datetime.fromisoformat(text).timestamp()
        except ValueError:
            return None
    return None


def _validate_input_permit(
    permit: Any,
    *,
    command_id: str,
    runtime_id: str,
    fence: str,
    current_snapshot_hash: str | None,
) -> str | None:
    """Return error detail if invalid; None if OK. Caller maps to INPUT_STATE_UNKNOWN."""
    if not isinstance(permit, dict):
        return "inputPermit must be an object"
    if permit.get("commandId") != command_id:
        return "inputPermit.commandId mismatch"
    if permit.get("runtimeId") != runtime_id:
        return "inputPermit.runtimeId mismatch"
    if str(permit.get("fence")) != str(fence):
        return "inputPermit.fence mismatch"
    if permit.get("paneMode") != "normal":
        return "inputPermit.paneMode must be normal"
    confirmed = _parse_confirmed_at(permit.get("confirmedAt"))
    if confirmed is None:
        return "inputPermit.confirmedAt missing or invalid"
    age = wall_time_s() - confirmed
    if age < 0 or age > INPUT_PERMIT_MAX_AGE_S:
        return "inputPermit expired or not yet valid"
    snapshot = permit.get("snapshotHash")
    if not isinstance(snapshot, str) or not snapshot:
        return "inputPermit.snapshotHash required"
    if current_snapshot_hash is not None and current_snapshot_hash != snapshot:
        return "snapshot hash mismatch"
    return None


def _ensure_txn(journal: Journal) -> None:
    if not journal.conn.in_transaction:
        journal.conn.execute("BEGIN IMMEDIATE")


def _commit_durable(journal: Journal) -> None:
    journal.conn.execute("COMMIT")


class SendRejected(Exception):
    def __init__(self, code: str, detail: str, *, side_effect: str = SIDE_EFFECT_NONE, retry_action: str = "", exit_code: int | None = None):
        self.code = code
        self.detail = detail
        self.side_effect = side_effect
        self.retry_action = retry_action
        self.exit_code = exit_code if exit_code is not None else exit_code_for_error(code)
        super().__init__(detail)


def _handle_send(request: dict[str, Any], journal: Journal) -> tuple[dict[str, Any], int]:
    request_id = str(request["requestId"])
    try:
        runtime_id = request.get("runtimeId")
        expected = request.get("expectedContext")
        reservation_id = request.get("reservationId")
        lease_token = request.get("leaseToken")
        fence = request.get("fence")
        command_id = request.get("commandId")
        correlation = request.get("correlationDigest")
        wire_prompt = request.get("wirePrompt")
        prompt_sha = request.get("promptSha256")
        cursor = request.get("observationCursor")
        permit = request.get("inputPermit")
        current_snapshot = request.get("currentSnapshotHash")

        for name, value in (
            ("runtimeId", runtime_id),
            ("reservationId", reservation_id),
            ("leaseToken", lease_token),
            ("fence", fence),
            ("commandId", command_id),
            ("wirePrompt", wire_prompt),
            ("promptSha256", prompt_sha),
        ):
            if not isinstance(value, str) or not value:
                raise SendRejected("INVALID_ARGUMENT", f"{name} is required")
        if not isinstance(expected, dict):
            raise SendRejected("INVALID_ARGUMENT", "expectedContext must be an object")
        if correlation is not None and not isinstance(correlation, str):
            raise SendRejected("INVALID_ARGUMENT", "correlationDigest must be a string")
        if cursor is not None and not isinstance(cursor, (dict, str)):
            raise SendRejected("INVALID_ARGUMENT", "observationCursor must be an object or string")
        if current_snapshot is not None and not isinstance(current_snapshot, str):
            raise SendRejected("INVALID_ARGUMENT", "currentSnapshotHash must be a string")

        wire_bytes = wire_prompt.encode("utf-8")
        if len(wire_bytes) > MAX_PROMPT_BYTES:
            raise SendRejected("INVALID_ARGUMENT", "wirePrompt exceeds 64 KiB")
        actual_sha = sha256_hex(wire_bytes)
        if actual_sha != prompt_sha:
            raise SendRejected("INVALID_ARGUMENT", "promptSha256 does not match wirePrompt")

        pane_id = expected.get("paneId")
        if not isinstance(pane_id, str) or not pane_id:
            raise SendRejected("INVALID_ARGUMENT", "expectedContext.paneId is required for send")

        parts = _resolve_scope_parts(request)
        if parts is None:
            raise SendRejected("INVALID_ARGUMENT", "socketPath is required")
        host_key, uid, socket_path = parts
        if not socket_path.startswith("/"):
            raise SendRejected("INVALID_ARGUMENT", "socketPath must be an absolute path")

        permit_err = _validate_input_permit(
            permit,
            command_id=command_id,
            runtime_id=runtime_id,
            fence=fence,
            current_snapshot_hash=current_snapshot,
        )
        if permit_err:
            raise SendRejected("INPUT_STATE_UNKNOWN", permit_err, retry_action="refresh_input_permit")

        # Lease/writer check before any durable send intent (same open journal txn).
        try:
            _writer_permit_on_journal(
                journal,
                runtime_id=runtime_id,
                socket_path=socket_path,
                mode="MANAGED",
                reservation_id=reservation_id,
                lease_token=lease_token,
                fence=fence,
            )
        except WriterDenied as denied:
            raise SendRejected(denied.code, denied.detail, side_effect=denied.side_effect, retry_action=denied.retry_action) from denied

        # Live revalidation before PREPARED/ATTEMPTING when the socket has an observable runtime.
        # Unit tests with empty observe_candidates skip this path (no synthetic identity).
        reservation_row = journal.conn.execute(
            "SELECT context_json FROM reservations WHERE reservation_id = ?",
            (reservation_id,),
        ).fetchone()
        reservation_context = None
        if reservation_row and reservation_row["context_json"]:
            try:
                reservation_context = json.loads(reservation_row["context_json"])
            except json.JSONDecodeError:
                reservation_context = None
        live_for_runtime = _find_observation(socket_path, runtime_id=runtime_id, selector=None)
        if live_for_runtime is not None:
            permit_snap = permit.get("snapshotHash") if isinstance(permit, dict) else None
            _revalidate_live_send_target(
                socket_path=socket_path,
                runtime_id=runtime_id,
                expected=expected,
                reservation_context=reservation_context if isinstance(reservation_context, dict) else None,
                current_snapshot=current_snapshot if isinstance(current_snapshot, str) else None,
                permit_snapshot=permit_snap if isinstance(permit_snap, str) else None,
            )
        elif isinstance(reservation_context, dict) and reservation_context.get("paneId") not in (None, pane_id):
            raise SendRejected("MISMATCH", "expectedContext.paneId does not match reservation frozen paneId")

        immutable = _immutable_send_payload(request)
        immutable_sha = sha256_hex(canonical_json(immutable))
        cursor_json = (
            json.dumps(cursor, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
            if isinstance(cursor, dict)
            else (cursor if isinstance(cursor, str) else None)
        )

        existing = journal.get_command(command_id)
        if existing is not None:
            if str(existing["request_body_sha256"]) != immutable_sha:
                raise SendRejected("CONFLICT", "commandId reused with different immutable payload")
            stage = str(existing["stage"])
            if stage in _POST_PREPARED_STAGES:
                data = {
                    "command": journal.command_public_view(existing),
                    "replayed": True,
                    "inputPerformed": False,
                }
                return build_response(request_id, True, data=data), 0
            if stage == "PREPARED":
                if str(existing["request_id"]) == request_id:
                    # Same request should have been satisfied via idempotency table; treat as replay.
                    data = {
                        "command": journal.command_public_view(existing),
                        "replayed": True,
                        "inputPerformed": False,
                    }
                    return build_response(request_id, True, data=data), 0
                # Resume with new requestId; payload already matched.
                journal.update_command(command_id, request_id=request_id)
            else:
                raise SendRejected("CONFLICT", f"unsupported command stage for send: {stage}")
        else:
            journal.insert_command_prepared(
                command_id=command_id,
                runtime_id=runtime_id,
                reservation_id=reservation_id,
                request_id=request_id,
                request_body_sha256=immutable_sha,
                wire_prompt=wire_prompt,
                wire_prompt_sha256=prompt_sha,
                cursor_json=cursor_json,
            )
            journal.add_receipt(
                command_id=command_id,
                request_id=request_id,
                kind="COMMAND_PREPARED",
                stage="PREPARED",
                side_effect=SIDE_EFFECT_NONE,
                payload={"commandId": command_id},
            )

        # Durable PREPARED before ATTEMPTING.
        _commit_durable(journal)
        _ensure_txn(journal)
        journal.update_command(command_id, stage="ATTEMPTING")
        journal.add_receipt(
            command_id=command_id,
            request_id=request_id,
            kind="COMMAND_ATTEMPTING",
            stage="ATTEMPTING",
            side_effect=SIDE_EFFECT_NONE,
            payload={"commandId": command_id},
        )
        _commit_durable(journal)

        hook = make_writer_pre_send_hook(
            runtime_id=runtime_id,
            socket_path=socket_path,
            mode="MANAGED",
            reservation_id=reservation_id,
            lease_token=lease_token,
            fence=fence,
            host_key=host_key,
            uid=uid,
        )
        try:
            transport = transport_send_prompt(
                pane_id,
                wire_prompt,
                socket_path=socket_path,
                press_enter=True,
                pre_send_hook=hook,
            )
        except Exception as exc:
            transport = {
                "ok": False,
                "error": str(exc),
                "sideEffect": SIDE_EFFECT_POSSIBLE,
                "deliveryDisposition": "DELIVERY_AMBIGUOUS",
                "completedStages": [],
                "failedStage": "transport",
            }

        _ensure_txn(journal)
        if transport.get("ok"):
            row = journal.update_command(
                command_id,
                stage="TRANSPORT_SENT",
                delivery_disposition="TRANSPORT_SENT",
            )
            journal.add_receipt(
                command_id=command_id,
                request_id=request_id,
                kind="TRANSPORT_SENT",
                stage="TRANSPORT_SENT",
                side_effect=str(transport.get("sideEffect") or SIDE_EFFECT_OBSERVED),
                payload={"stages": transport.get("stages"), "completedStages": transport.get("completedStages")},
            )
            cursor_obj = cursor if isinstance(cursor, dict) else None
            source_path = None
            if isinstance(cursor_obj, dict):
                source_path = cursor_obj.get("path")
            if observe_agent_received(
                command_id,
                wire_prompt=wire_prompt,
                cursor=cursor_obj,
                path=source_path,
                session_id=(expected.get("expectedSession") if isinstance(expected.get("expectedSession"), str) else None),
            ):
                row = journal.update_command(command_id, stage="AGENT_RECEIVED")
                journal.add_receipt(
                    command_id=command_id,
                    request_id=request_id,
                    kind="AGENT_RECEIVED",
                    stage="AGENT_RECEIVED",
                    side_effect=SIDE_EFFECT_OBSERVED,
                    payload={"commandId": command_id},
                )
            data = {
                "command": journal.command_public_view(row),
                "transport": transport,
                "inputPerformed": True,
            }
            return build_response(request_id, True, data=data), 0

        row = journal.update_command(
            command_id,
            stage="DELIVERY_AMBIGUOUS",
            delivery_disposition="DELIVERY_AMBIGUOUS",
        )
        journal.add_receipt(
            command_id=command_id,
            request_id=request_id,
            kind="DELIVERY_AMBIGUOUS",
            stage="DELIVERY_AMBIGUOUS",
            side_effect=str(transport.get("sideEffect") or SIDE_EFFECT_POSSIBLE),
            payload={
                "error": transport.get("error"),
                "completedStages": transport.get("completedStages"),
                "failedStage": transport.get("failedStage"),
            },
        )
        err = build_error(
            "DELIVERY_AMBIGUOUS",
            str(transport.get("error") or "transport failed after ATTEMPTING"),
            side_effect=str(transport.get("sideEffect") or SIDE_EFFECT_POSSIBLE),
            retry_action="query_same_command_do_not_resend",
        )
        return build_response(request_id, False, data={"command": journal.command_public_view(row), "transport": transport}, error=err), 2
    except SendRejected as rejected:
        err = build_error(rejected.code, rejected.detail, rejected.side_effect, rejected.retry_action)
        return build_response(request_id, False, error=err), rejected.exit_code


def _handle_interrupt(request: dict[str, Any], journal: Journal) -> tuple[dict[str, Any], int]:
    request_id = str(request["requestId"])
    try:
        runtime_id = request.get("runtimeId")
        expected = request.get("expectedContext")
        reservation_id = request.get("reservationId")
        lease_token = request.get("leaseToken")
        fence = request.get("fence")
        command_id = request.get("commandId")
        interrupt_request_id = request.get("interruptRequestId") or request_id
        reason = request.get("reason")

        for name, value in (
            ("runtimeId", runtime_id),
            ("reservationId", reservation_id),
            ("leaseToken", lease_token),
            ("fence", fence),
            ("commandId", command_id),
        ):
            if not isinstance(value, str) or not value:
                raise SendRejected("INVALID_ARGUMENT", f"{name} is required")
        if expected is not None and not isinstance(expected, dict):
            raise SendRejected("INVALID_ARGUMENT", "expectedContext must be an object")
        if reason is not None and not isinstance(reason, str):
            raise SendRejected("INVALID_ARGUMENT", "reason must be a string")
        if not isinstance(interrupt_request_id, str) or not interrupt_request_id:
            raise SendRejected("INVALID_ARGUMENT", "interruptRequestId is required")

        pane_id = None
        if isinstance(expected, dict):
            pane_id = expected.get("paneId")
        if not isinstance(pane_id, str) or not pane_id:
            raise SendRejected("INVALID_ARGUMENT", "expectedContext.paneId is required for interrupt")

        parts = _resolve_scope_parts(request)
        if parts is None:
            raise SendRejected("INVALID_ARGUMENT", "socketPath is required")
        host_key, uid, socket_path = parts

        try:
            _writer_permit_on_journal(
                journal,
                runtime_id=runtime_id,
                socket_path=socket_path,
                mode="MANAGED",
                reservation_id=reservation_id,
                lease_token=lease_token,
                fence=fence,
            )
        except WriterDenied as denied:
            raise SendRejected(denied.code, denied.detail, side_effect=denied.side_effect, retry_action=denied.retry_action) from denied

        command = journal.get_command(command_id)
        if command is None:
            raise SendRejected("INVALID_ARGUMENT", "command not found")
        if str(command["runtime_id"]) != runtime_id:
            raise SendRejected("MISMATCH", "command runtimeId mismatch")

        if journal.has_final(command_id):
            journal.add_receipt(
                command_id=command_id,
                request_id=request_id,
                kind="ALREADY_FINAL",
                stage=str(command["stage"]),
                side_effect=SIDE_EFFECT_NONE,
                payload={"interruptRequestId": interrupt_request_id},
            )
            data = {
                "command": journal.command_public_view(command),
                "alreadyFinal": True,
                "inputPerformed": False,
            }
            return build_response(request_id, True, data=data), 0

        if command["interrupt_intent_at"]:
            data = {
                "command": journal.command_public_view(command),
                "interruptAlreadyRecorded": True,
                "inputPerformed": False,
            }
            return build_response(request_id, True, data=data), 0

        stamp = observed_at_now()
        row = journal.update_command(
            command_id,
            stage="CANCEL_REQUESTED",
            interrupt_intent_at=stamp,
        )
        journal.add_receipt(
            command_id=command_id,
            request_id=request_id,
            kind="INTERRUPT_INTENT",
            stage="CANCEL_REQUESTED",
            side_effect=SIDE_EFFECT_NONE,
            payload={"interruptRequestId": interrupt_request_id, "reason": reason},
        )
        _commit_durable(journal)

        hook = make_writer_pre_send_hook(
            runtime_id=runtime_id,
            socket_path=socket_path,
            mode="MANAGED",
            reservation_id=reservation_id,
            lease_token=lease_token,
            fence=fence,
            host_key=host_key,
            uid=uid,
        )
        try:
            transport = transport_interrupt(pane_id, socket_path=socket_path, pre_send_hook=hook)
        except Exception as exc:
            transport = {
                "ok": False,
                "error": str(exc),
                "sideEffect": SIDE_EFFECT_POSSIBLE,
                "deliveryDisposition": "DELIVERY_AMBIGUOUS",
            }

        _ensure_txn(journal)
        journal.add_receipt(
            command_id=command_id,
            request_id=request_id,
            kind="INTERRUPT_SENT" if transport.get("ok") else "INTERRUPT_TRANSPORT_RESULT",
            stage="CANCEL_REQUESTED",
            side_effect=str(transport.get("sideEffect") or SIDE_EFFECT_OBSERVED),
            payload={"transport": transport, "interruptRequestId": interrupt_request_id},
        )
        row = journal.get_command(command_id)
        assert row is not None
        data = {
            "command": journal.command_public_view(row),
            "transport": transport,
            "inputPerformed": bool(transport.get("ok")),
        }
        if transport.get("ok"):
            return build_response(request_id, True, data=data), 0
        err = build_error(
            "DELIVERY_AMBIGUOUS",
            str(transport.get("error") or "interrupt transport failed"),
            side_effect=str(transport.get("sideEffect") or SIDE_EFFECT_POSSIBLE),
            retry_action="query_same_command_do_not_resend",
        )
        return build_response(request_id, False, data=data, error=err), 2
    except SendRejected as rejected:
        err = build_error(rejected.code, rejected.detail, rejected.side_effect, rejected.retry_action)
        return build_response(request_id, False, error=err), rejected.exit_code


def _agent_pid_for_collect_enrich(
    *,
    expected: dict[str, Any] | None,
    pane_id: str,
    socket_path: str | None,
    agent_kind: str | None = "codex",
) -> int | None:
    """Resolve live agent pid for BOOTSTRAP path enrich.

    Prefer explicit expected.agentPid, else observe_candidates on the Managed socket.
    Never fall back to default-tmux pane_field alone (proof socket ≠ default server).
    """
    if isinstance(expected, dict):
        raw = expected.get("agentPid")
        if raw is None and isinstance(expected.get("identityEvidence"), dict):
            raw = expected["identityEvidence"].get("agentPid")
        if raw is not None:
            try:
                return int(raw)
            except (TypeError, ValueError):
                pass
    if not socket_path:
        return None
    try:
        for cand in observe_candidates(socket_path, agent_kind):
            evidence = cand.get("identityEvidence") if isinstance(cand, dict) else None
            if not isinstance(evidence, dict):
                continue
            if evidence.get("paneId") != pane_id:
                continue
            raw = evidence.get("agentPid")
            if raw is None:
                continue
            try:
                return int(raw)
            except (TypeError, ValueError):
                continue
    except Exception:
        return None
    return None


def _enrich_collect_cursor(
    cursor: dict[str, Any],
    *,
    expected: dict[str, Any] | None,
    journal: Journal,
    command: Any,
    socket_path: str | None = None,
    agent_kind: str | None = None,
) -> dict[str, Any]:
    """Ensure collect cursor has a transcript/rollout path when possible.

    Live reserve freezes ``{"kind":"BOOTSTRAP","runtimeId":...}`` with no path
    because no session file exists yet. Collect must not fail with
    INVALID_ARGUMENT for that case: either resolve the live path and
    persist it, or leave path absent so the caller returns RESULT_NOT_FINAL.

    Path resolve must use the Managed socket's live agentPid — never default
    tmux pane_field alone (Run 4: proof socket %0 ≠ default-server %0).
    """
    if cursor.get("path"):
        return cursor

    profile_root: str | None = None
    pane_id: str | None = None
    workspace: str | None = None
    kind = agent_kind
    if isinstance(expected, dict):
        if isinstance(expected.get("profileRoot"), str):
            profile_root = expected["profileRoot"]
        if isinstance(expected.get("paneId"), str):
            pane_id = expected["paneId"]
        if isinstance(expected.get("workspaceRoot"), str):
            workspace = expected["workspaceRoot"]
        if kind is None and isinstance(expected.get("agentKind"), str):
            kind = expected["agentKind"]
    if profile_root is None or pane_id is None or workspace is None or kind is None:
        try:
            reservation = journal.reservation_summary(str(command["runtime_id"]))
            ctx = (reservation or {}).get("context") if reservation else None
            if isinstance(ctx, dict):
                if profile_root is None and isinstance(ctx.get("profileRoot"), str):
                    profile_root = ctx["profileRoot"]
                if pane_id is None and isinstance(ctx.get("paneId"), str):
                    pane_id = ctx["paneId"]
                if workspace is None and isinstance(ctx.get("workspaceRoot"), str):
                    workspace = ctx["workspaceRoot"]
                if kind is None and isinstance(ctx.get("agentKind"), str):
                    kind = ctx["agentKind"]
        except Exception:
            pass

    if not profile_root or not pane_id:
        return cursor

    agent_pid = _agent_pid_for_collect_enrich(
        expected=expected if isinstance(expected, dict) else None,
        pane_id=pane_id,
        socket_path=socket_path,
        agent_kind=kind if isinstance(kind, str) else "codex",
    )

    enriched_path: str | None = None
    enriched_session: str | None = None
    try:
        if kind in {"claude-pro", "claude-team"}:
            if not workspace:
                return cursor
            from actl.agents import claude as claude_agent

            managed = claude_agent.resolve_claude_managed(Path(profile_root), workspace, agent_pid)
            if managed.code == "OK" and managed.transcript_path is not None:
                enriched_path = str(managed.transcript_path)
                enriched_session = managed.session_id
            else:
                resolution = claude_agent.resolve_claude(Path(profile_root), workspace, agent_pid)
                if resolution.confidence == "exact" and resolution.transcript is not None:
                    enriched_path = str(resolution.transcript)
                    enriched_session = resolution.session_id
        elif kind == "grok":
            from actl.agents import grok as grok_agent

            managed = grok_agent.resolve_grok_managed(
                Path(profile_root), pane_id, agent_pid, workspace=workspace
            )
            if managed.code == "OK" and managed.chat_history_path is not None:
                enriched_path = str(managed.chat_history_path)
                enriched_session = managed.session_id
            else:
                resolution = grok_agent.resolve_grok(
                    Path(profile_root), pane_id, agent_pid, workspace=workspace
                )
                if resolution.confidence == "exact" and resolution.session_dir is not None:
                    history = resolution.session_dir / "chat_history.jsonl"
                    if history.is_file():
                        enriched_path = str(history)
                        enriched_session = resolution.session_id
        else:
            from actl.agents import codex as codex_agent

            resolution = codex_agent.resolve_codex(Path(profile_root), pane_id, codex_pid=agent_pid)
            if resolution is not None and resolution.rollout_path is not None:
                # Accept "exact" only for Managed binding; avoid newest-file guesses.
                if getattr(resolution, "confidence", None) == "exact":
                    enriched_path = str(resolution.rollout_path)
                    enriched_session = resolution.session_id
    except Exception:
        return cursor

    if not enriched_path:
        return cursor

    enriched = dict(cursor)
    enriched["path"] = enriched_path
    enriched.setdefault("byteOffset", 0)
    if enriched_session:
        enriched["sessionId"] = enriched_session
    # Persist so subsequent collect/watch do not re-resolve every time.
    try:
        journal.conn.execute(
            "UPDATE commands SET cursor_json = ?, updated_at = ? WHERE command_id = ?",
            (
                json.dumps(enriched, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
                observed_at_now(),
                str(command["command_id"]),
            ),
        )
    except Exception:
        pass
    return enriched


def _handle_collect(request: dict[str, Any], journal: Journal) -> tuple[dict[str, Any], int]:
    request_id = str(request["requestId"])
    try:
        command_id = request.get("commandId")
        runtime_id = request.get("runtimeId")
        expected = request.get("expectedContext")
        known_result_id = request.get("resultId")
        if not isinstance(command_id, str) or not command_id:
            raise SendRejected("INVALID_ARGUMENT", "commandId is required")
        if runtime_id is not None and not isinstance(runtime_id, str):
            raise SendRejected("INVALID_ARGUMENT", "runtimeId must be a string")
        if expected is not None and not isinstance(expected, dict):
            raise SendRejected("INVALID_ARGUMENT", "expectedContext must be an object")
        if known_result_id is not None and not isinstance(known_result_id, str):
            raise SendRejected("INVALID_ARGUMENT", "resultId must be a string")

        command = journal.get_command(command_id)
        if command is None:
            raise SendRejected("INVALID_ARGUMENT", "command not found")
        if runtime_id and str(command["runtime_id"]) != runtime_id:
            raise SendRejected("MISMATCH", "command runtimeId mismatch")

        existing_packet = journal.get_final_packet(command_id)
        if existing_packet is not None:
            if known_result_id and existing_packet.get("resultId") != known_result_id:
                raise SendRejected("CONFLICT", "resultId does not match stored FINAL")
            data = {
                "command": journal.command_public_view(command),
                "final": existing_packet,
                "replayed": True,
            }
            return build_response(request_id, True, data=data), 0

        agent_kind = None
        if isinstance(expected, dict):
            agent_kind = expected.get("agentKind")
        if agent_kind is None:
            # Fall back to reservation frozen context.
            reservation = journal.reservation_summary(str(command["runtime_id"]))
            ctx = (reservation or {}).get("context") if reservation else None
            if isinstance(ctx, dict):
                agent_kind = ctx.get("agentKind")
        if agent_kind not in {"codex", "claude-pro", "claude-team", "grok"}:
            raise SendRejected("UNSUPPORTED", f"managed.collect.final unsupported for agentKind={agent_kind!r}")

        stage = str(command["stage"])
        if stage == "DELIVERY_AMBIGUOUS" and not command["session_id"]:
            err = build_error(
                "DELIVERY_AMBIGUOUS",
                "transport ambiguous and no agent turn bound yet",
                side_effect=SIDE_EFFECT_POSSIBLE,
                retry_action="query_same_command_do_not_resend",
            )
            return build_response(request_id, False, data={"command": journal.command_public_view(command)}, error=err), 2

        wire_prompt = command["wire_prompt"]
        prompt_sha = command["wire_prompt_sha256"]
        if not isinstance(wire_prompt, str) or not isinstance(prompt_sha, str):
            raise SendRejected("INVALID_ARGUMENT", "command missing wirePrompt")
        cursor_raw = command["cursor_json"]
        if not cursor_raw:
            raise SendRejected("RESULT_NOT_FINAL", "command has no observationCursor")
        try:
            cursor = json.loads(cursor_raw) if isinstance(cursor_raw, str) else cursor_raw
        except json.JSONDecodeError as exc:
            raise SendRejected("INVALID_ARGUMENT", "cursor_json is not valid JSON") from exc
        if not isinstance(cursor, dict):
            raise SendRejected("INVALID_ARGUMENT", "observationCursor must be an object")
        # Live reserve freezes BOOTSTRAP without path (session file does not exist yet).
        # Resolve path on collect when possible; otherwise RESULT_NOT_FINAL — never
        # INVALID_ARGUMENT for the known BOOTSTRAP-missing-path case (Run 3 hold).
        socket_path: str | None = None
        try:
            parts = _resolve_scope_parts(request)
            if parts is not None:
                socket_path = parts[2]
        except Exception:
            socket_path = None
        cursor = _enrich_collect_cursor(
            cursor,
            expected=expected if isinstance(expected, dict) else None,
            journal=journal,
            command=command,
            socket_path=socket_path,
            agent_kind=agent_kind if isinstance(agent_kind, str) else None,
        )
        if not cursor.get("path"):
            raise SendRejected(
                "RESULT_NOT_FINAL",
                "BOOTSTRAP observationCursor has no path yet; session rollout not resolved",
                retry_action="retry_collect_after_session_appears",
            )

        from actl.agents import claude as claude_agent
        from actl.agents import codex as codex_agent
        from actl.agents import grok as grok_agent

        path = Path(str(cursor["path"]))
        cancel_requested = stage == "CANCEL_REQUESTED" or bool(command["interrupt_intent_at"])
        # Prefer command binding, else enriched cursor sessionId.
        bound_session = None
        if command["session_id"]:
            bound_session = str(command["session_id"])
        elif isinstance(cursor.get("sessionId"), str) and cursor["sessionId"]:
            bound_session = str(cursor["sessionId"])
        collect_kwargs = {
            "path": path,
            "cursor": cursor,
            "wire_prompt": wire_prompt,
            "command_id": command_id,
            "runtime_id": str(command["runtime_id"]),
            "prompt_sha256": prompt_sha,
            "session_id": bound_session,
            "cancel_requested": cancel_requested,
        }
        if agent_kind in {"claude-pro", "claude-team"}:
            result = claude_agent.collect_claude_final(**collect_kwargs)
        elif agent_kind == "grok":
            result = grok_agent.collect_grok_final(**collect_kwargs)
        else:
            result = codex_agent.collect_codex_final(**collect_kwargs)
        if result.code == "FINAL" and result.packet is not None:
            packet = result.packet
            # Immutable commit — never overwrite an existing FINAL.
            if journal.get_final_packet(command_id) is not None:
                packet = journal.get_final_packet(command_id)
                assert packet is not None
            else:
                journal.update_command(
                    command_id,
                    stage="FINAL",
                    result_id=str(packet["resultId"]),
                )
                # Persist session/turn binding on the command row.
                journal.conn.execute(
                    "UPDATE commands SET session_id = ?, turn_id = ?, updated_at = ? WHERE command_id = ?",
                    (packet.get("sessionId"), packet.get("turnId"), observed_at_now(), command_id),
                )
                journal.add_receipt(
                    command_id=command_id,
                    request_id=request_id,
                    kind="FINAL",
                    stage="FINAL",
                    side_effect=SIDE_EFFECT_NONE,
                    payload=packet,
                )
            row = journal.get_command(command_id)
            assert row is not None
            return build_response(request_id, True, data={"command": journal.command_public_view(row), "final": packet}), 0

        if result.code == "AMBIGUOUS_SESSION":
            err = build_error("AMBIGUOUS_SESSION", result.detail, retry_action="rediscover")
            return build_response(request_id, False, error=err), 2
        if result.code == "RESULT_NOT_FINAL":
            # Optionally promote AGENT_RECEIVED when bind succeeded but final not ready.
            if agent_kind in {"claude-pro", "claude-team"}:
                forward = claude_agent.read_claude_jsonl_forward(path, cursor)
                bind_ok = False
                binding_session = binding_turn = None
                if forward.code == "OK":
                    binding = claude_agent.bind_claude_user_turn(
                        cursor, wire_prompt, forward.events, session_id=bound_session
                    )
                    bind_ok = binding.code == "OK"
                    binding_session, binding_turn = binding.session_id, binding.turn_id
            elif agent_kind == "grok":
                forward = grok_agent.read_grok_jsonl_forward(path, cursor)
                bind_ok = False
                binding_session = binding_turn = None
                if forward.code == "OK":
                    binding = grok_agent.bind_grok_user_turn(
                        cursor, wire_prompt, forward.events, session_id=bound_session
                    )
                    bind_ok = binding.code == "OK"
                    binding_session, binding_turn = binding.session_id, binding.turn_id
            else:
                forward = codex_agent.read_jsonl_forward(path, cursor)
                bind_ok = False
                binding_session = binding_turn = None
                if forward.code == "OK":
                    binding = codex_agent.bind_user_turn(cursor, wire_prompt, forward.events)
                    bind_ok = binding.code == "OK"
                    binding_session, binding_turn = binding.session_id, binding.turn_id
            if bind_ok and stage not in {"AGENT_RECEIVED", "FINAL", "CANCEL_REQUESTED"}:
                journal.update_command(command_id, stage="AGENT_RECEIVED")
                journal.conn.execute(
                    "UPDATE commands SET session_id = ?, turn_id = ?, updated_at = ? WHERE command_id = ?",
                    (binding_session, binding_turn, observed_at_now(), command_id),
                )
                journal.add_receipt(
                    command_id=command_id,
                    request_id=request_id,
                    kind="AGENT_RECEIVED",
                    stage="AGENT_RECEIVED",
                    side_effect=SIDE_EFFECT_OBSERVED,
                    payload={"sessionId": binding_session, "turnId": binding_turn},
                )
            err = build_error("RESULT_NOT_FINAL", result.detail, retry_action="bounded_collect_poll")
            row = journal.get_command(command_id)
            assert row is not None
            return build_response(request_id, False, data={"command": journal.command_public_view(row)}, error=err), 2

        raise SendRejected(result.code, result.detail)
    except SendRejected as rejected:
        err = build_error(rejected.code, rejected.detail, rejected.side_effect, rejected.retry_action)
        return build_response(request_id, False, error=err), rejected.exit_code


def _dispatch_operation(request: dict[str, Any], journal: Journal) -> tuple[dict[str, Any], int]:
    operation = str(request["operation"])
    if operation == "reserve":
        return _handle_reserve(request, journal)
    if operation == "discover":
        return _handle_discover(request, journal)
    if operation == "status":
        return _handle_status(request, journal)
    if operation == "send":
        return _handle_send(request, journal)
    if operation == "interrupt":
        return _handle_interrupt(request, journal)
    if operation == "collect":
        return _handle_collect(request, journal)
    err = build_error("INVALID_ARGUMENT", f"unknown operation: {operation}")
    return build_response(str(request["requestId"]), False, error=err), 3


def handle_runtime_request(request: dict[str, Any] | Any) -> tuple[dict[str, Any], int]:
    validated, early, early_exit = _validate_envelope(request)
    if validated is None:
        assert early is not None
        return early, early_exit

    request_id = str(validated["requestId"])
    operation = str(validated["operation"])
    body_sha = sha256_hex(canonical_json(validated))

    if operation in _UNSUPPORTED_OPS:
        err = build_error("UNSUPPORTED", f"operation {operation} not implemented yet")
        return build_response(request_id, False, error=err), 2

    if operation not in _JOURNAL_OPS:
        err = build_error("INVALID_ARGUMENT", f"unknown operation: {operation}")
        return build_response(request_id, False, error=err), 3

    try:
        path = _journal_path_from_request(validated)
        with open_journal(path) as journal:
            journal.conn.execute("BEGIN IMMEDIATE")
            try:
                existing = journal.get_idempotency(request_id)
                if existing is not None:
                    prev_sha, prev_json, prev_exit = existing
                    if prev_sha != body_sha:
                        err = build_error("CONFLICT", "requestId reused with different body")
                        response = build_response(request_id, False, error=err)
                        journal.conn.execute("COMMIT")
                        return response, 3
                    journal.conn.execute("COMMIT")
                    return json.loads(prev_json), prev_exit

                response, code = _dispatch_operation(validated, journal)
                journal.put_idempotency(request_id, body_sha, response, code)
                journal.conn.execute("COMMIT")
                return response, code
            except Exception:
                try:
                    journal.conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
    except JournalError as exc:
        err = build_error("UNSUPPORTED", f"journal unavailable: {exc}", retry_action="repair_journal")
        return build_response(request_id, False, error=err), 2
    except sqlite3.Error as exc:
        err = build_error("UNSUPPORTED", f"journal error: {exc}", retry_action="repair_journal")
        return build_response(request_id, False, error=err), 2
