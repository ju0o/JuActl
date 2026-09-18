"""Live Managed observation for an explicit tmux socket scope (PHASE 1A-LIVE / 1B).

Derives Managed candidates (Codex + Claude pro/team + Grok) from tmux + /proc +
executable identity only. Never uses pane title, pane_current_command alone,
newest-session heuristics, or the default/production tmux server unless that
socket path is explicitly given. Claude variants require explicit CLAUDE_CONFIG_DIR.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from actl.core.tmux import TmuxError, capture_pane, list_panes, pane_field
from actl.core.validation import ProcessInfo, pane_processes

_CD_FLAG_RE = re.compile(r"(?:^|\s)(?:-C|--cd)(?:=|\s+)(\S+)")


def read_boot_id() -> str | None:
    try:
        raw = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return raw or None


def proc_start_ticks(pid: int) -> str | None:
    """Return /proc/<pid>/stat starttime (field 22) as a decimal string."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
        tail = stat.rsplit(") ", 1)[1].split()
        return str(int(tail[19], 10))
    except (OSError, IndexError, ValueError):
        return None


def proc_exe_identity(pid: int) -> tuple[str, str, str] | None:
    """Return (exePath, dev, inode) for /proc/<pid>/exe."""
    try:
        exe = Path(os.readlink(f"/proc/{pid}/exe"))
        # Some kernels append " (deleted)"; strip for path compare but keep real path string.
        exe_str = str(exe)
        st = Path(f"/proc/{pid}/exe").stat()
        return exe_str, str(st.st_dev), str(st.st_ino)
    except OSError:
        return None


def socket_file_identity(socket_path: str) -> tuple[str, str, str] | None:
    """Return (absoluteSocketPath, dev, inode)."""
    try:
        path = Path(socket_path)
        if not path.is_absolute():
            path = path.resolve()
        st = path.stat()
        return str(path), str(st.st_dev), str(st.st_ino)
    except OSError:
        return None


def tmux_server_pid(socket_path: str) -> int | None:
    try:
        from actl.core.tmux import _run, _tmux_base

        raw = _run([*_tmux_base(socket_path), "display-message", "-p", "#{pid}"]).stdout.strip()
    except (TmuxError, OSError):
        return None
    try:
        return int(raw, 10)
    except ValueError:
        return None


def pane_mode_is_normal(pane_id: str, socket_path: str) -> bool | None:
    """True when pane is in normal mode; None if unreadable."""
    try:
        mode = pane_field(pane_id, "#{pane_in_mode}", socket_path=socket_path)
    except TmuxError:
        return None
    # pane_in_mode is 1 when in any mode (copy/view/...), 0 when normal.
    if mode in {"0", ""}:
        return True
    if mode == "1":
        return False
    try:
        mode2 = pane_field(pane_id, "#{pane_mode}", socket_path=socket_path)
    except TmuxError:
        return None
    return mode2 in {"", "0"}


def snapshot_hash_for_pane(pane_id: str, socket_path: str, *, history: int = 200) -> str | None:
    """SHA-256 of capture-pane bytes for the exact pane on the given socket."""
    import hashlib

    try:
        text = capture_pane(pane_id, history=history, socket_path=socket_path)
    except TmuxError:
        return None
    return hashlib.sha256(text.encode("utf-8")).hexdigest()

def _executable_name(process: ProcessInfo) -> str:
    first = process.args.split(maxsplit=1)[0] if process.args else ""
    return Path(first).name.lower()


def _args_tokens(process: ProcessInfo) -> list[str]:
    return (process.args or "").split()


def _is_codex_process(process: ProcessInfo) -> bool:
    """True when argv identifies Codex — binary name or shebang script path .../codex."""
    if _executable_name(process) == "codex":
        return True
    tokens = _args_tokens(process)
    # e.g. "python3 /path/to/codex" from a shebang wrapper used in tests / launches.
    for token in tokens[1:]:
        if Path(token).name.lower() == "codex":
            return True
    return False


