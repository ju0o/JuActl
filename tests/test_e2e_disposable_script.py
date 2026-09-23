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


def fake_ssh(tmp_path: Path) -> tuple[Path, Path]:
    state = tmp_path / "remote-session"
    log = tmp_path / "ssh.log"
    actl = tmp_path / "remote-actl"
    actl.write_text(
        "#!/bin/sh\n"
        "echo \"actl $*\" >> \"$FAKE_SSH_LOG\"\n"
        "case \"$1\" in\n"
        "  send) cat >/dev/null; echo 'sent to commandcode' ;;\n"
        "  copy) echo 'RESULT::ACTL_E2E_PROBE' ;;\n"
        "  *) exit 1 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    actl.chmod(0o755)
    fake = tmp_path / "ssh"
    fake.write_text(
        "#!/bin/sh\n"
        "shift\n"
        "command=$(printf '%s' \"$*\" | sed \"s|~/.local/bin/actl|$FAKE_REMOTE_ACTL|g\")\n"
        "bash -c \"$command\"\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    tmux = tmp_path / "tmux"
    tmux.write_text(
        "#!/bin/sh\n"
        "state=\"$FAKE_REMOTE_SESSION\"\n"
        "case \"$1\" in\n"
        "  has-session) test -e \"$state\" ;;\n"
        "  new-session) touch \"$state\" ;;\n"
        "  capture-pane) echo 'STUB_PROMPT>' ;;\n"
        "  kill-session) echo kill >> \"$state.log\"; rm -f \"$state\" ;;\n"
        "  *) exit 1 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    tmux.chmod(0o755)
    return state, log


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


def test_concurrent_runs_use_unique_sessions_with_same_second(tmp_path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    tmux = tmp_path / "tmux"
    tmux.write_text(
        "#!/bin/sh\n"
        "state=\"$FAKE_TMUX_SESSIONS\"\n"
        "name() {\n"
        "  case \"$1\" in\n"
        "    has-session|kill-session) printf '%s' \"$3\" ;;\n"
        "    new-session) printf '%s' \"$4\" ;;\n"
        "  esac\n"
        "}\n"
        "case \"$1\" in\n"
        "  has-session) test -e \"$state/$(name \"$@\")\" ;;\n"
        "  new-session) mkdir \"$state/$(name \"$@\")\" || exit 2; echo \"$(name \"$@\")\" >> \"$state.log\"; exit 1 ;;\n"
        "  kill-session) rm -f \"$state/$(name \"$@\")\" ;;\n"
        "  *) exit 1 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    tmux.chmod(0o755)
    date = tmp_path / "date"
    date.write_text(
        "#!/bin/sh\n"
        "case \"$1\" in\n"
        "  +%s|+%s%N) echo 1700000000000000000 ;;\n"
        "  *) /bin/date \"$@\" ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    date.chmod(0o755)
    env = os.environ.copy()
    env.update(PATH=f"{tmp_path}:{env['PATH']}", FAKE_TMUX_SESSIONS=str(sessions))
    runs = [
        subprocess.Popen(["bash", str(SCRIPT)], cwd=ROOT, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE),
        subprocess.Popen(["bash", str(SCRIPT)], cwd=ROOT, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE),
    ]
    results = [run.communicate() for run in runs]

    assert all(run.returncode != 0 for run in runs)
    names = (tmp_path / "sessions.log").read_text(encoding="utf-8").splitlines()
    assert len(names) == 2
    assert len(set(names)) == 2
    assert all("." not in name for name in names)


def test_ssh_reaches_send_and_copy_with_remote_bash(tmp_path):
    state, log = fake_ssh(tmp_path)
    env = os.environ.copy()
    env.update(
        PATH=f"{tmp_path}:{env['PATH']}",
        FAKE_REMOTE_SESSION=str(state),
        FAKE_REMOTE_ACTL=str(tmp_path / "remote-actl"),
        FAKE_SSH_LOG=str(log),
    )
    result = subprocess.run(
        ["bash", str(SCRIPT), "--ssh", "asus"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    assert "actl send commandcode" in log.read_text(encoding="utf-8")
    assert "actl copy commandcode --print" in log.read_text(encoding="utf-8")
    assert not state.exists()


def test_stub_agent_contract():
    source = (ROOT / "tests" / "fixtures" / "stub_agent.py").read_text(encoding="utf-8")
    assert "STUB_PROMPT>" in source
    assert "RESULT::{prompt}" in source
