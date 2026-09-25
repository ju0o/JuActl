"""§6-5 / V08: disposable tmux socket transport with a non-Agent receiver.

Uses real /usr/bin/tmux and a short /tmp socket path. Never targets the default
tmux server, operational panes, or Agent sessions. Cleans up only resources it
creates.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from actl.core import tmux

REAL_TMUX = "/usr/bin/tmux"
EVIDENCE_DIR = Path(__file__).resolve().parents[2] / "test-artifacts"
PROMPT = 'line1\n한글\n$HOME\n`backtick`\n"quote"\n```python\nprint("hello")\n```'


def _force_real_tmux(monkeypatch) -> None:
    """Ignore PATH stubs (baseline-bin/tmux) for this disposable-socket proof."""

    def base(socket_path: str | None = None) -> list[str]:
        if socket_path:
            return [REAL_TMUX, "-S", socket_path]
        # §6-5 must never fall back to default socket in this module.
        raise AssertionError("disposable transport test refused default tmux socket")

    monkeypatch.setattr(tmux, "_tmux_base", base)


def _default_inventory(runner=None) -> dict:
    """Read-only snapshot of the default server (no input)."""
    run = runner or subprocess.run
    panes = run(
        [REAL_TMUX, "list-panes", "-a", "-F", "#{pane_id}\t#{session_name}:#{window_index}.#{pane_index}\t#{pane_current_command}"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    buffers = run(
        [REAL_TMUX, "list-buffers", "-F", "#{buffer_name}"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return {
        "panesExit": panes.returncode,
        "panes": [ln for ln in panes.stdout.splitlines() if ln.strip()],
        "bufferNames": [ln for ln in buffers.stdout.splitlines() if ln.strip()],
    }


def _start_receiver(work: Path, name: str) -> tuple[str, Path, str]:
    sock = work / f"{name}.sock"
    out = work / f"{name}.recv.bin"
    # Unbuffered receiver so bytes are visible before kill-server.
    # Keep the pane alive after stdin EOF for orderly cleanup.
    cmd = f"stdbuf -o0 cat > {out}; sleep 60"
    subprocess.run(
        [REAL_TMUX, "-S", str(sock), "new-session", "-d", "-s", name, "-n", "recv", cmd],
        check=True,
        capture_output=True,
        text=True,
    )
    time.sleep(0.15)
    pane = subprocess.check_output(
        [REAL_TMUX, "-S", str(sock), "list-panes", "-a", "-F", "#{pane_id}"],
        text=True,
    ).splitlines()[0].strip()
    return str(sock), out, pane


def _kill_socket(sock: str) -> None:
    subprocess.run([REAL_TMUX, "-S", sock, "kill-server"], capture_output=True)


def test_v08_disposable_socket_utf8_multiline_one_paste_one_enter(monkeypatch):
    assert Path(REAL_TMUX).is_file()
    _force_real_tmux(monkeypatch)

    before = _default_inventory()
    work = Path(tempfile.mkdtemp(prefix="actl65-", dir="/tmp"))
    sock_a = out_a = pane_a = None
    sock_b = out_b = pane_b = None
    transport_calls: list[list[str]] = []
    receipt = None
    try:
        sock_a, out_a, pane_a = _start_receiver(work, "a")
        sock_b, out_b, pane_b = _start_receiver(work, "b")

        real_run = tmux._run

        def counting_run(args, **kwargs):
            transport_calls.append(list(args))
            assert args[0] == REAL_TMUX
            assert args[1] == "-S"
            return real_run(args, **kwargs)

        monkeypatch.setattr(tmux, "_run", counting_run)
        # delete-buffer / target_exists use subprocess.run; require explicit -S.
        real_sub_run = tmux.subprocess.run

        def guarded_sub_run(args, **kwargs):
            if args and args[0] == REAL_TMUX:
                assert args[1] == "-S", args
            return real_sub_run(args, **kwargs)

        monkeypatch.setattr(tmux.subprocess, "run", guarded_sub_run)
        # Also used for post-check inventory so default-server reads bypass the -S guard.
        inventory_runner = real_sub_run

        receipt = tmux.send_prompt_staged(pane_a, PROMPT, socket_path=sock_a, press_enter=True)
        assert receipt["ok"] is True
        assert receipt["completedStages"] == ["verify_target", "load_buffer", "paste_buffer", "enter"]
        assert receipt["deliveryDisposition"] == "TRANSPORT_SENT"

        primary_calls = list(transport_calls)
        assert primary_calls
        for c in primary_calls:
            assert c[2] == sock_a

        # Wait for cat to flush after Enter closes the line / EOF path.
        deadline = time.time() + 2.0
        data = b""
        while time.time() < deadline:
            if out_a.exists():
                data = out_a.read_bytes()
                if data:
                    break
            time.sleep(0.05)

        expected = PROMPT.encode("utf-8") + b"\n"
        assert data == expected, (data, expected)
        assert hashlib.sha256(data).hexdigest() == hashlib.sha256(expected).hexdigest()

        paste_cmds = [c for c in primary_calls if len(c) > 3 and c[3] == "paste-buffer"]
        enter_cmds = [c for c in primary_calls if len(c) > 3 and c[3] == "send-keys" and c[-1] == "Enter"]
        load_cmds = [c for c in primary_calls if len(c) > 3 and c[3] == "load-buffer"]
        assert len(load_cmds) == 1
        assert len(paste_cmds) == 1
        assert len(enter_cmds) == 1

        # Same pane-id string on a different disposable socket must not touch socket A.
        before_a = data
        cross = tmux.send_prompt_staged(pane_a, "LEAK-SHOULD-NOT-HIT-A", socket_path=sock_b, press_enter=True)
        # pane_a may or may not exist on B; if B also has %N, send goes only to B.
        time.sleep(0.2)
        after_a = out_a.read_bytes() if out_a.exists() else b""
        assert after_a == before_a
        if cross.get("ok"):
            # Input landed on B's pane with the same id, not on A.
            bdata = out_b.read_bytes() if out_b.exists() else b""
            assert b"LEAK-SHOULD-NOT-HIT-A" in bdata
        else:
            # Or target missing on B — still proves A unchanged.
            assert cross.get("failedStage") in {"verify_target", "paste_buffer", "enter", "load_buffer"}

        after = _default_inventory(inventory_runner)
        assert after["panes"] == before["panes"], "default server pane inventory changed"
        # No new actl-buffer-* leaked onto the default server.
        new_bufs = set(after["bufferNames"]) - set(before["bufferNames"])
        assert not any(name.startswith("actl-buffer-") for name in new_bufs)

        evidence = {
            "case": "V08_disposable_socket_transport",
            "section": "6-5",
            "socketPathA": sock_a,
            "socketPathB": sock_b,
            "paneA": pane_a,
            "paneB": pane_b,
            "promptSha256": hashlib.sha256(PROMPT.encode("utf-8")).hexdigest(),
            "receivedSha256": hashlib.sha256(data).hexdigest(),
            "receivedByteLength": len(data),
            "pasteCount": len(paste_cmds),
            "enterCount": len(enter_cmds),
            "loadCount": len(load_cmds),
            "receipt": receipt,
            "crossSocketAUnchanged": True,
            "defaultInventoryUnchanged": True,
            "defaultPanesBefore": before["panes"],
            "defaultPanesAfter": after["panes"],
            "tmuxBinary": REAL_TMUX,
            "ok": True,
        }
        EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
        (EVIDENCE_DIR / "section65-v08-transport.json").write_text(
            json.dumps(evidence, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    finally:
        if sock_a:
            _kill_socket(sock_a)
        if sock_b:
            _kill_socket(sock_b)
        shutil.rmtree(work, ignore_errors=True)


def test_v08_missing_socket_target_sends_nothing(monkeypatch):
    _force_real_tmux(monkeypatch)
    work = Path(tempfile.mkdtemp(prefix="actl65m-", dir="/tmp"))
    try:
        sock, out, pane = _start_receiver(work, "m")
        before = _default_inventory()
        result = tmux.send_prompt_staged("%999", "nope", socket_path=sock, press_enter=True)
        assert result["ok"] is False
        assert result["failedStage"] == "verify_target"
        assert result["sideEffect"] == tmux.SIDE_EFFECT_NONE
        time.sleep(0.1)
        assert (not out.exists()) or out.read_bytes() == b""
        after = _default_inventory()
        assert after["panes"] == before["panes"]
    finally:
        _kill_socket(str(work / "m.sock"))
        shutil.rmtree(work, ignore_errors=True)


def test_isolated_tmux_concurrent_sends_do_not_interleave(monkeypatch):
    assert Path(REAL_TMUX).is_file()
    _force_real_tmux(monkeypatch)
    work = Path(tempfile.mkdtemp(prefix="actl-lock-", dir="/tmp"))
    sock = out = pane = None
    try:
        sock, out, pane = _start_receiver(work, "lock")
        results = []

        def send(marker):
            results.append(tmux.send_prompt_staged(pane, f"{marker}-a\n{marker}-b", socket_path=sock))

        threads = [threading.Thread(target=send, args=(marker,)) for marker in ("FIRST", "SECOND")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        assert all(result["ok"] for result in results)
        deadline = time.time() + 2
        while time.time() < deadline and (not out.exists() or b"FIRST-a" not in out.read_bytes() or b"SECOND-a" not in out.read_bytes()):
            time.sleep(0.05)
        assert out.read_bytes() in {
            b"FIRST-a\nFIRST-b\nSECOND-a\nSECOND-b\n",
            b"SECOND-a\nSECOND-b\nFIRST-a\nFIRST-b\n",
        }
    finally:
        if sock:
            _kill_socket(sock)
        shutil.rmtree(work, ignore_errors=True)
