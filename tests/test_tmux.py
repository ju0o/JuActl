from pathlib import Path

from actl.core import tmux


def test_multiline_uses_one_bracketed_raw_buffer_and_one_enter(monkeypatch):
    calls = []
    payloads = []

    def fake_run(args, **kwargs):
        calls.append(args)
        if args[1] == "load-buffer":
            payloads.append(Path(args[-1]).read_text(encoding="utf-8"))
        class Result:
            stdout = ""
        return Result()

    monkeypatch.setattr(tmux, "target_exists", lambda target, socket_path=None: True)
    monkeypatch.setattr(tmux, "_run", fake_run)
    prompt = 'line1\n한글\n$HOME\n`backtick`\n"quote"\n```python\nprint("hello")\n```'
    tmux.send_prompt("0:0.1", prompt)
    assert payloads == [prompt]
    paste = [c for c in calls if c[1] == "paste-buffer"]
    enter = [c for c in calls if c[1] == "send-keys"]
    assert len(paste) == 1
    assert {"-p", "-r", "-d"}.issubset(paste[0])
    assert enter == [["tmux", "send-keys", "-t", "0:0.1", "Enter"]]


def test_missing_target_sends_nothing(monkeypatch):
    monkeypatch.setattr(tmux, "target_exists", lambda target, socket_path=None: False)
    try:
        tmux.send_prompt("missing:0.0", "do not send")
    except tmux.TmuxError:
        pass
    else:
        raise AssertionError("expected TmuxError")


def test_tmux_output_replaces_invalid_title_bytes(monkeypatch):
    captured = {}
    monkeypatch.setattr(tmux.subprocess, "run", lambda *args, **kwargs: captured.update(kwargs) or type("Result", (), {"stdout": ""})())
    tmux._run(["tmux", "list-panes"])
    assert captured["errors"] == "replace"


def test_windows_remote_format_uses_waited_native_ssh(monkeypatch):
    monkeypatch.setattr(tmux.os, "name", "nt")
    monkeypatch.setattr(tmux, "REMOTE_SSH_TARGET", "asus")
    args = tmux._remote_args(["tmux", "list-panes", "-F", "#{pane_id}	#{session_name}"])
    assert args == [
        "ssh", "-n", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "asus",
        "tmux", "list-panes", "-F", "'#{pane_id}\t#{session_name}'",
    ]


def test_socket_path_passed_as_dash_s(monkeypatch):
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        class Result:
            stdout = ""
        return Result()

    monkeypatch.setattr(tmux, "target_exists", lambda target, socket_path=None: True)
    monkeypatch.setattr(tmux, "_run", fake_run)
    monkeypatch.setattr(tmux.subprocess, "run", lambda *a, **k: type("Result", (), {"returncode": 0, "stdout": ""})())
    tmux.send_prompt("0:0.1", "hi", socket_path="/tmp/actl-proof.sock")
    assert calls
    for args in calls:
        assert args[0] == "tmux"
        assert args[1] == "-S"
        assert args[2] == "/tmp/actl-proof.sock"
    assert tmux._tmux_base(None) == ["tmux"]
    assert tmux._tmux_base("/tmp/x.sock") == ["tmux", "-S", "/tmp/x.sock"]


def test_tmux_names_are_renamed_with_the_expected_target(monkeypatch):
    calls = []
    monkeypatch.setattr(tmux, "_run", lambda args: calls.append(args))
    tmux.rename_session("main", "Main PC")
    tmux.rename_window("main:1", "Agent Desk")
    tmux.rename_pane("%7", "Codex")
    assert calls == [
        ["tmux", "rename-session", "-t", "main", "Main PC"],
        ["tmux", "rename-window", "-t", "main:1", "Agent Desk"],
        ["tmux", "select-pane", "-t", "%7", "-T", "Codex"],
    ]


def test_tmux_names_reject_empty_or_multiline_values():
    for rename in (tmux.rename_session, tmux.rename_window, tmux.rename_pane):
        try:
            rename("target", "bad\nname")
        except ValueError:
            pass
        else:
            raise AssertionError("expected tmux name validation")


