"""Clipboard delivery tests: OSC52, tmux wrapping, SSH, honesty, unicode."""
import base64
import io
import os
import sys

from actl.utils import clipboard as cb
from actl import cli
from actl.core.models import CopyResult


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def test_plain_terminal_osc52_raw_sequence(monkeypatch, tmp_path):
    # Simulate plain terminal (no tmux, no SSH, no WAYLAND_DISPLAY)
    monkeypatch.setattr(cb.os.environ, "get", lambda k, d=None: None if k in {"TMUX", "WAYLAND_DISPLAY", "DISPLAY", "SSH_TTY", "SSH_CONNECTION", "SSH_CLIENT"} else os.environ.get(k, d))
    # Ensure TMUX not in env dict
    monkeypatch.setattr(cb.os, "environ", {k: v for k, v in os.environ.items() if k not in {"TMUX", "WAYLAND_DISPLAY", "DISPLAY", "SSH_TTY", "SSH_CONNECTION", "SSH_CLIENT"}})
    # Also mock shutil.which to hide wl-copy/xclip
    monkeypatch.setattr(cb.shutil, "which", lambda _: None)
    # Capture stdout
    buf = io.StringIO()
    monkeypatch.setattr(cb.sys, "stdout", buf)
    # need isatty
    monkeypatch.setattr(buf, "isatty", lambda: True)
    # suppress stderr tty path
    monkeypatch.setattr(cb.sys, "stderr", io.StringIO())

    backend = cb.copy_text("hello")
    assert backend == "osc52"
    out = buf.getvalue()
    assert out == f"\033]52;c;{_b64('hello')}\a"
    # Must not claim guaranteed copy; cli layer uses honest message
    # Verify sequence decodes correctly
    assert _b64("hello") in out


def test_unicode_korean_roundtrip(monkeypatch):
    text = "한글 test ✓ — 코드"
    # Force plain raw path
    env = {k: v for k, v in os.environ.items() if k not in {"TMUX", "WAYLAND_DISPLAY", "DISPLAY", "SSH_TTY", "SSH_CONNECTION", "SSH_CLIENT"}}
    monkeypatch.setattr(cb.os, "environ", env)
    monkeypatch.setattr(cb.shutil, "which", lambda _: None)
    buf = io.StringIO()
    buf.isatty = lambda: True  # type: ignore
    monkeypatch.setattr(cb.sys, "stdout", buf)
    monkeypatch.setattr(cb.sys, "stderr", io.StringIO())
    cb.copy_text(text)
    out = buf.getvalue()

    def _extract_b64(s: str) -> str:
        # raw: \033]52;c;ENC\a ; wrapped: \033Ptmux;\033\033]52;c;ENC\a\033\\
        marker = ";c;"
        start = s.find(marker) + len(marker)
        end = s.find("\a", start)
        return s[start:end]

    encoded = _extract_b64(out)
    assert base64.b64decode(encoded).decode("utf-8") == text


def test_multiline_content_preserved(monkeypatch):
    text = "line1\nline2\n\nline4 with spaces  \n\tindented"
    env = {k: v for k, v in os.environ.items() if k not in {"TMUX", "WAYLAND_DISPLAY", "DISPLAY", "SSH_TTY", "SSH_CONNECTION", "SSH_CLIENT"}}
    monkeypatch.setattr(cb.os, "environ", env)
    monkeypatch.setattr(cb.shutil, "which", lambda _: None)
    buf = io.StringIO()
    buf.isatty = lambda: True  # type: ignore
    monkeypatch.setattr(cb.sys, "stdout", buf)
    monkeypatch.setattr(cb.sys, "stderr", io.StringIO())
    cb.copy_text(text)
    out = buf.getvalue()

    def _extract_b64(s: str) -> str:
        marker = ";c;"
        start = s.find(marker) + len(marker)
        end = s.find("\a", start)
        return s[start:end]

    decoded = base64.b64decode(_extract_b64(out)).decode("utf-8")
    assert decoded == text