def _claude_config_dir(agent_pid: int) -> str | None:
    try:
        from actl.core.validation import process_environment

        env = process_environment(agent_pid)
    except Exception:
        return None
    raw = env.get("CLAUDE_CONFIG_DIR")
    if not raw:
        return None
    try:
        return str(Path(raw).expanduser().resolve(strict=False))
    except OSError:
        return str(Path(raw).expanduser())


def _claude_agent_kind(agent_pid: int) -> str:
    """Map CLAUDE_CONFIG_DIR to claude-pro/claude-team; bare claude if unknown/missing."""
    configured = _claude_config_dir(agent_pid)
    if not configured:
        return "claude"
    try:
        from actl.core.registry import AGENTS

        for agent in ("claude-pro", "claude-team"):
            expected = AGENTS[agent].data_dirs[0].expanduser().resolve(strict=False)
            if Path(configured).resolve(strict=False) == expected:
                return agent
    except Exception:
        pass
    return "claude"


def _find_unique_agent(
    pane_pid: int,
    processes: list[ProcessInfo],
    *,
    want_kind: str | None,
) -> tuple[str, ProcessInfo] | None:
    """Return exactly one Agent (kind, process) nearest to the pane root, or None."""
    found: list[tuple[str, ProcessInfo]] = []
    for process in processes:
        name = _executable_name(process)
        kind: str | None = None
        if _is_codex_process(process):
            kind = "codex"
        elif name == "claude":
            kind = _claude_agent_kind(process.pid)
        elif name == "opencode":
            kind = "opencode"
        elif name == "grok":
            kind = "grok"
        elif name in {"cmd", "cmd.exe", "commandcode"} or "commandcode" in process.args.lower():
            kind = "commandcode"
        elif name in {"cline", ".cline"} or "/cline" in process.args.lower():
            kind = "cline"
        elif "cursor-agent" in process.args.lower():
            kind = "cursor"
        if kind is None:
            continue
        if want_kind and kind != want_kind:
            continue
        found.append((kind, process))
    if not found:
        return None
    by_pid = {p.pid: p for p in processes}

    def depth(pid: int) -> int:
        result = 0
        while pid != pane_pid and pid in by_pid:
            pid = by_pid[pid].ppid
            result += 1
        return result if pid == pane_pid else 10**9

    nearest = min(depth(p.pid) for _, p in found)
    closest = [(k, p) for k, p in found if depth(p.pid) == nearest]
    kinds = {k for k, _ in closest}
    if len(kinds) != 1 or len(closest) != 1:
        return None
    return closest[0]


def _codex_profile_root(agent_pid: int) -> str | None:
    try:
        from actl.core.validation import process_environment

        env = process_environment(agent_pid)
    except Exception:
        env = {}
    raw = env.get("CODEX_HOME")
    if raw:
        try:
            return str(Path(raw).expanduser().resolve(strict=False))
        except OSError:
            return str(Path(raw).expanduser())
    try:
        return str((Path.home() / ".codex").resolve(strict=False))
    except OSError:
        return str(Path.home() / ".codex")


def _claude_profile_root(agent_pid: int) -> str | None:
    """Explicit CLAUDE_CONFIG_DIR only; never default-guess team vs pro."""
    return _claude_config_dir(agent_pid)


