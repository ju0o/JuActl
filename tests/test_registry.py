from actl.core.registry import AGENTS, ALIASES, registry_issues, resolve_agent


def test_aliases():
    assert resolve_agent("ct") == "claude-team"
    assert resolve_agent("team") == "claude-team"
    assert resolve_agent("cp") == "claude-pro"
    assert resolve_agent("pro") == "claude-pro"
    assert resolve_agent("oc") == "opencode"
    assert resolve_agent("cx") == "codex"
    assert resolve_agent("cu") == "cursor"
    assert resolve_agent("cmd") == "commandcode"
    assert resolve_agent("cl") == "cline"
    assert resolve_agent("gr") == "grok"


def test_eight_agents_and_unique_aliases():
    assert len(AGENTS) == 8
    assert len(ALIASES) == sum(len(spec.aliases) for spec in AGENTS.values())


def test_claude_storage_isolation():
    assert AGENTS["claude-team"].data_dirs != AGENTS["claude-pro"].data_dirs
    assert str(AGENTS["claude-team"].data_dirs[0]).endswith(".claude-team")
    assert str(AGENTS["claude-pro"].data_dirs[0]).endswith(".claude-pro")


def test_adapter_registry_is_structurally_complete():
    assert registry_issues() == []
