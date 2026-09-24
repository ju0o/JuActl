from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import sys
from pathlib import Path

from actl.agents.extract import extract_last_response
from actl.core.config import CONFIG_PATH, backup_config, ensure_config, get_target, load_config, migration_warning, save_config
from actl.core.activity import observe_activity
from actl.core.input import read_event
from actl.core.discovery import STRONG_CONFIDENCE, Detection, auto_bind_opencode_session, discover, manual_map, mapping_state, reconcile
from actl.core.registry import AGENTS, resolve_agent
from actl.core.probe import probe_agent
from actl.core.status import agent_status
from actl.core.runtime import WriterDenied
from actl.core.tmux import TmuxError, capture_pane, pane_field, send_keys, send_prompt, target_exists
from actl.core.validation import pane_processes, require_valid_target, validate_target
from actl.utils.clipboard import copy_text

VERSION = "0.1.1-managed"

BANNER = "Agent Control"


def _unknown_agent(value: str) -> str:
    return f"Unknown agent: {value} — 쓸 수 있는 이름: {', '.join(AGENTS)}"


def _activity_label(target: str) -> str:
    try:
        state, _ = observe_activity(target)
    except Exception:
        state = "UNKNOWN"
    return {"RUNNING": "작업 중", "IDLE": "대기"}.get(state, "미확인")


def _menu() -> None:
    print(BANNER)
    print()
    for idx, spec in enumerate(AGENTS.values(), 1):
        print(f"[{idx}] {spec.display_name}")
    print()


def _resolve_selection(value: str) -> str | None:
    value = value.strip()
    if value.isdigit():
        idx = int(value) - 1
        names = list(AGENTS)
        if 0 <= idx < len(names):
            return names[idx]
    return resolve_agent(value)


def _print_status(config: dict, agent: str | None = None, *, json_output: bool = False) -> None:
    names = [agent] if agent else list(AGENTS)
    rows: list[dict] = []
    for name in names:
        try:
            s = agent_status(config, name)
            rows.append({"id": name, **s})
            if not json_output:
                activity = _activity_label(s["target"]) if s.get("pane") == "UP" else "미확인"
                print(f"{s['agent']:<12} {s['pane']:<4} {activity:<4} {s['target']:<14} cmd={s['command']:<14} path={s['path']}")
        except Exception as exc:
            row = {"id": name, "agent": AGENTS[name].display_name, "state": "ERROR", "detail": str(exc)}
            rows.append(row)
            if not json_output:
                print(f"{AGENTS[name].display_name:<12} ERROR {exc}")
    if json_output:
        print(json.dumps(rows, ensure_ascii=False, separators=(",", ":")))


def _doctor(*, json_output: bool = False) -> int:
    """상용화 자가진단: python/tmux/ssh/클립보드/매핑 상태를 한 번에 출력."""
    import shutil
    import sys as _sys

    ok = True
    checks: list[dict[str, object]] = []

    def console_safe(value: str) -> str:
        encoding = getattr(_sys.stdout, "encoding", None) or "ascii"
        try:
            value.encode(encoding)
            return value
        except (LookupError, UnicodeEncodeError):
            return value.encode(encoding, errors="replace").decode(encoding, errors="replace")

    def line(name: str, good: bool, detail: str = "") -> None:
        nonlocal ok
        if not good:
            ok = False
        checks.append({"name": name, "ok": good, "detail": detail})
        if json_output:
            return
        mark = "✓" if good else "✗"
        mark = "✓" if good else "✗"
        line_text = f"{mark} {name}" + (f" — {detail}" if detail else "")
        print(console_safe(line_text))

    def note(name: str, detail: str) -> None:
        checks.append({"name": name, "ok": None, "detail": detail})
        if not json_output:
            print(console_safe(f"- {name} — {detail}"))

    line("python", _sys.version_info >= (3, 10), _sys.version.split()[0])
    from actl.core.registry import registry_issues

    registry_errors = registry_issues()
    line("adapter registry", not registry_errors, ", ".join(registry_errors) or f"{len(AGENTS)} agents")
    remote = "--ssh" in _sys.argv
    if _sys.platform == "win32":
        line("tmux", True, "MainPC에서는 로컬 tmux 불필요")
        line("ssh", shutil.which("ssh") is not None)
        if remote:
            line("tmux 서버", True, "asus 원격")
        else:
            note("tmux 서버", "원격 확인은 --ssh asus 사용")
    else:
        line("tmux", shutil.which("tmux") is not None)
        line("ssh", shutil.which("ssh") is not None)
        try:
            from actl.core.tmux import list_panes

            panes = list_panes()
            line("tmux 서버", True, f"{len(panes)} panes")
        except Exception as exc:
            line("tmux 서버", False, str(exc)[:100])
    if _sys.platform == "win32":
        line("로컬 클립보드", True, "Windows Set-Clipboard")
    else:
        local_backend = next(
            (name for name in ("wl-copy", "xclip", "xsel") if shutil.which(name)),
            None,
        )
        line("로컬 클립보드", local_backend is not None, local_backend or "OSC52/수동 복사 사용")
    if _sys.platform == "win32" and not remote:
        note("live 매핑", "원격 확인은 actl doctor --ssh asus")
    else:
        try:
            config = load_config()
            from actl.core.validation import validate_target

            live = sum(
                1
                for a, e in config.get("agents", {}).items()
                if isinstance(e, dict) and e.get("target") and validate_target(a, e["target"]).valid
            )
            line("live 매핑", True, f"{live}/{len(AGENTS)} agents")
        except Exception as exc:
            line("live 매핑", False, str(exc)[:100])
    if json_output:
        # JSON is often piped through Windows PowerShell's legacy cp1252 stream;
        # ASCII escapes keep the machine contract lossless on every console.
        print(json.dumps({"ok": ok, "checks": checks}, ensure_ascii=True, separators=(",", ":")))
    else:
        print("OK" if ok else "일부 항목 확인 필요 (위 ✗ 참조)")
    return 0 if ok else 1


def _audit(limit: int = 50) -> int:
    from actl.core.audit import read

    for row in read(limit):
        print(json.dumps(row, ensure_ascii=False, separators=(",", ":")))
    return 0


def _history(agent: str | None = None, limit: int = 50) -> int:
    from actl.core.audit import read

    if limit < 1:
        return 0
    rows = [row for row in read(max(100, limit * 4))
            if row.get("event") == "copy" and row.get("ok") is True and row.get("result_hash")]
    if agent:
        rows = [row for row in rows if row.get("agent") == agent]
    for row in rows[-limit:]:
        print(json.dumps(row, ensure_ascii=False, separators=(",", ":")))
    return 0


def _push_file(path: str, *, print_only: bool = False) -> int:
    """Push a local file to the MainPC side over the current SSH session.

    Two transports, no MainPC sshd setup required:
    - default: base64 payload delimited by BEGIN/END lines for the MainPC
      receiver (PowerShell snippet in MAINPC_SETUP.md §10).
    - --print: raw file bytes to stdout for manual copy/paste.
    """
    import base64
    import hashlib

    src = Path(path).expanduser()
    if not src.is_file():
        print(f"✗ Not a file: {path}")
        return 1
    data = src.read_bytes()
    if print_only:
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()
        return 0
    digest = hashlib.sha256(data).hexdigest()[:12]
    b64 = base64.b64encode(data).decode("ascii")
    print(f"ACTL_PUSH_BEGIN {src.name} {len(data)} {digest}")
    for i in range(0, len(b64), 76):
        print(b64[i : i + 76])
    print(f"ACTL_PUSH_END {digest}")
    print(f"→ On MainPC: save the block between BEGIN/END, then decode (see MAINPC_SETUP.md §10).", file=sys.stderr)
    return 0


