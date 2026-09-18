from __future__ import annotations

from pathlib import Path

from actl.core.models import AgentSpec

HOME = Path.home()

AGENTS: dict[str, AgentSpec] = {
    "claude-team": AgentSpec(
        "claude-team", "Claude Team", ("claude-team", "ct", "team"), ("~/.local/bin/claude", "claude"),
        (HOME / ".claude-team",),
    ),
    "claude-pro": AgentSpec(
        "claude-pro", "Claude Pro", ("claude-pro", "cp", "pro"), ("~/.local/bin/claude", "claude"),
        (HOME / ".claude-pro",),
    ),
    "opencode": AgentSpec(
        "opencode", "OpenCode", ("opencode", "oc"), ("/usr/local/bin/opencode", "opencode"),
        (HOME / ".local/share/opencode", HOME / ".config/opencode"),
    ),
    "codex": AgentSpec(
        "codex", "Codex", ("codex", "cx"), ("/usr/local/bin/codex", "codex"),
        (HOME / ".codex",),
    ),
    "cursor": AgentSpec(
        "cursor", "Cursor", ("cursor", "cu"), ("cursor-agent", "agent", "cursor"),
        (HOME / ".cursor/chats",),
    ),
    "commandcode": AgentSpec(
        "commandcode", "CommandCode", ("commandcode", "cmd"), ("/usr/local/bin/cmd", "cmd"),
        (HOME / ".commandcode", HOME / ".config/commandcode", HOME / ".local/share/commandcode"),
        official_copy_command="/copy",
    ),
    "cline": AgentSpec(
        "cline", "Cline", ("cline", "cl"), ("/usr/local/bin/cline", "cline"),
        (HOME / ".cline", HOME / ".config/cline", HOME / ".local/share/cline"),
    ),
    "grok": AgentSpec(
        "grok", "Grok", ("grok", "gr"), ("~/.grok/bin/grok", "grok"),
        (HOME / ".grok", HOME / ".config/grok", HOME / ".local/share/grok"),
    ),
}

ALIASES = {alias: name for name, spec in AGENTS.items() for alias in spec.aliases}

if len(ALIASES) != sum(len(spec.aliases) for spec in AGENTS.values()):
    raise RuntimeError("actl agent aliases must be unique")


def resolve_agent(value: str) -> str | None:
    return ALIASES.get(value.strip().lower())


def registry_issues() -> list[str]:
    """Return structural adapter-registry errors without touching runtimes."""
    issues: list[str] = []
    for name, spec in AGENTS.items():
        if spec.name != name:
            issues.append(f"{name}: spec.name mismatch")
        if not spec.display_name or not spec.aliases:
            issues.append(f"{name}: missing display name or aliases")
        if not spec.binary_candidates:
            issues.append(f"{name}: missing binary candidates")
        if not spec.data_dirs:
            issues.append(f"{name}: missing storage roots")
    return issues
