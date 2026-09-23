import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "e2e_disposable.sh"


def fake_tmux(tmp_path: Path, *, existing: bool = False) -> Path:
    state = tmp_path / "session"
    if existing:
        state.write_text("existing", encoding="utf-8")
    fake = tmp_path / "tmux"
    fake.write_text(
        "#!/bin/sh\n"
        "state=${FAKE_TMUX_STATE}\n"
        "case \"$1\" in\n"
        "  has-session) test -e \"$state\" ;;\n"
        "  new-session) touch \"$state\" ;;\n"
        "  kill-session) echo kill >> \"$state.log\"; rm -f \"$state\" ;;\n"
        "  *) exit 1 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    return state


def run_script(tmp_path: Path, *args: str, existing: bool = False) -> subprocess.CompletedProcess[str]:
    state = fake_tmux(tmp_path, existing=existing)
    env = os.environ.copy()
    env["PATH"] = f"{tmp_path}:{env['PATH']}"
    env["FAKE_TMUX_STATE"] = str(state)
    return subprocess.run(["bash", str(SCRIPT), *args], cwd=ROOT, env=env, text=True, capture_output=True)


def test_argument_parsing():
    result = subprocess.run(["bash", str(SCRIPT), "--bad"], cwd=ROOT, text=True, capture_output=True)
    assert result.returncode == 2
    assert "Usage: " in result.stderr


def test_refuses_existing_session(tmp_path):
    result = run_script(tmp_path, existing=True)
    assert result.returncode == 1
    assert "session already exists" in result.stderr
    assert not (tmp_path / "session.log").exists()


def test_cleanup_trap_kills_created_session(tmp_path):
    result = run_script(tmp_path)
    assert result.returncode != 0
    assert not (tmp_path / "session").exists()
    assert (tmp_path / "session.log").read_text(encoding="utf-8").splitlines() == ["kill"]


def test_stub_agent_contract():
    source = (ROOT / "tests" / "fixtures" / "stub_agent.py").read_text(encoding="utf-8")
    assert "STUB_PROMPT>" in source
    assert "RESULT::{prompt}" in source