def test_tmux_wrapping_when_passthrough_on(monkeypatch):
    # TMUX present + allow-passthrough on => DCS wrapper
    env = dict(os.environ)
    env["TMUX"] = "/tmp/tmux-1000/default,1,0"
    monkeypatch.setattr(cb.os, "environ", env)
    monkeypatch.setattr(cb.shutil, "which", lambda _: None)

    # Mock tmux show-option to return "on"
    import subprocess as sp

    def fake_run(args, **kw):
        class R:
            stdout = "on\n"
            returncode = 0

        assert "allow-passthrough" in args
        return R()

    monkeypatch.setattr(cb.subprocess, "run", fake_run)

    buf = io.StringIO()
    buf.isatty = lambda: True  # type: ignore
    monkeypatch.setattr(cb.sys, "stdout", buf)
    monkeypatch.setattr(cb.sys, "stderr", io.StringIO())

    # Direct helper should produce wrapped
    seq = cb.osc52_sequence_for_test("ACTL_OSC52_TEST_20260908", tmux_wrap=True)
    assert seq.startswith("\033Ptmux;")
    assert seq.endswith("\033\\")
    assert _b64("ACTL_OSC52_TEST_20260908") in seq

    # copy_text with passthrough on should emit wrapped
    # Need to ensure is_ssh false (SSH vars absent) but tmux triggers wrapped path
    # Remove SSH vars for this test
    if "SSH_TTY" in env:
        del env["SSH_TTY"]
    if "SSH_CONNECTION" in env:
        del env["SSH_CONNECTION"]
    monkeypatch.setattr(cb.os, "environ", env)
    # Re-mock run for second call
    monkeypatch.setattr(cb.subprocess, "run", fake_run)
    cb.copy_text("ACTL_OSC52_TEST_20260908")
    out = buf.getvalue()
    assert out.startswith("\033Ptmux;")
    assert _b64("ACTL_OSC52_TEST_20260908") in out


def test_tmux_raw_when_passthrough_off(monkeypatch):
    env = dict(os.environ)
    env["TMUX"] = "/tmp/tmux-1000/default,1,0"
    env.pop("SSH_TTY", None)
    env.pop("SSH_CONNECTION", None)
    env.pop("SSH_CLIENT", None)
    monkeypatch.setattr(cb.os, "environ", env)
    monkeypatch.setattr(cb.shutil, "which", lambda _: None)

    def fake_run_off(args, **kw):
        class R:
            stdout = "off\n"
            returncode = 0

        return R()

    monkeypatch.setattr(cb.subprocess, "run", fake_run_off)
    buf = io.StringIO()
    buf.isatty = lambda: True  # type: ignore
    monkeypatch.setattr(cb.sys, "stdout", buf)
    monkeypatch.setattr(cb.sys, "stderr", io.StringIO())
    cb.copy_text("wrap-test")
    out = buf.getvalue()
    assert out == f"\033]52;c;{_b64('wrap-test')}\a"
    assert not out.startswith("\033Ptmux;")


def test_ssh_headless_skips_wl_copy_and_uses_osc52(monkeypatch):
    # SSH vars present => must skip wl-copy even if available
    env = dict(os.environ)
    env["SSH_TTY"] = "/dev/pts/0"
    env["SSH_CONNECTION"] = "1.1.1.1 22 2.2.2.2 22"
    env["WAYLAND_DISPLAY"] = "wayland-0"
    monkeypatch.setattr(cb.os, "environ", env)
    # Pretend wl-copy exists and would succeed if called — should NOT be called
    monkeypatch.setattr(cb.shutil, "which", lambda n: "/usr/bin/wl-copy" if n == "wl-copy" else None)

    def fail_run(*a, **kw):
        raise AssertionError("wl-copy must not be invoked in SSH session")

    monkeypatch.setattr(cb.subprocess, "run", fail_run)
    # For tmux passthrough check, ensure it doesn't call tmux run unexpectedly
    # Provide a dummy that returns off
    original_run = cb.subprocess.run

    def tmux_or_fail(args, **kw):
        if "allow-passthrough" in args:
            class R:
                stdout = "off\n"
                returncode = 0

            return R()
        return fail_run(*args, **kw)

    monkeypatch.setattr(cb.subprocess, "run", tmux_or_fail)
    buf = io.StringIO()
    buf.isatty = lambda: True  # type: ignore
    monkeypatch.setattr(cb.sys, "stdout", buf)
    monkeypatch.setattr(cb.sys, "stderr", io.StringIO())
    backend = cb.copy_text("ssh-test")
    assert backend == "osc52"
    assert _b64("ssh-test") in buf.getvalue()


def test_wl_copy_unavailable_falls_back_to_osc52(monkeypatch):
    env = {k: v for k, v in os.environ.items() if k not in {"TMUX", "SSH_TTY", "SSH_CONNECTION", "SSH_CLIENT", "DISPLAY"}}
    env.pop("WAYLAND_DISPLAY", None)
    monkeypatch.setattr(cb.os, "environ", env)
    monkeypatch.setattr(cb.shutil, "which", lambda _: None)
    buf = io.StringIO()
    buf.isatty = lambda: True  # type: ignore
    monkeypatch.setattr(cb.sys, "stdout", buf)
    monkeypatch.setattr(cb.sys, "stderr", io.StringIO())
    assert cb.copy_text("fallback") == "osc52"