def _extract(config: dict, agent: str, target: str | None) -> int:
    """Machine pipe: print extracted last-response text to stdout only.

    No clipboard, no reconcile, no prompts. Used by MainPC remote mode:
    ``ssh asus actl extract <agent> <pane>``. Target falls back to the
    stored mapping when omitted. Detail goes to stderr, exit 1 on empty.
    """
    from actl.core.config import get_target as _get_target

    pane = target
    if not pane:
        try:
            pane = _get_target(config, agent).target
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1
    result = extract_last_response(agent, pane, config)
    if not result.text:
        print(result.detail or "No response text found", file=sys.stderr)
        return 1
    sys.stdout.write(result.text)
    sys.stdout.flush()
    return 0


def _copy(config: dict, agent: str, *, print_only: bool = False) -> int:
    """Extract + deliver the last response. Returns 0 on delivery/print success."""
    try:
        target = _resolve_live_target(config, agent)
    except ValueError as exc:
        print(f"✗ {exc}")
        return 1
    result = extract_last_response(agent, target, config)
    if not result.text:
        from actl.core.audit import record

        record("copy", agent=agent, target=target, ok=False, source=result.source,
               confidence=result.confidence, detail=result.detail)
        print("아직 새 답이 없어요 — 작업이 끝나면 다시 해 보세요")
        return 1
    if print_only:
        # Safe manual-copy fallback — prints exact extracted Result verbatim.
        # Deliberately no backend prefix so selection is clean.
        print(result.text)
        from actl.core.audit import record

        record("copy", agent=agent, target=target, ok=True, mode="print", source=result.source,
               confidence=result.confidence, chars=len(result.text),
               result_hash=hashlib.sha256(result.text.encode("utf-8")).hexdigest()[:16])
        from actl.core.state import acknowledge

        acknowledge(agent, hashlib.sha256(result.text.encode("utf-8")).hexdigest()[:16])
        return 0
    preferred = config.get("clipboard_backend", "auto")
    try:
        backend = copy_text(result.text, preferred=preferred)
    except Exception as exc:
        from actl.core.audit import record

        record("copy", agent=agent, target=target, ok=False, mode=preferred,
               source=result.source, confidence=result.confidence, error=type(exc).__name__)
        # Clipboard transport failed — offer manual fallback.
        print(f"✗ Clipboard delivery failed: {exc}")
        print("  Use /copy --print or /result for manual copy.")
        return 1
    note = f" [{result.source}, confidence={result.confidence}]" if result.confidence not in {"high", "exact"} else ""
    if backend == "osc52":
        # OSC52 is best-effort: host cannot verify the Windows terminal actually
        # accepted the sequence, so never claim guaranteed clipboard success.
        print(f"터미널 클립보드로 보냈어요 (안 붙여지면 actl copy {agent} --print)")
    else:
        print(f"✓ Last response copied via {backend}{note}")
    from actl.core.audit import record

    record("copy", agent=agent, target=target, ok=True, mode=backend,
           source=result.source, confidence=result.confidence, chars=len(result.text),
           result_hash=hashlib.sha256(result.text.encode("utf-8")).hexdigest()[:16])
    from actl.core.state import acknowledge

    acknowledge(agent, hashlib.sha256(result.text.encode("utf-8")).hexdigest()[:16])
    return 0


def _send_to_selected(config: dict, agent: str, prompt: str, *, target: str | None = None) -> str:
    """Resolve on every send; never retain a target across /switch."""
    from actl.core.remote import is_remote, remote_send

    if target is not None:
        validation = validate_target(agent, target)
        if not validation.valid:
            raise ValueError(
                f"Selected runtime is {validation.state}; sending blocked: {validation.detail}"
            )
        try:
            if is_remote():
                from actl.core.remote import ManagedUnsupported, remote_managed_send

                try:
                    delivery = remote_managed_send(agent, target, prompt)
                    return str(delivery)
                except ManagedUnsupported:
                    pass
            send_prompt(target, prompt)
        except WriterDenied as denied:
            from actl.core.audit import record

            record("send", agent=agent, target=target, ok=False, chars=len(prompt), error=denied.code)
            raise RuntimeError(f"{denied.code}: {denied.detail}") from denied
        except Exception as exc:
            from actl.core.audit import record

            record("send", agent=agent, target=target, ok=False, chars=len(prompt), error=type(exc).__name__)
            raise
        from actl.core.audit import record

        record("send", agent=agent, target=target, ok=True, chars=len(prompt))
        return target

    if is_remote():
        try:
            target = remote_send(agent, prompt)
        except Exception as exc:
            from actl.core.audit import record

            record("send", agent=agent, remote=True, ok=False, chars=len(prompt), error=type(exc).__name__)
            raise
        from actl.core.audit import record

        record("send", agent=agent, target=target, remote=True, ok=True, chars=len(prompt))
        return target
    target = _resolve_live_target(config, agent)
    try:
        send_prompt(target, prompt)
    except WriterDenied as denied:
        from actl.core.audit import record

        record("send", agent=agent, target=target, ok=False, chars=len(prompt), error=denied.code)
        raise RuntimeError(f"{denied.code}: {denied.detail}") from denied
    except Exception as exc:
        from actl.core.audit import record

        record("send", agent=agent, target=target, ok=False, chars=len(prompt), error=type(exc).__name__)
        raise
    from actl.core.audit import record

    record("send", agent=agent, target=target, ok=True, chars=len(prompt))
    return target


