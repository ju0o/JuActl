import os
import shutil
import subprocess
import sys


def test_runner_isolates_default_state_paths(tmp_path):
    fake_home = tmp_path / "home"
    real_state = fake_home / ".local/state/actl"
    real_state.mkdir(parents=True)
    (real_state / "audit.jsonl").write_text("sentinel\n", encoding="utf-8")
    (real_state / "state.json").write_text("sentinel\n", encoding="utf-8")

    root = tmp_path / "runner"
    tests = root / "tests"
    tests.mkdir(parents=True)
    shutil.copy2(os.path.join(os.path.dirname(__file__), "run_tests.py"), tests / "run_tests.py")
    (tests / "test_probe.py").write_text(
        "import os\n"
        "from pathlib import Path\n"
        "\n"
        "def test_paths_are_temporary():\n"
        "    assert Path(os.environ['ACTL_AUDIT_PATH']).parent != Path.home() / '.local/state/actl'\n"
        "    assert Path(os.environ['ACTL_STATE_PATH']).parent != Path.home() / '.local/state/actl'\n"
        "    assert Path.home() != Path(os.environ['ORIGINAL_HOME'])\n"
        "    (Path.home() / '.local/state/actl').mkdir(parents=True)\n"
        "    (Path.home() / '.local/state/actl/runtime.sqlite3').write_text('test')\n"
        "\n",
        encoding="utf-8",
    )

    env = {**os.environ, "HOME": str(fake_home), "ORIGINAL_HOME": str(fake_home)}
    result = subprocess.run(
        [sys.executable, str(tests / "run_tests.py")],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert (real_state / "audit.jsonl").read_text(encoding="utf-8") == "sentinel\n"
    assert (real_state / "state.json").read_text(encoding="utf-8") == "sentinel\n"
    assert not (real_state / "runtime.sqlite3").exists()