def test_osc52_payload_too_large_raises(monkeypatch):
    env = {k: v for k, v in os.environ.items() if k not in {"TMUX", "SSH_TTY", "SSH_CONNECTION"}}
    monkeypatch.setattr(cb.os, "environ", env)
    monkeypatch.setattr(cb.shutil, "which", lambda _: None)
    large = "x" * 60000  # > 56k -> > 74994 b64
    try:
        cb.copy_text(large)
        assert False, "should have raised ClipboardError"
    except cb.ClipboardError as e:
        assert "too large" in str(e)
        assert "/copy --print" in str(e)


def test_copy_message_honesty_for_osc52(monkeypatch, tmp_path):
    # Ensure _copy prints honest OSC52 wording, not "copied via osc52"
    monkeypatch.setattr(cli, "_resolve_live_target", lambda *_: "%0")
    monkeypatch.setattr(cli, "extract_last_response", lambda *_: CopyResult("hello world", "claude-session:/tmp/x", "exact"))
    monkeypatch.setattr(cli, "copy_text", lambda t, preferred="auto": "osc52")
    import io, sys as _sys

    old_out = _sys.stdout
    buf = io.StringIO()
    _sys.stdout = buf
    try:
        cli._copy({}, "claude-pro")
    finally:
        _sys.stdout = old_out
    out = buf.getvalue()
    assert "터미널 클립보드로 보냈어요" in out
    assert "copied via osc52" not in out
    assert "actl copy claude-pro --print" in out


def test_copy_message_for_wl_copy_still_says_copied(monkeypatch):
    monkeypatch.setattr(cli, "_resolve_live_target", lambda *_: "%0")
    monkeypatch.setattr(cli, "extract_last_response", lambda *_: CopyResult("hi", "x", "exact"))
    monkeypatch.setattr(cli, "copy_text", lambda t, preferred="auto": "wl-copy")
    import io, sys as _sys

    buf = io.StringIO()
    old = _sys.stdout
    _sys.stdout = buf
    try:
        cli._copy({}, "claude-pro")
    finally:
        _sys.stdout = old
    assert "답을 복사했어요 — 붙여넣기 하세요" in buf.getvalue()
    assert "wl-copy" not in buf.getvalue()
    assert "sent to terminal" not in buf.getvalue()


def test_copy_respects_preferred_osc52_config(monkeypatch):
    # clipboard_backend=osc52 must force OSC52 even on a non-SSH local session.
    monkeypatch.setattr(cli, "_resolve_live_target", lambda *_: "%0")
    monkeypatch.setattr(cli, "extract_last_response", lambda *_: CopyResult("hi", "x", "exact"))
    calls = {}
    monkeypatch.setattr(cli, "copy_text", lambda t, preferred="auto": calls.setdefault("preferred", preferred) or "osc52")
    import io, sys as _sys

    buf = io.StringIO()
    old = _sys.stdout
    _sys.stdout = buf
    try:
        cli._copy({"clipboard_backend": "osc52"}, "claude-pro")
    finally:
        _sys.stdout = old
    assert calls.get("preferred") == "osc52"
    assert "터미널 클립보드로 보냈어요" in buf.getvalue()


def test_copy_respects_preferred_local_config(monkeypatch):
    monkeypatch.setattr(cli, "_resolve_live_target", lambda *_: "%0")
    monkeypatch.setattr(cli, "extract_last_response", lambda *_: CopyResult("hi", "x", "exact"))
    calls = {}
    monkeypatch.setattr(cli, "copy_text", lambda t, preferred="auto": calls.setdefault("preferred", preferred) or "wl-copy")
    cli._copy({"clipboard_backend": "local"}, "claude-pro")
    assert calls.get("preferred") == "local"


def test_copy_returns_exit_code_and_handles_unmapped(monkeypatch):
    monkeypatch.setattr(cli, "_resolve_live_target", lambda *_: (_ for _ in ()).throw(ValueError("unmapped")))
    assert cli._copy({}, "pro") == 1


def test_copy_returns_zero_on_success(monkeypatch):
    monkeypatch.setattr(cli, "_resolve_live_target", lambda *_: "%0")
    monkeypatch.setattr(cli, "extract_last_response", lambda *_: CopyResult("ok", "x", "exact"))
    monkeypatch.setattr(cli, "copy_text", lambda t, preferred="auto": "wl-copy")
    assert cli._copy({}, "pro") == 0


