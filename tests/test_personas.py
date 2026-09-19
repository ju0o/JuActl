"""Small, dependency-free persona gates for the production boundaries."""
from __future__ import annotations

from pathlib import Path

from actl.core import tmux

ROOT = Path(__file__).resolve().parents[1]


def test_persona_founder_transport_is_owned_and_noninteractive():
    source = (ROOT / "src/actl/core/tmux.py").read_text(encoding="utf-8")
    assert '"ssh", "-T"' in source
    assert '"BatchMode=yes"' in source
    assert "process.kill()" in source
    assert "new-session" not in source.split("class RemoteTransport", 1)[1].split("def _read_loop", 1)[0]


def test_persona_operator_refresh_is_event_first_with_health_fallback():
    gui = (ROOT / "src/actl/gui.py").read_text(encoding="utf-8")
    tui = (ROOT / "src/actl/tui.py").read_text(encoding="utf-8")
    assert "remote_events()" in gui
    assert "remote_events()" in tui
    assert "60000" in gui


def test_persona_mapping_stays_fail_closed_for_ambiguous_panes():
    discovery = (ROOT / "src/actl/core/discovery.py").read_text(encoding="utf-8")
    assert 'STRONG_CONFIDENCE = {"exact", "high"}' in discovery
    assert "return None, \"low\", \"multiple Agent process identities in one pane\"" in discovery


def test_persona_no_session_is_a_visible_failure_not_a_new_shell():
    source = (ROOT / "src/actl/core/tmux.py").read_text(encoding="utf-8")
    assert "remote tmux has no existing session; refusing to create one" in source
