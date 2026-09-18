#!/usr/bin/env python3
import json, sys
sys.path.insert(0, "src")
from actl.core.config import load_config
from actl.core.validation import validate_target, pane_processes
from actl.core.tmux import pane_field
from actl.agents.opencode import resolve_opencode, stored_session_id, live_cmdline_session, _opencode_pids, _db_live_session, session_row
from actl.agents.grok import resolve_grok, _grok_pid
from pathlib import Path

config = load_config()

agents = config.get("agents", {})

for agent_name in ["opencode", "grok"]:
    print(f"\n=== {agent_name} ===")
    entry = agents.get(agent_name, {})
    target = entry.get("target", "UNMAPPED")
    print(f"config target: {target}")
    val = validate_target(agent_name, target)
    print(f"validation: state={val.state} pid={val.agent_pid} cmd={val.command} path={val.path}")

    # List processes in pane
    if val.agent_pid:
        pids = _opencode_pids(val.agent_pid) if agent_name == "opencode" else []
        procs = pane_processes(val.agent_pid)
        for p in procs:
            exe = p.args.split()[0] if p.args else "?"
            print(f"  proc: pid={p.pid} exe={exe} args={p.args!r}")

    if agent_name == "opencode":
        spec_data = Path("~/.local/share/opencode").expanduser().resolve(strict=False)
        expected = stored_session_id(config)
        print(f"stored_session_id: {expected}")
        resolution = resolve_opencode(spec_data, target, val.agent_pid, expected)
        print(f"resolution.session_id: {resolution.session_id}")
        print(f"resolution.live_session_id: {resolution.live_session_id}")
        print(f"resolution.match_method: {resolution.match_method}")
        print(f"resolution.confidence: {resolution.confidence}")
        print(f"resolution.detail: {resolution.detail}")

    if agent_name == "grok":
        from actl.agents.grok import extract_grok
        spec_data = Path("~/.grok").expanduser().resolve(strict=False)
        resolution = resolve_grok(spec_data, target)
        print(f"resolution.session_id: {resolution.session_id}")
        print(f"resolution.session_dir: {resolution.session_dir}")
        print(f"resolution.match_method: {resolution.match_method}")
        print(f"resolution.confidence: {resolution.confidence}")
        print(f"resolution.detail: {resolution.detail}")