def _debug(config: dict, agent: str) -> None:
    """Read-only selected-agent resolution trace; never sends a key."""
    spec = AGENTS[agent]
    try:
        target = get_target(config, agent).target
    except ValueError as exc:
        print(f"selected canonical agent: {agent}\nselected display name: {spec.display_name}\nresolved target: UNMAPPED\nreason: {exc}")
        return
    print(f"selected canonical agent: {agent}")
    print(f"selected display name: {spec.display_name}")
    print(f"selected config key: {agent}")
    print(f"resolved target: {target}")
    print(f"copy extractor: {'claude-project-final-text' if agent.startswith('claude-') else agent}")
    print(f"copy storage root: {spec.data_dirs[0] if spec.data_dirs else '-'}")
    validation = validate_target(agent, target)
    print(f"target validation: {validation.state}")
    if validation.detail:
        print(f"validation detail: {validation.detail}")
    if not validation.valid:
        return
    print(f"resolved pane id: {validation.pane_id}")
    print(f"pane current command: {validation.command}")
    pane_path = validation.path
    print(f"pane current path: {pane_path}")
    if agent == "opencode":
        from actl.agents.opencode import resolve_opencode, session_row, stored_session_id

        expected = stored_session_id(config)
        resolution = resolve_opencode(spec.data_dirs[0], target, validation.agent_pid, expected)
        print(f"canonical: {agent}")
        print(f"target: {target}")
        print(f"pane cwd: {pane_path}")
        print(f"bound session id: {expected or '-'}")
        print(f"live cmdline session id: {resolution.live_session_id or '-'}")
        print(f"active session id: {resolution.session_id or '-'}")
        print(f"session row: {session_row(spec.data_dirs[0], expected) is not None if expected else '-'}")
        print(f"match method: {resolution.match_method}")
        print(f"copy confidence: {resolution.confidence}")
        if resolution.detail:
            print(f"copy detail: {resolution.detail}")
        return
    if agent == "cursor":
        from actl.agents.cursor import resolve_cursor
        resolution = resolve_cursor(spec.data_dirs[0], target, validation.agent_pid)
        print(f"cursor pid: {validation.agent_pid or '-'}")
        print(f"workspace/session id: {resolution.session_id or '-'}")
        print(f"matched chat directory: {resolution.chat_dir or '-'}")
        print(f"matched store.db: {resolution.store_db or '-'}")
        print(f"match method: {resolution.match_method}")
        print(f"copy confidence: {resolution.confidence}")
        if resolution.detail:
            print(f"copy detail: {resolution.detail}")
        return
    if agent == "codex":
        from actl.agents.codex import resolve_codex
        resolution = resolve_codex(spec.data_dirs[0], target, validation.agent_pid)
        print(f"codex pid: {resolution.foreground_pid or validation.agent_pid or '-'}")
        print(f"tty: {resolution.tty or '-'}")
        print(f"session id: {resolution.session_id or '-'}")
        print(f"matched rollout: {resolution.rollout_path or '-'}")
        print(f"match method: {resolution.match_method}")
        print(f"copy confidence: {resolution.confidence}")
        if resolution.detail:
            print(f"copy detail: {resolution.detail}")
        return
    if agent == "cline":
        from actl.agents.cline import resolve_cline
        resolution = resolve_cline(spec.data_dirs[0], target, validation.agent_pid)
        print(f"cline pid: {validation.agent_pid or '-'}")
        print(f"active session id: {resolution.session_id or '-'}")
        print(f"matched messages: {resolution.messages_path or '-'}")
        print(f"match method: {resolution.match_method}")
        print(f"copy confidence: {resolution.confidence}")
        if resolution.detail:
            print(f"copy detail: {resolution.detail}")
        return
    if agent in {"claude-team", "claude-pro"}:
        from actl.agents.claude import resolve_claude
        raw_pane_pid = pane_field(target, "#{pane_pid}")
        try:
            pane_pid = int(raw_pane_pid)
        except ValueError:
            pane_pid = None
        resolution = resolve_claude(spec.data_dirs[0], pane_path, pane_pid)
        print(f"canonical: {agent}")
        print(f"target: {target}")
        print(f"pane cwd: {pane_path}")
        print(f"profile root: {spec.data_dirs[0]}")
        print(f"active session id: {resolution.session_id or '-'}")
        print(f"matched transcript: {resolution.transcript or '-'}")
        print(f"match method: {resolution.match_method}")
        print(f"latest assistant timestamp: {resolution.latest_assistant_timestamp or '-'}")
        print(f"copy confidence: {resolution.confidence}")
        if resolution.detail:
            print(f"copy detail: {resolution.detail}")


def _copy_diagnostic(config: dict, agent: str) -> int:
    """Read-only extraction probe: never calls the clipboard backend."""
    from actl.core.models import CopyResult

    try:
        target = require_valid_target(config, agent)
    except ValueError as exc:
        print(f"canonical: {agent}")
        print("pane_id: -")
        print("pid: -")
        print("tty: -")
        print("cwd: -")
        print("session_id: -")
        print("storage_path: -")
        print("correlation: none")
        print("confidence: none")
        print("source: unresolved")
        print("text_found: NO")
        print(f"failure_stage: mapping-or-validation")
        print(f"detail: {exc}")
        return 1

    spec = AGENTS[agent]
    validation = validate_target(agent, target)
    print(f"canonical: {agent}")
    print(f"pane_id: {validation.pane_id or '-'}")
    print(f"pid: {validation.agent_pid or '-'}")
    print(f"cwd: {validation.path or '-'}")

    result: CopyResult
    failure_stage = "none"
    if agent in {"claude-team", "claude-pro"}:
        from actl.agents.claude import extract_resolved_claude, resolve_claude

        resolution = resolve_claude(spec.data_dirs[0], validation.path, validation.pane_pid) if validation.valid else None
        result = extract_resolved_claude(resolution) if resolution else CopyResult(None, "claude-unresolved", "none", validation.detail)
        print(f"tty: {resolution.tty if resolution and resolution.tty else '-'}")
        print(f"session_id: {resolution.session_id if resolution else '-'}")
        print(f"storage_path: {resolution.transcript if resolution and resolution.transcript else '-'}")
        print(f"correlation: {resolution.match_method if resolution else 'none'}")
        if not resolution or not resolution.session_id:
            failure_stage = "session-correlation"
        elif not resolution.transcript:
            failure_stage = "transcript-binding"
        elif not result.text:
            failure_stage = "completed-assistant-result"
    elif agent == "codex":
        from actl.agents.codex import resolve_codex

        resolution = resolve_codex(spec.data_dirs[0], target, validation.agent_pid) if validation.valid else None
        result = extract_last_response(agent, target, config)
        print(f"tty: {resolution.tty if resolution and resolution.tty else '-'}")
        print(f"session_id: {resolution.session_id if resolution else '-'}")
        print(f"storage_path: {resolution.rollout_path if resolution and resolution.rollout_path else '-'}")
        print(f"correlation: {resolution.match_method if resolution else 'none'}")
        if not resolution or resolution.confidence != "exact":
            failure_stage = "session-correlation"
        elif not result.text:
            failure_stage = "completed-assistant-result"
    elif agent == "cursor":
        from actl.agents.cursor import resolve_cursor

        resolution = resolve_cursor(spec.data_dirs[0], target, validation.agent_pid if validation.valid else None)
        result = extract_last_response(agent, target, config)
        print("tty: -")
        print(f"session_id: {resolution.session_id or '-'}")
        print(f"storage_path: {resolution.store_db or '-'}")
        print(f"correlation: {resolution.match_method}")
        if resolution.confidence != "exact":
            failure_stage = "session-correlation"
        elif not result.text:
            failure_stage = "completed-assistant-result"
    elif agent == "cline":
        from actl.agents.cline import resolve_cline

        resolution = resolve_cline(spec.data_dirs[0], target, validation.agent_pid if validation.valid else None)
        result = extract_last_response(agent, target, config)
        print("tty: -")
        print(f"session_id: {resolution.session_id or '-'}")
        print(f"storage_path: {resolution.messages_path or '-'}")
        print(f"correlation: {resolution.match_method}")
        if resolution.confidence != "exact":
            failure_stage = "session-correlation"
        elif not result.text:
            failure_stage = "completed-assistant-result"
    elif agent == "grok":
        from actl.agents.grok import resolve_grok

        resolution = resolve_grok(spec.data_dirs[0], target)
        result = extract_last_response(agent, target, config)
        print("tty: -")
        print(f"session_id: {resolution.session_id or '-'}")
        print(f"storage_path: {resolution.session_dir or '-'}")
        print(f"correlation: {resolution.match_method}")
        if resolution.confidence != "exact":
            failure_stage = "session-correlation"
        elif not result.text:
            failure_stage = "completed-assistant-result"
    elif agent == "opencode":
        from actl.agents.opencode import resolve_opencode, stored_session_id

        expected = stored_session_id(config)
        resolution = (
            resolve_opencode(spec.data_dirs[0], target, validation.agent_pid, expected)
            if validation.valid
            else resolve_opencode(spec.data_dirs[0], target, None, expected)
        )
        result = extract_last_response(agent, target, config)
        print(f"tty: {resolution.tty or '-'}")
        print(f"session_id: {resolution.session_id or '-'}")
        print(f"live_session_id: {resolution.live_session_id or '-'}")
        print(f"storage_path: {resolution.storage_path or spec.data_dirs[0]}")
        print(f"correlation: {resolution.match_method}")
        if resolution.confidence != "exact":
            failure_stage = "live-session-binding" if not expected else "session-correlation"
        elif not result.text:
            failure_stage = "completed-assistant-result"
    elif agent == "commandcode":
        from actl.agents.commandcode import resolve_commandcode

        resolution = (
            resolve_commandcode(spec.data_dirs[0], target, validation.agent_pid)
            if validation.valid
            else resolve_commandcode(spec.data_dirs[0], target, None)
        )
        result = extract_last_response(agent, target, config)
        print("tty: -")
        print(f"session_id: {resolution.session_id or '-'}")
        print(f"storage_path: {resolution.session_path or spec.data_dirs[0]}")
        print(f"correlation: {resolution.match_method}")
        if resolution.confidence != "exact":
            failure_stage = "session-correlation"
        elif not result.text:
            failure_stage = "completed-assistant-result"
    else:
        result = extract_last_response(agent, target, config)
        print("tty: -")
        print("session_id: -")
        print(f"storage_path: {spec.data_dirs[0] if spec.data_dirs else '-'}")
        print("correlation: none")
        failure_stage = "unsupported"

    print(f"confidence: {result.confidence}")
    print(f"source: {result.source}")
    print(f"text_found: {'YES' if result.text else 'NO'}")
    if result.text:
        print(f"result_length: {len(result.text)}")
        print(f"result_sha256_12: {hashlib.sha256(result.text.encode()).hexdigest()[:12]}")
        print(f"result_tail_sha256_12: {hashlib.sha256(result.text[-80:].encode()).hexdigest()[:12]}")
        failure_stage = "none"
    else:
        print(f"failure_stage: {failure_stage}")
    if result.detail:
        print(f"detail: {result.detail}")
    return 0 if result.text else 1


