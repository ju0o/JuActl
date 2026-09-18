from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path

from actl.core.config import get_target
from actl.core.models import PaneInfo
from actl.core.registry import AGENTS
from actl.core.tmux import TmuxError, list_panes, pane_field
from actl.core.validation import ProcessInfo, pane_processes, process_environment, validate_target

STRONG_CONFIDENCE = {"exact", "high"}


@dataclass(frozen=True)
class Detection:
    pane: PaneInfo
    agent: str | None
    confidence: str
    evidence: str


def _executable(process: ProcessInfo) -> str:
    return Path(process.args.split(maxsplit=1)[0]).name.lower() if process.args else ""


def _candidate(pane_pid: int, processes: list[ProcessInfo]) -> tuple[str | None, str, str]:
    found: list[tuple[str, str, str, int]] = []
    for process in processes:
        executable = _executable(process)
        if executable == "claude":
            profile = process_environment(process.pid).get("CLAUDE_CONFIG_DIR")
            for agent in ("claude-team", "claude-pro"):
                expected = AGENTS[agent].data_dirs[0].expanduser().resolve(strict=False)
                if profile and Path(profile).expanduser().resolve(strict=False) == expected:
                    found.append((agent, "exact", f"pid {process.pid}: claude profile {expected}", process.pid))
                    break
            else:
                found.append(("", "low", f"pid {process.pid}: claude without a supported CLAUDE_CONFIG_DIR", process.pid))
        elif executable == "opencode":
            found.append(("opencode", "high", f"pid {process.pid}: opencode", process.pid))
        elif executable == "codex":
            found.append(("codex", "high", f"pid {process.pid}: codex", process.pid))
        elif "cursor-agent" in process.args.lower() or "/cursor-agent/" in process.args.lower():
            found.append(("cursor", "exact", f"pid {process.pid}: Cursor Agent runtime", process.pid))
        elif executable in {"cmd", "cmd.exe", "commandcode"}:
            found.append(("commandcode", "high", f"pid {process.pid}: {executable}", process.pid))
        elif executable == "cline":
            found.append(("cline", "high", f"pid {process.pid}: cline", process.pid))
        elif "/cline" in process.args.lower() or executable == ".cline":
            found.append(("cline", "high", f"pid {process.pid}: Cline runtime", process.pid))
        elif executable == "grok":
            found.append(("grok", "high", f"pid {process.pid}: grok", process.pid))
        elif "commandcode" in process.args.lower() or "command code" in process.args.lower():
            found.append(("commandcode", "high", f"pid {process.pid}: CommandCode runtime", process.pid))
    by_pid = {process.pid: process for process in processes}

    def depth(pid: int) -> int:
        result = 0
        while pid != pane_pid and pid in by_pid:
            pid = by_pid[pid].ppid
            result += 1
        return result if pid == pane_pid else 10**9

    agents = [item for item in found if item[0]]
    if agents:
        nearest = min(depth(item[3]) for item in agents)
        closest = [item for item in agents if depth(item[3]) == nearest]
        names = {item[0] for item in closest}
        if len(names) == 1:
            agent, confidence, evidence, _ = closest[0]
            return agent, confidence, evidence
        return None, "low", "multiple Agent process identities in one pane"
    return None, ("low" if found else "unknown"), (found[0][2] if found else "no supported Agent process")


def detect_pane(pane: PaneInfo) -> Detection:
    try:
        pane_pid = int(pane_field(pane.pane_id, "#{pane_pid}"))
    except (TmuxError, ValueError):
        return Detection(pane, None, "unknown", "pane PID unavailable")
    agent, confidence, evidence = _candidate(pane_pid, pane_processes(pane_pid))
    return Detection(pane, agent or None, confidence, evidence)


def suggest_agent(pane: PaneInfo) -> str | None:
    return detect_pane(pane).agent


def discover() -> list[Detection]:
    return [detect_pane(pane) for pane in list_panes()]


def target_matches(pane: PaneInfo, target: str) -> bool:
    return target in {pane.pane_id, pane.target}


def mapping_state(config: dict, detection: Detection) -> str:
    if not detection.agent:
        return "-"
    try:
        target = get_target(config, detection.agent).target
    except ValueError:
        return "UNMAPPED"
    if target_matches(detection.pane, target):
        return "MATCH"
    return "STALE" if validate_target(detection.agent, target).state in {"DOWN", "MISMATCH"} else "OTHER"