def test_force_osc52_backend(monkeypatch):
    env = {k: v for k, v in os.environ.items() if k not in {"TMUX", "SSH_TTY", "SSH_CONNECTION", "SSH_CLIENT", "DISPLAY"}}
    monkeypatch.setattr(cb.os, "environ", env)
    monkeypatch.setattr(cb.shutil, "which", lambda n: "/usr/bin/wl-copy")
    buf = io.StringIO()
    buf.isatty = lambda: True  # type: ignore
    monkeypatch.setattr(cb.sys, "stdout", buf)
    monkeypatch.setattr(cb.sys, "stderr", io.StringIO())
    backend = cb.copy_text("force", preferred="osc52")
    assert backend == "osc52"
    assert _b64("force") in buf.getvalue()


def test_force_local_backend_fails_closed_when_unavailable(monkeypatch):
    env = {k: v for k, v in os.environ.items() if k not in {"TMUX", "SSH_TTY", "SSH_CONNECTION", "SSH_CLIENT", "DISPLAY"}}
    env.pop("WAYLAND_DISPLAY", None)
    monkeypatch.setattr(cb.os, "environ", env)
    monkeypatch.setattr(cb.shutil, "which", lambda _: None)
    try:
        cb.copy_text("x", preferred="local")
        assert False, "should raise ClipboardError"
    except cb.ClipboardError as e:
        assert "preferred=local" in str(e)


def test_unknown_backend_rejected():
    try:
        cb.copy_text("x", preferred="bogus")
        assert False
    except cb.ClipboardError:
        pass


def test_copy_print_fallback(monkeypatch):
    monkeypatch.setattr(cli, "_resolve_live_target", lambda *_: "%0")
    monkeypatch.setattr(cli, "extract_last_response", lambda *_: CopyResult("PRINT_ME", "x", "exact"))
    # copy_text must NOT be called in --print mode
    monkeypatch.setattr(cli, "copy_text", lambda t: (_ for _ in ()).throw(AssertionError("should not be called")))
    import io, sys as _sys

    buf = io.StringIO()
    old = _sys.stdout
    _sys.stdout = buf
    try:
        cli._copy({}, "claude-pro", print_only=True)
    finally:
        _sys.stdout = old
    assert buf.getvalue().strip() == "PRINT_ME"


def test_osc52_guidance_mentions_tmux_passthrough_when_off(monkeypatch):
    env = dict(os.environ)
    env["TMUX"] = "/tmp/tmux-1000/default,1,0"
    env.pop("SSH_TTY", None)
    env.pop("SSH_CONNECTION", None)
    env.pop("SSH_CLIENT", None)
    monkeypatch.setattr(cb.os, "environ", env)

    def fake_run_off(args, **kw):
        class R:
            stdout = "off\n"
            returncode = 0

        return R()

    monkeypatch.setattr(cb.subprocess, "run", fake_run_off)
    guidance = cb.osc52_guidance()
    assert guidance is not None
    assert "allow-passthrough on" in guidance
    assert "/copy --print" in guidance


def test_osc52_guidance_none_when_passthrough_on(monkeypatch):
    env = dict(os.environ)
    env["TMUX"] = "/tmp/tmux-1000/default,1,0"
    monkeypatch.setattr(cb.os, "environ", env)

    def fake_run_on(args, **kw):
        class R:
            stdout = "on\n"
            returncode = 0

        return R()

    monkeypatch.setattr(cb.subprocess, "run", fake_run_on)
    assert cb.osc52_guidance() is None


def test_osc52_guidance_none_outside_tmux(monkeypatch):
    env = {k: v for k, v in os.environ.items() if k != "TMUX"}
    monkeypatch.setattr(cb.os, "environ", env)
    assert cb.osc52_guidance() is None


def test_harmless_token_no_model_prompt(monkeypatch):
    # Verify the fixed test token from the spec round-trips correctly
    token = "ACTL_OSC52_TEST_20260908"
    env = {k: v for k, v in os.environ.items() if k not in {"TMUX", "SSH_TTY", "SSH_CONNECTION", "WAYLAND_DISPLAY", "DISPLAY"}}
    monkeypatch.setattr(cb.os, "environ", env)
    monkeypatch.setattr(cb.shutil, "which", lambda _: None)
    buf = io.StringIO()
    buf.isatty = lambda: True  # type: ignore
    monkeypatch.setattr(cb.sys, "stdout", buf)
    monkeypatch.setattr(cb.sys, "stderr", io.StringIO())
    cb.copy_text(token)
    encoded = _b64(token)
    assert encoded in buf.getvalue()
    assert base64.b64decode(encoded).decode() == token