def _discover(config: dict, detections: list[Detection] | None = None) -> list[Detection]:
    detections = detections if detections is not None else discover()
    print("#  pane_id  cwd                              command          detected      confidence mapping evidence")
    for index, detection in enumerate(detections, 1):
        pane = detection.pane
        detected = detection.agent or "Unknown"
        print(
            f"{index:<2} {pane.pane_id:<8} {pane.current_path:<32} {pane.current_command:<16} "
            f"{detected:<13} {detection.confidence:<10} {mapping_state(config, detection):<8} {detection.evidence}"
        )
    print("\nDiscovery is read-only; config was not modified.")
    return detections


def _discover_json(config: dict, detections: list[Detection] | None = None) -> list[Detection]:
    detections = detections if detections is not None else discover()
    panes = []
    for detection in detections:
        pane = detection.pane
        try:
            tail = [line.strip() for line in capture_pane(pane.pane_id, history=50).splitlines() if line.strip()][-3:]
        except Exception:
            tail = []
        panes.append(
            {
                "paneId": pane.pane_id,
                "cwd": pane.current_path,
                "command": pane.current_command,
                "detected": detection.agent,
                "confidence": detection.confidence,
                "mapping": mapping_state(config, detection),
                "evidence": detection.evidence,
                "tail": tail,
            }
        )
    print(json.dumps({"panes": panes}, ensure_ascii=False))
    return detections


def _apply_discovery(config: dict, detections: list[Detection] | None = None) -> dict:
    detections = detections if detections is not None else discover()
    backup = backup_config()
    updated, changes = reconcile(config, detections)
    if changes:
        save_config(updated)
        print(f"Backup: {backup}")
        print("Mappings changed:")
        print("\n".join(changes))
        return updated
    print(f"Backup: {backup}")
    print("No safe mapping changes detected.")
    return config


def _map(config: dict, agent: str, session_id: str | None = None) -> dict:
    detections = _discover(config)
    raw = input("Choose pane: ").strip()
    detection = None
    if raw.startswith("%"):
        for d in detections:
            if d.pane.pane_id == raw:
                detection = d
                break
        if detection is None:
            print(f"✗ No pane matching {raw}")
            return config
    else:
        try:
            choice = int(raw)
            detection = detections[choice - 1]
        except (ValueError, IndexError):
            print("✗ Invalid pane selection")
            return config
    try:
        updated = manual_map(config, agent, detection)
    except ValueError as exc:
        print(f"✗ {exc}")
        return config
    if agent == "opencode" and session_id:
        bound = _bind_opencode_session(updated, detection.pane.pane_id, session_id)
        if bound is None:
            return config
        updated = bound
    elif agent != "opencode" and session_id:
        print("✗ --session is only supported for opencode")
        return config
    backup = backup_config()
    save_config(updated)
    from actl.core.audit import record

    record("map", agent=agent, target=detection.pane.pane_id, ok=True)
    print(f"Backup: {backup}")
    print(f"✓ {AGENTS[agent].display_name} mapped to {detection.pane.pane_id}")
    return updated


