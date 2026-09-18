from __future__ import annotations

from actl.agents.claude import extract_claude
from actl.agents.cline import extract_cline, resolve_cline
from actl.agents.codex import extract_codex, resolve_codex
from actl.agents.commandcode import extract_commandcode, resolve_commandcode
from actl.agents.cursor import extract_cursor, resolve_cursor
from actl.agents.grok import extract_grok, resolve_grok
from actl.agents.opencode import extract_opencode, resolve_opencode
from actl.core.models import CopyResult
from actl.core.registry import AGENTS
from actl.core.validation import validate_target


def extract_last_response(agent: str, target: str, config: dict | None = None) -> CopyResult:
    spec = AGENTS[agent]
    if agent in {"claude-team", "claude-pro"}:
        # tmux capture could expose a user prompt and cannot prove profile or
        # project correlation, so Claude fails closed instead of falling back.
        from actl.core.tmux import pane_field

        pane_path = pane_field(target, "#{pane_current_path}")
        raw_pane_pid = pane_field(target, "#{pane_pid}")
        try:
            pane_pid = int(raw_pane_pid)
        except ValueError:
            pane_pid = None
        return extract_claude(spec.data_dirs[0], pane_path, pane_pid)
    if agent == "cursor":
        validation = validate_target(agent, target)
        result = extract_cursor(resolve_cursor(spec.data_dirs[0], target, validation.agent_pid if validation.valid else None))
        if result:
            return result
        # Cursor is never allowed to fall back to a pane scrape: that would
        # bypass the process/session/store correlation above.
        return CopyResult(
            None,
            "cursor-unresolved",
            "none",
            "Could not confidently identify the active Cursor conversation. Nothing copied.",
        )
    if agent == "codex":
        validation = validate_target(agent, target)
        if not validation.valid:
            return CopyResult(None, "codex-unresolved", "none", validation.detail)
        resolution = resolve_codex(spec.data_dirs[0], target, validation.agent_pid)
        result = extract_codex(resolution)
        if result:
            return result
        return CopyResult(
            None,
            "codex-unresolved",
            "none",
            resolution.detail or "No completed assistant AgentMessage in matched Codex rollout",
        )
    if agent == "grok":
        resolution = resolve_grok(spec.data_dirs[0], target)
        result = extract_grok(resolution)
        if result:
            return result
        return CopyResult(
            None,
            "grok-unresolved",
            "none",
            resolution.detail or "No exact live Grok session correlation; pane capture and newest-file fallbacks are disabled.",
        )
    if agent == "cline":
        validation = validate_target(agent, target)
        resolution = resolve_cline(spec.data_dirs[0], target, validation.agent_pid)
        result = extract_cline(resolution)
        if result:
            return result
        return CopyResult(None, "cline-unresolved", "none", resolution.detail)
    if agent == "opencode":
        from actl.agents.opencode import stored_session_id

        validation = validate_target(agent, target)
        resolution = resolve_opencode(
            spec.data_dirs[0],
            target,
            validation.agent_pid if validation.valid else None,
            stored_session_id(config),
        )
        result = extract_opencode(resolution)
        if result:
            return result
        return CopyResult(None, "opencode-blocked", "none", resolution.detail)
    if agent == "commandcode":
        validation = validate_target(agent, target)
        resolution = resolve_commandcode(
            spec.data_dirs[0],
            target,
            validation.agent_pid if validation.valid else None,
        )
        result = extract_commandcode(resolution)
        if result:
            return result
        return CopyResult(None, "commandcode-unresolved", "none", resolution.detail)
    return CopyResult(None, f"{agent}-unresolved", "none", "No exact live session correlation; pane capture and newest-file fallbacks are disabled.")