def newest_detection(choices: list[Detection]) -> Detection | None:
    """Pick the pane whose agent process started most recently.

    Each candidate pane has exactly one live agent PID (parsed from the
    evidence string); compare wall-clock process start times via /proc and
    return the youngest. Ties or unreadable start times return None so the
    caller keeps fail-closed behavior.
    """
    import os
    import re
    import time

    def _start_ms(pid: int) -> int | None:
        try:
            stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
            tail = stat.rsplit(") ", 1)[1].split()
            starttime_ticks = int(tail[19])
            clk_tck = os.sysconf("SC_CLK_TCK")
            uptime = float(Path("/proc/uptime").read_text(encoding="utf-8").split()[0])
            return int((time.time() - uptime + starttime_ticks / clk_tck) * 1000)
        except Exception:
            return None

    scored: list[tuple[int, Detection]] = []
    for choice in choices:
        match = re.search(r"pid (\d+)", choice.evidence or "")
        if not match:
            return None
        started = _start_ms(int(match.group(1)))
        if started is None:
            return None
        scored.append((started, choice))
    if not scored:
        return None
    scored.sort(key=lambda item: item[0])
    if len(scored) >= 2 and scored[-1][0] == scored[-2][0]:
        return None
    return scored[-1][1]


def reconcile(config: dict, detections: list[Detection]) -> tuple[dict, list[str]]:
    """Return a pane-id based mapping update without writing it.

    Stale mappings are removed fail-closed. Unmapped agents with exactly one
    strong detection are mapped; when several strong panes exist, an existing
    still-live mapping is kept, otherwise the most recently started agent
    process wins (newest pane auto-selected). Ties and unreadable start times
    stay fail-closed.
    """
    updated = deepcopy(config)
    agents = updated.setdefault("agents", {})
    changes: list[str] = []
    for agent in AGENTS:
        try:
            target = get_target(updated, agent).target
        except ValueError:
            continue
        if validate_target(agent, target).state in {"DOWN", "MISMATCH"}:
            agents.pop(agent, None)
            changes.append(f"{agent}: stale mapping removed")

    strong = [d for d in detections if d.agent and d.confidence in STRONG_CONFIDENCE]
    for agent in AGENTS:
        choices = [d for d in strong if d.agent == agent]
        if not choices:
            continue
        chosen: Detection | None = None
        if len(choices) == 1:
            chosen = choices[0]
        else:
            try:
                current = get_target(updated, agent).target
            except ValueError:
                current = None
            if current and validate_target(agent, current).valid:
                continue
            newest = newest_detection(choices)
            if newest is None:
                continue
            chosen = newest
        if chosen is None:
            continue
        occupied = False
        for other in AGENTS:
            if other == agent:
                continue
            try:
                target = get_target(updated, other).target
            except ValueError:
                continue
            if target_matches(chosen.pane, target):
                occupied = True
                break
        if occupied:
            continue
        old = agents.get(agent, {}).get("target")
        if old != chosen.pane.pane_id:
            entry: dict = {"target": chosen.pane.pane_id}
            if agent == "opencode":
                sid = auto_bind_opencode_session(
                    chosen.pane.pane_id, AGENTS[agent].data_dirs[0]
                )
                if sid:
                    entry["session_id"] = sid
            agents[agent] = entry
            suffix = " +session" if entry.get("session_id") else ""
            changes.append(f"{agent}: {chosen.pane.pane_id}{suffix}")
    return updated, changes


def auto_bind_opencode_session(pane_id: str, data_root: Path) -> str | None:
    """Auto-bind a session_id for an OpenCode pane.

    Returns the bound session_id, or None when no unambiguous session could be
    determined. Prefers the live process cmdline ``--session`` flag; falls back
    to DB adoption (pane cwd + process lifetime) for bare TUIs.
    """
    from actl.agents.opencode import _db_live_session, _opencode_pids, live_cmdline_session

    try:
        pane_pid = int(pane_field(pane_id, "#{pane_pid}"))
    except (TmuxError, ValueError):
        return None
    pids = _opencode_pids(pane_pid)
    if len(pids) != 1:
        return None
    sid = live_cmdline_session(pids[0])
    if sid is not None:
        return sid
    try:
        cwd = pane_field(pane_id, "#{pane_current_path}")
    except TmuxError:
        return None
    if not data_root:
        return None
    sid, _ = _db_live_session(data_root, pids[0], cwd)
    return sid or None


def manual_map(config: dict, agent: str, detection: Detection) -> dict:
    if validate_target(agent, detection.pane.pane_id).state != "UP":
        raise ValueError(f"Selected pane is not a live {AGENTS[agent].display_name} runtime")
    for other in AGENTS:
        if other == agent:
            continue
        try:
            target = get_target(config, other).target
        except ValueError:
            continue
        if target_matches(detection.pane, target):
            raise ValueError(f"Pane {detection.pane.pane_id} is already mapped to {other}")
    updated = deepcopy(config)
    entry = {"target": detection.pane.pane_id}
    if agent == "opencode":
        spec = AGENTS[agent]
        sid = auto_bind_opencode_session(detection.pane.pane_id, spec.data_dirs[0])
        if sid:
            entry["session_id"] = sid
    updated.setdefault("agents", {})[agent] = entry
    return updated
