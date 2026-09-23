from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "e2e_disposable.sh"


def test_e2e_script_contract():
    source = SCRIPT.read_text(encoding="utf-8")
    assert "Usage: $0 [--ssh HOST]" in source
    assert "actl-e2e-$(date +%s)" in source
    assert "tmux has-session -t \"$session\"" in source
    assert "tmux kill-session -t \"$session\"" in source
    assert "rm -rf \"$config_dir\"" in source
    assert "send commandcode" in source
    assert "copy commandcode --print" in source


def test_stub_agent_contract():
    source = (ROOT / "tests" / "fixtures" / "stub_agent.py").read_text(encoding="utf-8")
    assert "STUB_PROMPT>" in source
    assert "RESULT::{prompt}" in source