def _confirm(prompt: str) -> bool:
    """Single explicit confirmation; EOF or anything but yes means no."""
    try:
        answer = input(f"{prompt} [Y/n] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return answer in {"", "y", "yes"}


def _pane_opencode_pids(pane_pid: int) -> list[int]:
    from pathlib import Path as _Path

    found = []
    for proc in pane_processes(pane_pid):
        if not proc.args:
            continue
        if _Path(proc.args.split(maxsplit=1)[0]).name == "opencode":
            found.append(proc.pid)
    return found


def _wait_while(predicate, timeout: float, interval: float = 0.5) -> bool:
    import time as _time

    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        if not predicate():
            return True
        _time.sleep(interval)
    return not predicate()


def _wait_live_session(session_id: str, pane_pid: int, timeout: float = 30.0) -> int | None:
    """Poll for exactly one live opencode process proving session_id in cmdline."""
    from actl.agents.opencode import live_cmdline_session

    found: int | None = None

    def settled() -> bool:
        nonlocal found
        matches = [pid for pid in _pane_opencode_pids(pane_pid) if live_cmdline_session(pid) == session_id]
        if len(matches) == 1:
            found = matches[0]
            return True
        found = None
        return False

    import time as _time

    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline and not settled():
        _time.sleep(0.5)
    return found


def _bind_opencode(config: dict) -> tuple[dict, int]:
    """One-command exact session bind for the mapped OpenCode pane.

    Returns (possibly updated config, exit code). Writes config only after the
    live TUI provably runs the bound --session; relaunch happens only with one
    explicit confirmation. Never guesses, never uses newest/mtime/cwd-only.
    """
    from actl.agents.opencode import (
        OpenCodeSessionError,
        create_session,
        live_cmdline_session,
        resolve_opencode,
        stored_session_id,
        valid_session_id,
    )

    spec = AGENTS["opencode"]
    try:
        target = get_target(config, "opencode").target
    except ValueError as exc:
        print(f"✗ {exc}; run `actl map opencode` first")
        return config, 1
    validation = validate_target("opencode", target)
    if not validation.valid:
        print(f"✗ Configured target is {validation.state} for OpenCode; binding blocked: {validation.detail}")
        return config, 1
    print(f"OpenCode pane: {validation.pane_id or target}")

    stored = stored_session_id(config)
    current = resolve_opencode(spec.data_dirs[0], target, validation.agent_pid, stored)
    if current.confidence == "exact":
        print(f"Current session: {stored} (exact, verified live)")
        print("/copy ready — already bound, nothing changed")
        return config, 0
    if stored:
        print(f"Current session: {stored} (stale: {current.detail})")
    else:
        print("Current session: unbound")

    pids = _pane_opencode_pids(validation.pane_pid) if validation.pane_pid else []
    if len(pids) != 1:
        print("✗ Selected pane does not contain exactly one live opencode process")
        return config, 1
    live = live_cmdline_session(pids[0])
    if valid_session_id(live):
        print(f"Pane TUI runs session {live} (exact, from live process).")
        if not _confirm("Bind this OpenCode conversation now?"):
            print("Declined; nothing changed.")
            return config, 1
        resolution = resolve_opencode(spec.data_dirs[0], target, pids[0], live)
        if resolution.confidence != "exact":
            print(f"✗ Binding refused: {resolution.detail}")
            return config, 1
        updated = dict(config)
        updated["agents"] = dict(config.get("agents", {}))
        updated["agents"]["opencode"] = {"target": target, "session_id": live}
        backup = backup_config()
        save_config(updated)
        print(f"Backup: {backup}")
        print("✓ OpenCode bound")
        print(f"session: {live}")
        print("/copy ready")
        return updated, 0

    print("Pane TUI is bare `opencode` (no session identity). Binding needs")
    print("one exact session plus a pane relaunch of that TUI only.")
    if not _confirm("Bind this OpenCode conversation now?"):
        print("Declined; nothing changed.")
        return config, 1
    try:
        session_id = create_session(validation.path if validation.path != "-" else ".")
    except OpenCodeSessionError as exc:
        print(f"✗ Could not create an OpenCode session: {exc}")
        return config, 1
    print(f"OpenCode must restart with an explicit session ID ({session_id}).")
    if not _confirm(f"Relaunch pane {validation.pane_id or target} safely as `opencode --session {session_id}`?"):
        print(f"Declined; run it manually, then `actl opencode-session --bind --session {session_id}`")
        return config, 1
    try:
        send_keys(target, "C-c")
    except WriterDenied as denied:
        print(f"✗ {denied.code}: {denied.detail}")
        return config, 1
    except Exception as exc:
        print(f"✗ Could not interrupt pane TUI: {exc}")
        return config, 1
    if not _wait_while(lambda: bool(_pane_opencode_pids(validation.pane_pid)), 15.0):
        print("✗ Pane TUI did not exit; resolve it manually, then bind with "
              f"`actl opencode-session --bind --session {session_id}`")
        return config, 1
    launch = f"opencode --session {session_id} {shlex.quote(validation.path)}" if validation.path != "-" else f"opencode --session {session_id}"
    try:
        send_keys(target, launch, "Enter")
    except WriterDenied as denied:
        print(f"✗ {denied.code}: {denied.detail}")
        return config, 1
    except Exception as exc:
        print(f"✗ Could not relaunch pane TUI: {exc}; run manually: {launch}")
        return config, 1
    pid = _wait_live_session(session_id, validation.pane_pid, 30.0)
    if pid is None:
        print(f"✗ Relaunched TUI did not prove session {session_id}; run manually: {launch}")
        return config, 1
    resolution = resolve_opencode(spec.data_dirs[0], target, pid, session_id)
    if resolution.confidence != "exact":
        print(f"✗ Binding refused: {resolution.detail}")
        return config, 1
    updated = dict(config)
    updated["agents"] = dict(config.get("agents", {}))
    updated["agents"]["opencode"] = {"target": target, "session_id": session_id}
    backup = backup_config()
    save_config(updated)
    print(f"Backup: {backup}")
    print("✓ OpenCode bound")
    print(f"session: {session_id}")
    print("/copy ready")
    return updated, 0


def _bind_opencode_session(config: dict, target: str, session_id: str) -> dict | None:
    """Verify the live TUI proves the session, then persist the binding.

    Returns the updated config on success, None on failure (nothing written).
    """
    from actl.agents.opencode import resolve_opencode, valid_session_id

    if not valid_session_id(session_id):
        print(f"✗ Invalid OpenCode session id: {session_id}")
        return None
    validation = validate_target("opencode", target)
    if not validation.valid:
        print(f"✗ Configured target is {validation.state} for OpenCode; binding blocked: {validation.detail}")
        return None
    candidate = dict(config)
    candidate["agents"] = dict(config.get("agents", {}))
    candidate["agents"]["opencode"] = {"target": target, "session_id": session_id}
    resolution = resolve_opencode(
        AGENTS["opencode"].data_dirs[0], target, validation.agent_pid, session_id
    )
    if resolution.confidence != "exact":
        print(f"✗ Binding refused: {resolution.detail}")
        return None
    updated = dict(config)
    updated["agents"] = dict(config.get("agents", {}))
    updated["agents"]["opencode"] = {"target": target, "session_id": session_id}
    return updated


def _opencode_session_new(config: dict, directory: str | None) -> int:
    """Create an empty session via local ACP; never auto-starts any agent."""
    from actl.agents.opencode import OpenCodeSessionError, create_session

    cwd = directory or os.getcwd()
    if not Path(cwd).is_dir():
        print(f"✗ Directory does not exist: {cwd}")
        return 1
    try:
        session_id = create_session(cwd)
    except OpenCodeSessionError as exc:
        print(f"✗ Could not create an OpenCode session: {exc}")
        return 1
    print(f"session_id: {session_id}")
    print(f"launch: opencode --session {session_id} {cwd}")
    print("Next: run the launch command in your chosen pane, map it, then bind:")
    print(f"  actl opencode-session --bind --session {session_id}")
    return 0


def _opencode_session_bind(config: dict, session_id: str | None) -> int:
    if not session_id:
        print("✗ Usage: actl opencode-session --bind --session <SESSION_ID>")
        return 1
    try:
        target = get_target(config, "opencode").target
    except ValueError as exc:
        print(f"✗ {exc}")
        return 1
    bound = _bind_opencode_session(config, target, session_id)
    if bound is None:
        return 1
    backup = backup_config()
    save_config(bound)
    print(f"Backup: {backup}")
    print(f"✓ OpenCode session {session_id} bound to {target}")
    return 0


def _opencode_session_status(config: dict) -> int:
    from actl.agents.opencode import resolve_opencode, session_row, stored_session_id

    spec = AGENTS["opencode"]
    try:
        target = get_target(config, "opencode").target
    except ValueError as exc:
        print(f"bound session id: -\nreason: {exc}")
        return 1
    validation = validate_target("opencode", target)
    expected = stored_session_id(config)
    resolution = (
        resolve_opencode(spec.data_dirs[0], target, validation.agent_pid, expected)
        if validation.valid
        else resolve_opencode(spec.data_dirs[0], target, None, expected)
    )
    print(f"target: {target}")
    print(f"target state: {validation.state}")
    print(f"bound session id: {expected or '-'}")
    print(f"live cmdline session id: {resolution.live_session_id or '-'}")
    print(f"session row present: {session_row(spec.data_dirs[0], expected) is not None if expected else '-'}")
    print(f"match method: {resolution.match_method}")
    print(f"copy confidence: {resolution.confidence}")
    if resolution.detail:
        print(f"detail: {resolution.detail}")
    return 0 if resolution.confidence == "exact" else 1


def _unmap(config: dict, agent: str) -> dict:
    if agent not in config.get("agents", {}):
        print(f"{AGENTS[agent].display_name} is already unmapped")
        return config
    updated = dict(config)
    updated["agents"] = dict(config.get("agents", {}))
    updated["agents"].pop(agent, None)
    backup = backup_config()
    save_config(updated)
    from actl.core.audit import record

    record("unmap", agent=agent, target=config["agents"].get(agent, {}).get("target"), ok=True)
    print(f"Backup: {backup}")
    print(f"✓ {AGENTS[agent].display_name} unmapped")
    return updated


def _auto_reconcile(config: dict, *, announce: bool = True) -> dict:
    """Fail-closed automatic resync: remove stale mappings and map unique
    strong detections. reconcile() only ever applies unambiguous changes, so
    this never guesses. Writes config only when something changed."""
    detections = discover()
    updated, changes = reconcile(config, detections)
    if not changes:
        return config
    backup = backup_config()
    save_config(updated)
    if announce:
        print("Mappings changed:")
        print("\n".join(changes))
        print(f"Backup: {backup}")
    return updated


def _persist_target(config: dict, agent: str, pane_id: str) -> str:
    """ Persist pane_id (+ OpenCode session auto-bind) and return it."""
    from actl.core.discovery import auto_bind_opencode_session

    agents = dict(config.get("agents", {}))
    entry = dict(agents.get(agent, {}))
    entry["target"] = pane_id
    if agent == "opencode":
        sid = auto_bind_opencode_session(pane_id, AGENTS[agent].data_dirs[0])
        if sid:
            entry["session_id"] = sid
        elif "session_id" in entry:
            del entry["session_id"]
    agents[agent] = entry
    updated = dict(config)
    updated["agents"] = agents
    backup = backup_config()
    save_config(updated)
    print(f"Backup: {backup}")
    return pane_id


def _resolve_live_target(config: dict, agent: str) -> str:
    """Return the agent's live target.

    When the stored mapping went stale (agent turned off/on, relaunched, or
    moved panes), a live pane is found and the mapping is re-applied
    automatically before the operation proceeds. Unique pane: direct remap.
    Several panes: keep the call non-interactive callers safe by auto-selecting
    the most recently started agent process; when start times tie or are
    unreadable, fall back to the inline pane prompt. Zero panes fail closed.
    """
    stored_error: ValueError | None = None
    try:
        target = get_target(config, agent).target
        validation = validate_target(agent, target)
        if validation.valid:
            return target
        stored_error = ValueError(
            f"Configured target is {validation.state} for {AGENTS[agent].display_name}: {validation.detail}"
        )
    except ValueError as exc:
        stored_error = exc
    matches = [d for d in discover() if d.agent == agent and d.confidence in STRONG_CONFIDENCE]
    if len(matches) == 1:
        # Never mutate the caller's config: copy agents (and the entry) before
        # writing the re-mapped target.
        agents = dict(config.get("agents", {}))
        entry = dict(agents.get(agent, {}))
        entry["target"] = matches[0].pane.pane_id
        agents[agent] = entry
        updated = dict(config)
        updated["agents"] = agents
        backup = backup_config()
        save_config(updated)
        print(f"✓ {AGENTS[agent].display_name} re-mapped to {matches[0].pane.pane_id}")
        print(f"Backup: {backup}")
        return matches[0].pane.pane_id
    if len(matches) > 1:
        from actl.core.discovery import newest_detection

        auto = newest_detection(matches)
        if auto is not None:
            pane_id = _persist_target(config, agent, auto.pane.pane_id)
            print(f"✓ {AGENTS[agent].display_name} auto-mapped to {auto.pane.pane_id} (newest live pane)")
            return pane_id
        print(f"Multiple live {AGENTS[agent].display_name} panes found:")
        for idx, d in enumerate(matches, 1):
            print(f"  [{idx}] {d.pane.pane_id} — {d.evidence}")
        raw = input("Choose pane: ").strip()
        detection = None
        if raw.startswith("%"):
            detection = next((d for d in matches if d.pane.pane_id == raw), None)
            if detection is None:
                raise ValueError(f"No pane matching {raw}")
        else:
            try:
                detection = matches[int(raw) - 1]
            except (ValueError, IndexError):
                raise ValueError("Invalid pane selection")
        pane_id = _persist_target(config, agent, detection.pane.pane_id)
        print(f"✓ {AGENTS[agent].display_name} mapped to {detection.pane.pane_id}")
        return pane_id
    if stored_error is not None:
        raise stored_error
    raise ValueError(f"{AGENTS[agent].display_name} is not currently mapped to a live pane")


def _paste_mode(agent: str, target: str) -> None:
    print("Paste prompt.")
    print("Finish with a line containing only:")
    print("\n::send\n")
    lines: list[str] = []
    while True:
        try:
            line = input()
        except EOFError:
            print("✗ Paste cancelled (EOF)")
            return
        if line == "::send":
            break
        if line == "::cancel":
            print("✗ Paste cancelled")
            return
        lines.append(line)
    prompt = "\n".join(lines)
    if not prompt:
        print("✗ Empty prompt")
        return
    try:
        send_prompt(target, prompt)
    except WriterDenied as denied:
        print(f"✗ {denied.code}: {denied.detail}")
        return
    print(f"{AGENTS[agent].display_name}에게 보냈어요 · 답이 오면: actl copy {agent}")


def _print_cli_help() -> None:
    print(
        "actl — Agent Control CLI\n"
        "\n"
        "  actl                REPL (Agent > prompt, /help for commands)\n"
        "  actl tui            Agent board: number=select+preview, c=copy, p=print,\n"
        "                      m=remap, s=send, h=help, r=refresh, q=quit\n"
        "  actl gui [--ssh T]  Windows GUI board (buttons, no terminal keys)\n"
        "  actl serve [port] [--host HOST] [--token TOKEN]  Web board\n"
        "  actl copy AGENT [--print]   Copy (or print) last response\n"
        "  echo \"질문\" | actl send AGENT   Send a prompt to an agent\n"
        "  actl push FILE [--print]   Push file to MainPC over SSH session\n"
        "  actl doctor               자가진단 (python/tmux/ssh/클립보드/매핑)\n"
        "  actl audit [N]            본문 없는 로컬 감사 로그\n"
        "  actl history [AGENT] [N]  결과 hash/source 이력 (본문 없음)\n"
        "  actl map AGENT      Visual pane picker (number or %ID, e.g. %69)\n"
        "  actl discover [--json] [--apply]  List (or apply) live pane detections\n"
        "  actl status [AGENT] Probe-free mapping + liveness table\n"
        "  actl bind opencode  One-command OpenCode session bind\n"
        "  --ssh TARGET        Route tmux via ssh (MainPC board: actl tui --ssh asus)\n"
    )


def repl() -> int:
    config = load_config()
    config = _auto_reconcile(config)
    warning = migration_warning(config)
    if warning:
        print(f"WARNING: {warning}")
    _menu()
    current: str | None = None

    while current is None:
        try:
            value = read_event("Agent > ").text
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        current = _resolve_selection(value)
        if current is None:
            print("Unknown agent. Enter a number, name, or alias.")

    print(f"{AGENTS[current].display_name} selected")
    while True:
        try:
            event = read_event(f"{current} > ")
            line = event.text
        except EOFError:
            print()
            return 0
        except KeyboardInterrupt:
            print("\nUse /quit to exit.")
            continue
        if not line.strip():
            continue
        # A pasted string beginning with '/' is a prompt, never a controller
        # command. This is what prevents pasted multiline payloads splitting.
        if event.pasted or not line.startswith("/"):
            try:
                _send_to_selected(config, current, line)
                print(f"{AGENTS[current].display_name}에게 보냈어요 · 답이 오면: actl copy {current}")
            except Exception as exc:
                print(f"✗ {exc}")
            continue

        parts = shlex.split(line)
        cmd = parts[0].lower()
        try:
            if cmd in {"/quit", "/exit"}:
                return 0
            if cmd == "/help":
                print("/switch AGENT | /copy [--print] | /result | /bind | /status [AGENT] | /debug | /paste | /discover | /probe [AGENT] | /refresh | /config | /reload | /tui | /quit")
            elif cmd == "/switch":
                if len(parts) != 2:
                    print("Usage: /switch AGENT")
                    continue
                nxt = _resolve_selection(parts[1])
                if not nxt:
                    print(_unknown_agent(parts[1]))
                    continue
                current = nxt
                print(f"✓ {AGENTS[current].display_name} selected")
            elif cmd == "/copy":
                if len(parts) > 1 and parts[1] in {"--print", "--show", "--manual"}:
                    _copy(config, current, print_only=True)
                elif len(parts) == 1:
                    _copy(config, current)
                else:
                    print("Usage: /copy [--print]")
                    continue
            elif cmd == "/result":
                _copy(config, current, print_only=True)
            elif cmd == "/bind":
                if current != "opencode":
                    print("✗ /bind is only supported for OpenCode")
                    continue
                config, _ = _bind_opencode(config)
            elif cmd == "/status":
                name = _resolve_selection(parts[1]) if len(parts) > 1 else current
                if len(parts) > 1 and not name:
                    print(_unknown_agent(parts[1]))
                else:
                    _print_status(config, name)
            elif cmd == "/debug":
                _debug(config, current)
            elif cmd == "/paste":
                _paste_mode(current, _resolve_live_target(config, current))
            elif cmd == "/tui":
                from actl.tui import run_tui

                run_tui()
                config = load_config()
            elif cmd == "/discover":
                _discover(config)
            elif cmd == "/probe":
                name = _resolve_selection(parts[1]) if len(parts) > 1 else current
                if not name:
                    print(_unknown_agent(parts[1] if len(parts) > 1 else ""))
                else:
                    print("\n".join(probe_agent(name)))
            elif cmd == "/config":
                print(CONFIG_PATH)
            elif cmd == "/reload":
                config = load_config()
                warning = migration_warning(config)
                if warning:
                    print(f"WARNING: {warning}")
                print("✓ Config reloaded")
            elif cmd == "/refresh":
                # Re-run pane discovery and reconcile right now, without
                # restarting actl. Covers agents turned off/on or relaunched in
                # another pane/project. /copy itself already follows the pane's
                # current cwd on every call.
                config = _auto_reconcile(config)
            else:
                print(f"Unknown command: {cmd}. Use /help")
        except Exception as exc:
            print(f"✗ {exc}")


_RUNTIME_OPS = frozenset({"discover", "status", "reserve", "send", "collect", "interrupt"})


def _emit_runtime_envelope(envelope: dict, *, file=None) -> None:
    """Write exactly one JSON object to stdout (or the given file)."""
    target = file if file is not None else sys.stdout
    target.write(json.dumps(envelope, ensure_ascii=False, separators=(",", ":")) + "\n")
    target.flush()


def run_runtime_request_stdin(operation: str, *, stdin_text: str | None = None) -> int:
    """Managed JSON contract entry: no config/clipboard/reconcile side effects.

    Reads one JSON object from stdin (or stdin_text in tests), dispatches
    handle_runtime_request, prints one envelope to stdout, diagnostics to stderr.
    """
    from actl.core import runtime as runtime_mod

    if operation not in _RUNTIME_OPS:
        print(
            "Usage: actl runtime <discover|status|reserve|send|collect|interrupt> --request-stdin",
            file=sys.stderr,
        )
        return 3

    raw = sys.stdin.read() if stdin_text is None else stdin_text
    try:
        request = json.loads(raw) if raw.strip() else None
    except json.JSONDecodeError as exc:
        print(f"invalid JSON on stdin: {exc}", file=sys.stderr)
        envelope = runtime_mod.build_response(
            None,
            False,
            error=runtime_mod.build_error("INVALID_ARGUMENT", f"invalid JSON on stdin: {exc}"),
        )
        _emit_runtime_envelope(envelope)
        return 3

    if not isinstance(request, dict):
        print("runtime request must be a JSON object", file=sys.stderr)
        envelope = runtime_mod.build_response(
            None,
            False,
            error=runtime_mod.build_error("INVALID_ARGUMENT", "request must be a JSON object"),
        )
        _emit_runtime_envelope(envelope)
        return 3

    body_op = request.get("operation")
    if body_op is None:
        request = dict(request)
        request["operation"] = operation
    elif body_op != operation:
        print(f"CLI operation {operation!r} does not match request.operation {body_op!r}", file=sys.stderr)
        envelope = runtime_mod.build_response(
            request.get("requestId") if isinstance(request.get("requestId"), str) else None,
            False,
            error=runtime_mod.build_error(
                "INVALID_ARGUMENT",
                f"CLI operation {operation!r} does not match request.operation {body_op!r}",
            ),
        )
        _emit_runtime_envelope(envelope)
        return 3

    response, code = runtime_mod.handle_runtime_request(request)
    _emit_runtime_envelope(response)
    return code


def _dispatch(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="actl", description="Control multiple AI agent TUIs through tmux")
    parser.add_argument("--version", action="version", version=f"actl {VERSION}")
    parser.add_argument("--init", action="store_true", help="Create default config and exit")
    parser.add_argument("--discover", action="store_true", help="Show tmux panes and suggested agent mappings")
    parser.add_argument("--apply", action="store_true", help="Apply only exact/high-confidence discovery mappings")
    parser.add_argument("--status", nargs="?", const="all", help="Show status for all or one agent")
    parser.add_argument("--print-config", action="store_true", help="Print current config")
    parser.add_argument("--probe", help="Read-only inspect local session storage for one agent")
    parser.add_argument("--debug", help="Read-only selected-agent target and Claude correlation trace")
    parser.add_argument("--copy-diagnostic", help="Read-only extraction probe; never copies to clipboard")
    parser.add_argument("--session", help="OpenCode session id for map/bind")
    parser.add_argument("--dir", help="Working directory for opencode-session --new")
    parser.add_argument("--new", action="store_true", help="Create an OpenCode session id (opencode-session)")
    parser.add_argument("--bind", action="store_true", help="Bind an OpenCode session id (opencode-session)")
    parser.add_argument("--opencode-session", nargs="?", const="status", help="Session-aware OpenCode setup: --new | --bind --session ID | --status")
    parser.add_argument("--print", action="store_true", help="With copy: print the result instead of copying")
    parser.add_argument("--json", action="store_true", help="With discover or doctor: emit one machine-readable JSON object")
    parser.add_argument(
        "--request-stdin",
        action="store_true",
        help="With runtime: read one JSON request from stdin and emit one JSON envelope to stdout",
    )
    parser.add_argument(
        "--ssh",
        help="Route tmux through 'ssh TARGET' (MainPC remote board, e.g. --ssh asus)",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="With serve: bind address (public binding requires explicit --host)",
    )
    parser.add_argument("--token", help="With serve: bearer token for non-loopback web access")
    parser.add_argument("command", nargs="?", help="discover, map, unmap, bind, copy, extract, push, runtime, tui, gui, serve, help, doctor, or opencode-session")
    parser.add_argument("command_agent", nargs="?", help="Agent for map/unmap/copy/extract, port for serve, or runtime operation")
    parser.add_argument("command_extra", nargs="?", help="Pane for extract, or runtime operation")
    args = parser.parse_args(argv)

    if args.json and args.apply and (args.command == "discover" or args.discover):
        raise SystemExit("--json cannot be combined with --apply")

    if args.ssh:
        from actl.core.tmux import set_remote_ssh

        set_remote_ssh(args.ssh)

    # Managed contract path must not touch config, reconcile, clipboard, or REPL.
    if args.command == "runtime":
        if not args.request_stdin:
            print("runtime requires --request-stdin", file=sys.stderr)
            raise SystemExit(3)
        if not args.command_agent:
            print(
                "Usage: actl runtime <discover|status|reserve|send|collect|interrupt> --request-stdin",
                file=sys.stderr,
            )
            raise SystemExit(3)
        raise SystemExit(run_runtime_request_stdin(args.command_agent))

    if args.json and not args.apply and (
        (args.command == "discover" and not args.command_agent) or args.discover
    ):
        config = json.loads(CONFIG_PATH.read_text(encoding="utf-8")) if CONFIG_PATH.exists() else {"agents": {}}
        _discover_json(config)
        return

    ensure_config()
    if args.init:
        print(CONFIG_PATH)
        return
    if args.opencode_session and not args.command:
        if args.new and not args.bind:
            raise SystemExit(_opencode_session_new(load_config(), args.dir))
        if args.bind and not args.new:
            raise SystemExit(_opencode_session_bind(load_config(), args.session))
        if not args.new and not args.bind:
            raise SystemExit(_opencode_session_status(load_config()))
        raise SystemExit("Usage: actl opencode-session [--new [--dir DIR] | --bind --session ID | --status]")
    if args.command:
        if args.command == "status":
            agent = _resolve_selection(args.command_agent) if args.command_agent else None
            if args.command_agent and not agent:
                raise SystemExit(_unknown_agent(args.command_agent))
            _print_status(load_config(), agent, json_output=args.json)
            return
        if args.command == "discover" and not args.command_agent:
            config = load_config()
            detections = _discover(config)
            if args.apply:
                _apply_discovery(config, detections)
            return
        if args.command in {"map", "unmap"} and args.command_agent:
            agent = _resolve_selection(args.command_agent)
            if not agent:
                raise SystemExit(_unknown_agent(args.command_agent))
            if args.apply:
                raise SystemExit("--apply is only valid with discover")
            if args.command == "map":
                _map(load_config(), agent, args.session)
            else:
                if args.session:
                    raise SystemExit("--session is only valid with map opencode")
                _unmap(load_config(), agent)
            return
        if args.command == "opencode-session" and not args.command_agent:
            if args.new and not args.bind:
                raise SystemExit(_opencode_session_new(load_config(), args.dir))
            if args.bind and not args.new:
                raise SystemExit(_opencode_session_bind(load_config(), args.session))
            if not args.new and not args.bind:
                raise SystemExit(_opencode_session_status(load_config()))
            raise SystemExit("Usage: actl opencode-session [--new [--dir DIR] | --bind --session ID | --status]")
        if args.command == "bind" and args.command_agent:
            agent = _resolve_selection(args.command_agent)
            if not agent:
                raise SystemExit(_unknown_agent(args.command_agent))
            if agent != "opencode":
                raise SystemExit("bind is only supported for opencode")
            _, code = _bind_opencode(load_config())
            raise SystemExit(code)
        if args.command == "copy" and args.command_agent:
            agent = _resolve_selection(args.command_agent)
            if not agent:
                raise SystemExit(_unknown_agent(args.command_agent))
            raise SystemExit(_copy(load_config(), agent, print_only=args.print))
        if args.command == "extract" and args.command_agent:
            agent = _resolve_selection(args.command_agent)
            if not agent:
                raise SystemExit(_unknown_agent(args.command_agent))
            raise SystemExit(_extract(load_config(), agent, args.command_extra or args.session))
        if args.command == "send" and args.command_agent:
            agent = _resolve_selection(args.command_agent)
            if not agent:
                raise SystemExit(_unknown_agent(args.command_agent))
            prompt = sys.stdin.read()
            if not prompt.strip():
                print("empty prompt", file=sys.stderr)
                raise SystemExit(1)
            try:
                target = _send_to_selected(load_config(), agent, prompt)
            except Exception as exc:
                print(str(exc), file=sys.stderr)
                raise SystemExit(1)
            print(f"{AGENTS[agent].display_name}에게 보냈어요 · 답이 오면: actl copy {agent}")
            return
        if args.command == "tui" and not args.command_agent:
            from actl.tui import run_tui

            raise SystemExit(run_tui())
        if args.command == "gui" and not args.command_agent:
            from actl.gui import run_gui

            raise SystemExit(run_gui(ssh_target=args.ssh))
        if args.command == "serve":
            from actl.serve import run_serve

            port = args.command_agent or args.command_extra or 8765
            raise SystemExit(run_serve(host=args.host, port=int(port), token=args.token))
        if args.command == "help" and not args.command_agent:
            _print_cli_help()
            return
        if args.command == "push" and args.command_agent:
            raise SystemExit(_push_file(args.command_agent, print_only=args.print))
        if args.command == "doctor" and not args.command_agent:
            raise SystemExit(_doctor(json_output=args.json))
        if args.command == "audit":
            try:
                limit = int(args.command_agent or 50)
            except ValueError:
                raise SystemExit("audit limit must be an integer")
            raise SystemExit(_audit(limit))
        if args.command == "history":
            agent = _resolve_selection(args.command_agent) if args.command_agent else None
            if args.command_agent and not agent:
                raise SystemExit(_unknown_agent(args.command_agent))
            try:
                limit = int(args.command_extra or 50)
            except ValueError:
                raise SystemExit("history limit must be an integer")
            raise SystemExit(_history(agent, limit))
        raise SystemExit(
            "Usage: actl discover [--apply] | actl map AGENT [--session ID] | actl unmap AGENT | "
            "actl bind opencode | actl copy AGENT [--print] | actl extract AGENT [PANE] | actl push FILE [--print] | "
            "actl tui | actl help | actl doctor | actl audit [N] | actl history [AGENT] [N] | actl opencode-session ... | "
            "actl runtime <discover|status|reserve|send|collect|interrupt> --request-stdin"
        )
    if args.discover:
        config = load_config()
        detections = _discover(config)
        if args.apply:
            _apply_discovery(config, detections)
        return
    if args.status:
        cfg = load_config()
        agent = None if args.status == "all" else _resolve_selection(args.status)
        if args.status != "all" and not agent:
            raise SystemExit(_unknown_agent(args.status))
        _print_status(cfg, agent, json_output=args.json)
        return
    if args.probe:
        agent = _resolve_selection(args.probe)
        if not agent:
            raise SystemExit(_unknown_agent(args.probe))
        print("\n".join(probe_agent(agent)))
        return
    if args.debug:
        agent = _resolve_selection(args.debug)
        if not agent:
            raise SystemExit(_unknown_agent(args.debug))
        _debug(load_config(), agent)
        return
    if args.copy_diagnostic:
        agent = _resolve_selection(args.copy_diagnostic)
        if not agent:
            raise SystemExit(_unknown_agent(args.copy_diagnostic))
        raise SystemExit(_copy_diagnostic(load_config(), agent))
    if args.print_config:
        print(json.dumps(load_config(), indent=2, ensure_ascii=False))
        return
    raise SystemExit(repl())


def main(argv: list[str] | None = None) -> None:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if raw_argv and Path(raw_argv[0]).name == "actl":
        raw_argv = raw_argv[1:]
    try:
        _dispatch(raw_argv)
    except TmuxError as exc:
        if raw_argv[:1] == ["runtime"] and "--request-stdin" in raw_argv:
            raise
        reason = str(exc).splitlines()[0][:200] or type(exc).__name__
        print(
            "AI 작업창(tmux)이 켜져 있지 않아요 — ASUS에서 AI를 먼저 실행한 뒤 다시 시도하세요 "
            f"({reason})",
            file=sys.stderr,
        )
        raise SystemExit(1)


if __name__ == "__main__":
    main()
