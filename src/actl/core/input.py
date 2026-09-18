"""TTY input that preserves terminal bracketed-paste payloads as one event."""
from __future__ import annotations

from dataclasses import dataclass
import os
import select
import sys

try:
    import termios
    import tty
except ImportError:
    termios = None  # type: ignore[assignment]
    tty = None  # type: ignore[assignment]

PASTE_START = b"\x1b[200~"
PASTE_END = b"\x1b[201~"
_PARSERS: dict[int, "BracketedPasteParser"] = {}
_PENDING: dict[int, list["InputEvent"]] = {}


@dataclass(frozen=True)
class InputEvent:
    text: str
    pasted: bool = False


class BracketedPasteParser:
    """Small byte-state parser, intentionally independent from a real TTY."""
    def __init__(self) -> None:
        self.data = bytearray()
        self.pasting = False

    def feed(self, chunk: bytes) -> list[InputEvent]:
        self.data.extend(chunk)
        events: list[InputEvent] = []
        while self.data:
            if not self.pasting:
                if self.data.startswith(PASTE_START):
                    del self.data[:len(PASTE_START)]
                    self.pasting = True
                    continue
                if PASTE_START.startswith(bytes(self.data)):
                    break
                newline = next((i for i, b in enumerate(self.data) if b in (10, 13)), None)
                if newline is None:
                    break
                raw = bytes(self.data[:newline])
                del self.data[:newline + 1]
                if self.data[:1] == b"\n" and chunk and chunk[0] == 13:
                    del self.data[:1]
                events.append(InputEvent(raw.decode("utf-8", "replace")))
            else:
                end = self.data.find(PASTE_END)
                if end < 0:
                    break
                raw = bytes(self.data[:end])
                del self.data[:end + len(PASTE_END)]
                self.pasting = False
                # Terminals sometimes append Enter after the paste delimiter.
                if self.data[:1] in (b"\r", b"\n"):
                    del self.data[:1]
                events.append(InputEvent(raw.decode("utf-8", "replace"), pasted=True))
        return events


def _safe_isatty(stream) -> bool:
    try:
        return bool(stream is not None and stream.isatty())
    except Exception:
        return False


def read_event(prompt: str) -> InputEvent:
    """Read a normal line or one bracketed paste. SSH preserves these escapes."""
    if termios is None or tty is None:
        return InputEvent(input(prompt))
    if not _safe_isatty(sys.stdin) or not _safe_isatty(sys.stdout):
        return InputEvent(input(prompt))
    fd = sys.stdin.fileno()
    pending = _PENDING.setdefault(fd, [])
    if pending:
        return pending.pop(0)
    old = termios.tcgetattr(fd)
    parser = _PARSERS.setdefault(fd, BracketedPasteParser())
    sys.stdout.write("\x1b[?2004h" + prompt)
    sys.stdout.flush()
    try:
        tty.setraw(fd)
        while True:
            chunk = os.read(fd, 4096)
            if not chunk:
                raise EOFError
            if chunk == b"\x03":
                raise KeyboardInterrupt
            # Echo conservatively. The parser owns semantic newlines.
            if chunk in (b"\x7f", b"\x08") and not parser.pasting:
                if parser.data:
                    parser.data.pop()
                    sys.stdout.write("\b \b")
                    sys.stdout.flush()
                continue
            sys.stdout.buffer.write(chunk)
            sys.stdout.flush()
            events = parser.feed(chunk)
            if events:
                # Keep raw mode briefly after Enter. Without this drain window,
                # bytes arriving between consecutive read_event calls may be
                # handled by the restored line discipline and a /switch can be
                # lost while the following prompt is delivered to the old pane.
                while True:
                    readable, _, _ = select.select([fd], [], [], 0.05)
                    if not readable:
                        break
                    extra = os.read(fd, 4096)
                    if not extra:
                        raise EOFError
                    sys.stdout.buffer.write(extra)
                    sys.stdout.flush()
                    events.extend(parser.feed(extra))
                # A terminal/SSH read can contain multiple complete lines.
                # Dropping the later lines leaves /switch unapplied and routes
                # the next prompt to the prior selected agent.
                pending.extend(events[1:])
                sys.stdout.write("\n")
                sys.stdout.flush()
                return events[0]
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
        sys.stdout.write("\x1b[?2004l")
        sys.stdout.flush()
