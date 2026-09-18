import io
import json
import shutil
from contextlib import redirect_stdout

from actl import cli


def test_doctor_json_is_machine_readable_without_clipboard_write(monkeypatch):
    monkeypatch.setattr(cli.sys, "platform", "win32")
    monkeypatch.setattr(cli.sys, "argv", ["actl", "doctor", "--json"])
    monkeypatch.setattr(shutil, "which", lambda _: "available")
    monkeypatch.setattr(cli, "load_config", lambda: {"agents": {}})
    out = io.StringIO()
    with redirect_stdout(out):
        code = cli._doctor(json_output=True)

    assert code == 0
    payload = json.loads(out.getvalue())
    assert payload["ok"] is True
    assert all("name" in check and "ok" in check for check in payload["checks"])


def test_doctor_json_is_safe_on_legacy_windows_console(monkeypatch):
    class Cp1252Stream(io.StringIO):
        def write(self, value):
            value.encode("cp1252")
            return super().write(value)

    monkeypatch.setattr(cli.sys, "platform", "win32")
    monkeypatch.setattr(shutil, "which", lambda _: "available")
    monkeypatch.setattr(cli, "load_config", lambda: {"agents": {}})
    out = Cp1252Stream()
    with redirect_stdout(out):
        assert cli._doctor(json_output=True) == 0
    assert json.loads(out.getvalue())["ok"] is True


def test_status_json_is_one_machine_readable_array(monkeypatch):
    monkeypatch.setattr(cli, "AGENTS", {"commandcode": cli.AGENTS["commandcode"]})
    monkeypatch.setattr(cli, "agent_status", lambda *_: {
        "agent": "CommandCode", "target": "%12", "pane": "UP",
        "command": "cmd", "path": "/tmp", "binary": "cmd",
    })
    out = io.StringIO()
    with redirect_stdout(out):
        cli._print_status({}, "commandcode", json_output=True)
    payload = json.loads(out.getvalue())
    assert payload == [{"id": "commandcode", "agent": "CommandCode", "target": "%12",
                       "pane": "UP", "command": "cmd", "path": "/tmp", "binary": "cmd"}]


def test_status_command_alias_accepts_agent_and_json(monkeypatch):
    monkeypatch.setattr(cli.sys, "argv", ["actl", "status", "commandcode", "--json"])
    monkeypatch.setattr(cli, "load_config", lambda: {"agents": {}})
    monkeypatch.setattr(cli, "agent_status", lambda *_: {
        "agent": "CommandCode", "target": "%12", "pane": "UP",
        "command": "cmd", "path": "/tmp", "binary": "cmd",
    })
    out = io.StringIO()
    with redirect_stdout(out):
        cli.main()
    assert json.loads(out.getvalue())[0]["id"] == "commandcode"
