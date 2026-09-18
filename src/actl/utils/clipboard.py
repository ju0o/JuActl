from __future__ import annotations

import base64
import os
import shutil
import subprocess
import sys


class ClipboardError(RuntimeError):
    pass


# OSC 52 has a practical terminal limit. 74_994 base64 chars ~= 56kB raw.
# Windows Terminal / tmux often clip at ~100k. Keep conservative.
_OSC52_B64_LIMIT = 74_994


def _is_ssh_session() -> bool:
    return bool(
        os.environ.get("SSH_TTY")
        or os.environ.get("SSH_CONNECTION")
        or os.environ.get("SSH_CLIENT")
    )


def _tmux_passthrough_enabled() -> bool:
    """Return True iff tmux allow-passthrough is on/all (needs DCS wrapper)."""
    if not os.environ.get("TMUX"):
        return False
    try:
        proc = subprocess.run(
            ["tmux", "show-option", "-gv", "allow-passthrough"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=1,
        )
        return proc.stdout.strip() in {"on", "all"}
    except Exception:
        return False


def osc52_guidance() -> str | None:
    """Return user-facing guidance when raw OSC52 cannot reach the outer terminal.

    tmux forwards a raw OSC52 from a pane to the client only when the client
    terminfo defines the `Ms` capability (tmux man page). Ubuntu's terminfo
    base lacks Ms even for xterm-256color, so with allow-passthrough off the
    sequence is swallowed. Enabling allow-passthrough lets actl use the DCS
    wrapper, which bypasses the Ms gate entirely.
    """
    if not os.environ.get("TMUX"):
        return None
    if _tmux_passthrough_enabled():
        return None
    return (
        "tmux is blocking OSC52 passthrough (allow-passthrough off and no Ms "
        "terminfo capability). Add `set -g allow-passthrough on` to ~/.tmux.conf, "
        "then run /copy again; or use /copy --print for manual copy."
    )


def _try_local_clipboard(cmd: list[str], data: bytes, env_extra: dict[str, str] | None = None) -> bool:
    """Try a local clipboard program with timeout; return True on success.

    wl-copy/xclip/xsel fork a daemon that keeps serving the selection. Its
    inherited stdout/stderr must be detached (DEVNULL) or a pipe-mode caller
    (tests, scripts) hangs waiting for EOF. The daemon also must be fully
    detached from our session so it cannot stall the caller.
    """
    if not shutil.which(cmd[0]):
        return False
    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    try:
        subprocess.run(
            cmd,
            input=data,
            check=True,
            timeout=2,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return True
    except Exception:
        return False


def _osc52_sequence(text: str) -> tuple[str, str]:
    """Return (sequence, flavor) where flavor is 'tmux-wrapped' or 'raw'."""
    data = text.encode("utf-8")
    encoded = base64.b64encode(data).decode("ascii")
    if len(encoded) > _OSC52_B64_LIMIT:
        raise ClipboardError(
            f"OSC52 payload too large ({len(encoded)} base64 chars > {_OSC52_B64_LIMIT}); "
            "use /copy --print for manual copy"
        )
    # tmux 3.6: when allow-passthrough is on/all the DCS wrapper
    #   \ePtmux;\e\e]52;c;...\a\e\
    # is required for the outer terminal to receive the sequence.
    # With allow-passthrough off and set-clipboard external/on, tmux
    # itself intercepts raw \e]52;c; and forwards it, so raw is correct.
    if _tmux_passthrough_enabled():
        seq = f"\033Ptmux;\033\033]52;c;{encoded}\a\033\\"
        return seq, "tmux-wrapped"
    seq = f"\033]52;c;{encoded}\a"
    return seq, "raw"


def _safe_isatty(stream) -> bool:
    try:
        return bool(stream is not None and stream.isatty())
    except Exception:
        return False


def _write_osc52(seq: str) -> None:
    """Best-effort write of seq to the controlling terminal."""
    # Primary: current stdout (the pane pty when inside tmux).
    try:
        if sys.stdout is not None:
            sys.stdout.write(seq)
            sys.stdout.flush()
    except Exception:
        pass
    # If stdout was redirected/captured (e.g. piped tests), also try /dev/tty.
    if not _safe_isatty(sys.stdout):
        try:
            with open("/dev/tty", "w", encoding="utf-8") as fh:
                fh.write(seq)
                fh.flush()
        except Exception:
            pass
    # Additionally write to stderr if it is a tty and differs from stdout,
    # so the sequence reaches the terminal even when stdout is redirected.
    try:
        if _safe_isatty(sys.stderr) and sys.stderr is not sys.stdout:
            sys.stderr.write(seq)
            sys.stderr.flush()
    except Exception:
        pass


def _win_clipboard_set(data: bytes) -> bool:
    """Windows native clipboard via PowerShell Set-Clipboard (no modules)."""
    import sys as _sys

    if _sys.platform != "win32":
        return False
    if not shutil.which("powershell"):
        return False
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    import subprocess as _sp

    try:
        _sp.run(
            ["powershell", "-NoProfile", "-Command", "Set-Clipboard"],
            input=text,
            check=True,
            timeout=5,
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except Exception:
        return False


def copy_text(text: str, *, preferred: str = "auto") -> str:
    """Copy text to clipboard, returning backend name.

    ``preferred`` selects the transport:
      "auto"  — SSH → OSC52; Windows → Set-Clipboard; otherwise local
      wl-copy/xclip/xsel first.
      "osc52" — always emit the OSC52 terminal escape.
      "local" — always require a genuine local clipboard (Windows native,
      Wayland/X11).
    Priority (auto): 1. Windows native Set-Clipboard. 2. If not an SSH
    session and a local Wayland/X11 clipboard is genuinely usable, use
    wl-copy / xclip / xsel (verifiable, writes to host clipboard).
    3. Otherwise use OSC 52 terminal escape (best-effort; outer terminal
    support cannot be verified from the host). On OSC 52, the caller must
    not claim guaranteed clipboard success.
    """
    if preferred not in {"auto", "osc52", "local"}:
        raise ClipboardError(f"Unknown clipboard backend: {preferred}")
    data = text.encode("utf-8")
    is_ssh = _is_ssh_session()

    if preferred == "osc52":
        seq, _flavor = _osc52_sequence(text)
        _write_osc52(seq)
        return "osc52"

    # -- Windows native clipboard (PowerShell, no extra modules) --
    if _win_clipboard_set(data):
        return "win-clipboard"

    # -- Local clipboard only when NOT ssh (Windows SSH client wants OSC52) --
    if not (is_ssh and preferred != "local"):
        # wl-copy: accept both WAYLAND_DISPLAY and bare socket discovery via
        # XDG_RUNTIME_DIR.  If WAYLAND_DISPLAY is missing but the socket
        # exists, try with an explicit wayland-0.
        wl = shutil.which("wl-copy")
        if wl:
            wayland = os.environ.get("WAYLAND_DISPLAY")
            xdg = os.environ.get("XDG_RUNTIME_DIR", "")
            # Probe socket existence to supply wayland-0 when needed
            if not wayland and xdg and os.path.exists(os.path.join(xdg, "wayland-0")):
                if _try_local_clipboard(["wl-copy"], data, {"WAYLAND_DISPLAY": "wayland-0"}):
                    return "wl-copy"
            elif wayland:
                if _try_local_clipboard(["wl-copy"], data):
                    return "wl-copy"
            else:
                # No WAYLAND_DISPLAY and no XDG socket hint — still try once
                # (wl-copy may discover the socket itself, as observed).
                if _try_local_clipboard(["wl-copy"], data):
                    return "wl-copy"
        if os.environ.get("DISPLAY"):
            if _try_local_clipboard(["xclip", "-selection", "clipboard"], data):
                return "xclip"
            if _try_local_clipboard(["xsel", "--clipboard", "--input"], data):
                return "xsel"
        # No local clipboard succeeded — fall through to OSC52

    # -- OSC52 terminal clipboard (SSH/headless) --
    if preferred == "local":
        raise ClipboardError(
            "No local Wayland/X11 clipboard backend succeeded (preferred=local)"
        )
    seq, _flavor = _osc52_sequence(text)
    _write_osc52(seq)
    return "osc52"


def osc52_sequence_for_test(text: str, *, tmux_wrap: bool | None = None) -> str:
    """Test helper: produce the OSC52 sequence that copy_text would emit.

    If tmux_wrap is None, auto-detects via env; otherwise forces wrapping.
    """
    data = text.encode("utf-8")
    encoded = base64.b64encode(data).decode("ascii")
    if tmux_wrap:
        return f"\033Ptmux;\033\033]52;c;{encoded}\a\033\\"
    if tmux_wrap is False:
        return f"\033]52;c;{encoded}\a"
    seq, _ = _osc52_sequence(text)
    return seq
