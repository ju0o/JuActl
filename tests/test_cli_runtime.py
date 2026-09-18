"""Slice 6: actl runtime --request-stdin CLI path (no ensure_config side effects)."""
from __future__ import annotations

import io
import json
import uuid
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import actl.cli as cli
from actl.core import runtime


def test_runtime_cli_reserve_acquire_ok(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime, "_journal_root_override", tmp_path)
    sock = str(tmp_path / "sock")
    request = {
        "contractVersion": 1,
        "requestId": str(uuid.uuid4()),
        "operation": "reserve",
        "action": "acquire",
        "runtimeId": "rt1_cli",
        "mode": "MANAGED",
        "expectedContext": {"agentKind": "codex", "workspaceRoot": "/tmp/ws"},
        "serverScope": {"hostKey": "hk-cli", "uid": "1000", "socketPath": sock},
    }
    stdout = io.StringIO()
    with redirect_stdout(stdout):
        code = cli.run_runtime_request_stdin("reserve", stdin_text=json.dumps(request))
    assert code == 0
    lines = [ln for ln in stdout.getvalue().splitlines() if ln.strip()]
    assert len(lines) == 1
    envelope = json.loads(lines[0])
    assert envelope["ok"] is True
    assert envelope["data"]["reservationId"].startswith("rsv_")
    assert envelope["data"]["fence"] == "1"


def test_runtime_cli_bad_contract_version_exit_3(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime, "_journal_root_override", tmp_path)
    request = {
        "contractVersion": 99,
        "requestId": str(uuid.uuid4()),
        "operation": "reserve",
        "action": "acquire",
        "runtimeId": "rt1_bad",
        "mode": "DIRECT",
        "expectedContext": {},
        "serverScope": {"hostKey": "hk", "uid": "1", "socketPath": str(tmp_path / "s")},
    }
    stdout = io.StringIO()
    with redirect_stdout(stdout):
        code = cli.run_runtime_request_stdin("reserve", stdin_text=json.dumps(request))
    assert code == 3
    envelope = json.loads(stdout.getvalue().strip())
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "INVALID_ARGUMENT"


def test_runtime_cli_invalid_json_exit_3(monkeypatch):
    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = cli.run_runtime_request_stdin("reserve", stdin_text="{not-json")
    assert code == 3
    envelope = json.loads(stdout.getvalue().strip())
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "INVALID_ARGUMENT"
    assert "invalid JSON" in stderr.getvalue()


def test_runtime_cli_does_not_create_config(monkeypatch, tmp_path):
    missing_config = tmp_path / "no-such-dir" / "config.json"
    assert not missing_config.exists()
    monkeypatch.setattr(cli, "CONFIG_PATH", missing_config)
    import actl.core.config as config_mod

    monkeypatch.setattr(config_mod, "CONFIG_PATH", missing_config)
    monkeypatch.setattr(runtime, "_journal_root_override", tmp_path)

    def boom_ensure():
        raise AssertionError("ensure_config must not run on runtime path")

    monkeypatch.setattr(cli, "ensure_config", boom_ensure)
    monkeypatch.setattr(config_mod, "ensure_config", boom_ensure)
    monkeypatch.setattr(runtime, "observe_candidates", lambda *a, **k: [])

    request = {
        "contractVersion": 1,
        "requestId": str(uuid.uuid4()),
        "operation": "discover",
        "socketPath": str(tmp_path / "sock"),
        "hostKey": "hk",
        "uid": "1000",
    }
    monkeypatch.setattr(cli.sys, "argv", ["actl", "runtime", "discover", "--request-stdin"])
    stdout = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
        # Feed stdin without permanently replacing sys.stdout used by the runner.
        monkeypatch.setattr(cli.sys, "stdin", io.StringIO(json.dumps(request)))
        try:
            cli.main()
        except SystemExit as exc:
            assert exc.code == 0
        else:
            raise AssertionError("expected SystemExit")
    assert not missing_config.exists()
    envelope = json.loads(stdout.getvalue().strip())
    assert envelope["ok"] is True
    assert envelope["data"]["candidates"] == []


def test_runtime_cli_operation_mismatch_exit_3(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime, "_journal_root_override", tmp_path)
    request = {
        "contractVersion": 1,
        "requestId": str(uuid.uuid4()),
        "operation": "status",
        "runtimeId": "rt1_x",
        "socketPath": str(tmp_path / "sock"),
        "hostKey": "hk",
        "uid": "1",
    }
    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = cli.run_runtime_request_stdin("reserve", stdin_text=json.dumps(request))
    assert code == 3
    envelope = json.loads(stdout.getvalue().strip())
    assert envelope["error"]["code"] == "INVALID_ARGUMENT"