def _grok_profile_root(agent_pid: int) -> str | None:
    """GROK_HOME / XDG override if set; else process-owned registry dir; else ~/.grok."""
    try:
        from actl.core.validation import process_environment

        env = process_environment(agent_pid)
    except Exception:
        env = {}
    raw = env.get("GROK_HOME")
    if raw:
        try:
            return str(Path(raw).expanduser().resolve(strict=False))
        except OSError:
            return str(Path(raw).expanduser())
    for xdg_key, leaf in (("XDG_CONFIG_HOME", "grok"), ("XDG_DATA_HOME", "grok")):
        base = env.get(xdg_key)
        if not base:
            continue
        candidate = Path(base).expanduser() / leaf
        if candidate.is_dir():
            try:
                return str(candidate.resolve(strict=False))
            except OSError:
                return str(candidate)
    # Prefer a registry data dir that this process actually has open under sessions/.
    try:
        from actl.core.registry import AGENTS

        registry_dirs = list(AGENTS["grok"].data_dirs)
    except Exception:
        registry_dirs = [Path.home() / ".grok", Path.home() / ".config/grok", Path.home() / ".local/share/grok"]
    owned: list[Path] = []
    for data_dir in registry_dirs:
        try:
            root = data_dir.expanduser().resolve(strict=False)
        except OSError:
            root = data_dir.expanduser()
        sessions = root / "sessions"
        if not sessions.is_dir():
            continue
        from actl.agents.grok import _open_event_files

        if _open_event_files(agent_pid, sessions):
            owned.append(root)
    if len(owned) == 1:
        return str(owned[0])
    try:
        return str((Path.home() / ".grok").resolve(strict=False))
    except OSError:
        return str(Path.home() / ".grok")


def _workspace_root(agent_args: str, pane_cwd: str) -> str | None:
    match = _CD_FLAG_RE.search(agent_args or "")
    if match:
        try:
            return str(Path(match.group(1)).expanduser().resolve(strict=False))
        except OSError:
            return str(Path(match.group(1)).expanduser())
    if pane_cwd:
        try:
            return str(Path(pane_cwd).expanduser().resolve(strict=False))
        except OSError:
            return pane_cwd
    return None


def _expected_session_for_codex(agent_pid: int, profile_root: str, pane_id: str) -> str:
    """BOOTSTRAP when no exact process-owned session; else session id. Never newest-file pick."""
    try:
        from actl.agents import codex as codex_agent

        resolution = codex_agent.resolve_codex(Path(profile_root), pane_id, codex_pid=agent_pid)
        if resolution.session_id and resolution.confidence not in {"none", ""}:
            return str(resolution.session_id)
    except Exception:
        pass
    return "BOOTSTRAP"


def _expected_session_for_claude(
    agent_pid: int,
    profile_root: str,
    workspace: str | None,
    pane_pid: int,
) -> str:
    """BOOTSTRAP when no exact active session+transcript; else session id. Never newest-file."""
    if not workspace:
        return "BOOTSTRAP"
    try:
        from actl.agents import claude as claude_agent

        resolution = claude_agent.resolve_claude(Path(profile_root), workspace, pane_pid)
        if resolution.session_id and resolution.confidence == "exact" and resolution.transcript:
            return str(resolution.session_id)
    except Exception:
        pass
    return "BOOTSTRAP"


def _expected_session_for_grok(agent_pid: int, profile_root: str, pane_id: str) -> str:
    """BOOTSTRAP when no exact process-owned events.jsonl session; else session id. Never newest-file."""
    try:
        from actl.agents import grok as grok_agent

        resolution = grok_agent.resolve_grok(Path(profile_root), pane_id, grok_pid=agent_pid)
        if resolution.session_id and resolution.confidence == "exact":
            return str(resolution.session_id)
    except Exception:
        pass
    return "BOOTSTRAP"