def test_tmux_create_commands_preserve_session_window_pane_hierarchy(monkeypatch):
    calls = []
    monkeypatch.setattr(tmux, "_run", lambda args: calls.append(args))
    tmux.create_session("desk")
    tmux.create_window("desk", "agents")
    tmux.split_pane("desk:agents.0")
    assert calls == [
        ["tmux", "new-session", "-d", "-s", "desk"],
        ["tmux", "new-window", "-d", "-t", "desk", "-n", "agents"],
        ["tmux", "split-window", "-d", "-h", "-t", "desk:agents.0"],
    ]


def test_send_prompt_staged_records_load_paste_enter(monkeypatch):
    calls = []

    def fake_run(args, **kwargs):
        calls.append(list(args))
        class Result:
            stdout = ""
        return Result()

    monkeypatch.setattr(tmux, "target_exists", lambda target, socket_path=None: True)
    monkeypatch.setattr(tmux, "_run", fake_run)
    monkeypatch.setattr(tmux.subprocess, "run", lambda *a, **k: type("Result", (), {"returncode": 0, "stdout": ""})())
    result = tmux.send_prompt_staged("%1", "one\ntwo", socket_path="/tmp/s.sock")
    assert result["ok"] is True
    assert result["completedStages"] == ["verify_target", "load_buffer", "paste_buffer", "enter"]
    assert result["sideEffect"] == tmux.SIDE_EFFECT_OBSERVED
    assert result["deliveryDisposition"] == "TRANSPORT_SENT"
    cmds = [c[3] for c in calls]  # after tmux -S sock
    assert cmds == ["load-buffer", "paste-buffer", "send-keys"]
    assert calls.count([c for c in calls if c[3] == "load-buffer"][0]) == 1
    assert sum(1 for c in calls if c[3] == "paste-buffer") == 1
    assert sum(1 for c in calls if c[3] == "send-keys") == 1


def test_send_prompt_staged_mid_failure_after_paste_is_ambiguous(monkeypatch):
    def fake_run(args, **kwargs):
        cmd = args[1] if args[1] != "-S" else args[3]
        if cmd == "send-keys":
            raise tmux.TmuxError("enter failed")
        class Result:
            stdout = ""
        return Result()

    monkeypatch.setattr(tmux, "target_exists", lambda target, socket_path=None: True)
    monkeypatch.setattr(tmux, "_run", fake_run)
    monkeypatch.setattr(tmux.subprocess, "run", lambda *a, **k: type("Result", (), {"returncode": 0, "stdout": ""})())
    result = tmux.send_prompt_staged("%1", "payload")
    assert result["ok"] is False
    assert "paste_buffer" in result["completedStages"]
    assert result["failedStage"] == "enter"
    assert result["deliveryDisposition"] == "DELIVERY_AMBIGUOUS"
    assert result["sideEffect"] == tmux.SIDE_EFFECT_OBSERVED


def test_pre_send_hook_blocks_input(monkeypatch):
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        class Result:
            stdout = ""
        return Result()

    monkeypatch.setattr(tmux, "target_exists", lambda target, socket_path=None: True)
    monkeypatch.setattr(tmux, "_run", fake_run)

    def deny():
        raise RuntimeError("writer busy")

    result = tmux.send_prompt_staged("%1", "nope", pre_send_hook=deny)
    assert result["ok"] is False
    assert result["sideEffect"] == tmux.SIDE_EFFECT_NONE
    assert calls == []
    assert result["failedStage"] == "pre_send_hook"


def test_interrupt_ctrl_c_once(monkeypatch):
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        class Result:
            stdout = ""
        return Result()

    monkeypatch.setattr(tmux, "target_exists", lambda target, socket_path=None: True)
    monkeypatch.setattr(tmux, "_run", fake_run)
    result = tmux.interrupt_ctrl_c("%3", socket_path="/tmp/i.sock")
    assert result["ok"] is True
    assert calls == [["tmux", "-S", "/tmp/i.sock", "send-keys", "-t", "%3", "C-c"]]
