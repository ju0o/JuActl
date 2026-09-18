#!/usr/bin/env python3
import sys
sys.path.insert(0, "src")
from actl.core.config import load_config
from actl.core.validation import validate_target
from actl.core.registry import AGENTS
from actl.agents.extract import extract_last_response

config = load_config()

for agent_name in ["opencode", "grok"]:
    print(f"\n=== {agent_name} extraction ===")
    try:
        target = validate_target(agent_name, config.get("agents",{}).get(agent_name,{}).get("target","")).pane_id or config.get("agents",{}).get(agent_name,{}).get("target")
        print(f"target: {target}")
    except Exception as e:
        target = config.get("agents",{}).get(agent_name,{}).get("target")
        print(f"target(err): {target} ({e})")

    try:
        result = extract_last_response(agent_name, target, config)
        print(f"source: {result.source}")
        print(f"confidence: {result.confidence}")
        print(f"text length: {len(result.text) if result.text else 0}")
        print(f"preview:\n{result.text[:500] if result.text else '(None)'}")
    except Exception as e:
        import traceback
        traceback.print_exc()