def observe_socket_candidates(
    socket_path: str,
    agent_kind: str | None = None,
    *,
    host_key: str,
    uid: str,
) -> list[dict[str, Any]]:
    """Observe candidates on one explicit socket. Empty list on unreadable socket."""
    sock_id = socket_file_identity(socket_path)
    if sock_id is None:
        return []
    abs_sock, sock_dev, sock_ino = sock_id
    boot = read_boot_id()
    if not boot:
        return []
    server_pid = tmux_server_pid(abs_sock)
    if server_pid is None:
        return []
    server_ticks = proc_start_ticks(server_pid)
    if server_ticks is None:
        return []

    try:
        panes = list_panes(socket_path=abs_sock)
    except TmuxError:
        return []

    want = agent_kind
    out: list[dict[str, Any]] = []
    for pane in panes:
        try:
            pane_pid_raw = pane_field(pane.pane_id, "#{pane_pid}", socket_path=abs_sock)
            pane_pid = int(pane_pid_raw, 10)
        except (TmuxError, ValueError):
            continue
        processes = pane_processes(pane_pid)
        matched = _find_unique_agent(pane_pid, processes, want_kind=want)
        if matched is None:
            # Incomplete / wrong / multi-agent: emit non-issuable only when filter allows inspection.
            if want:
                continue
            continue
        kind, agent_proc = matched
        pane_ticks = proc_start_ticks(pane_pid)
        agent_ticks = proc_start_ticks(agent_proc.pid)
        exe = proc_exe_identity(agent_proc.pid)
        if pane_ticks is None or agent_ticks is None or exe is None:
            evidence = {
                "hostKey": host_key,
                "uid": uid,
                "bootId": boot,
                "socketPath": abs_sock,
                "socketDev": sock_dev,
                "socketInode": sock_ino,
                "serverPid": str(server_pid),
                "serverStartTicks": server_ticks,
                "paneId": pane.pane_id,
                "panePid": str(pane_pid),
                "paneStartTicks": pane_ticks or "",
                "agentKind": kind,
                "agentPid": str(agent_proc.pid),
                "agentStartTicks": agent_ticks or "",
                "agentExePath": (exe[0] if exe else ""),
                "agentExeDev": (exe[1] if exe else ""),
                "agentExeInode": (exe[2] if exe else ""),
            }
            out.append({
                "identityEvidence": evidence,
                "processState": "DOWN",
                "profileRoot": None,
                "workspaceRoot": None,
                "expectedSession": None,
                "mappingState": "UNMAPPED",
            })
            continue

        exe_path, exe_dev, exe_ino = exe
        workspace = _workspace_root(agent_proc.args, pane.current_path)
        profile_root: str | None = None
        expected_session: str | None = None
        process_state = "UP"
        if kind == "codex":
            profile_root = _codex_profile_root(agent_proc.pid)
            expected_session = _expected_session_for_codex(
                agent_proc.pid,
                profile_root or str(Path.home() / ".codex"),
                pane.pane_id,
            )
        elif kind in {"claude-pro", "claude-team"}:
            profile_root = _claude_profile_root(agent_proc.pid)
            if not profile_root:
                process_state = "DOWN"
            else:
                expected_session = _expected_session_for_claude(
                    agent_proc.pid,
                    profile_root,
                    workspace,
                    pane_pid,
                )
        elif kind == "claude":
            # Missing/unknown CLAUDE_CONFIG_DIR: never guess team vs pro.
            process_state = "DOWN"
        elif kind == "grok":
            profile_root = _grok_profile_root(agent_proc.pid)
            expected_session = _expected_session_for_grok(
                agent_proc.pid,
                profile_root or str(Path.home() / ".grok"),
                pane.pane_id,
            )
        evidence = {
            "hostKey": host_key,
            "uid": uid,
            "bootId": boot,
            "socketPath": abs_sock,
            "socketDev": sock_dev,
            "socketInode": sock_ino,
            "serverPid": str(server_pid),
            "serverStartTicks": server_ticks,
            "paneId": pane.pane_id,
            "panePid": str(pane_pid),
            "paneStartTicks": pane_ticks,
            "agentKind": kind,
            "agentPid": str(agent_proc.pid),
            "agentStartTicks": agent_ticks,
            "agentExePath": exe_path,
            "agentExeDev": exe_dev,
            "agentExeInode": exe_ino,
        }
        out.append({
            "identityEvidence": evidence,
            "processState": process_state,
            "profileRoot": profile_root,
            "workspaceRoot": workspace,
            "expectedSession": expected_session,
            "mappingState": "UNMAPPED",
            "paneModeNormal": pane_mode_is_normal(pane.pane_id, abs_sock),
        })
    return out
