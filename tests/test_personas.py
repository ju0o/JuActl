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


def test_persona_dashboard_shows_live_runtimes_and_gates_controls():
    gui = (ROOT / "src/actl/gui.py").read_text(encoding="utf-8")
    assert 'row.get("control_ready")' in gui
    assert "LIVE RUNTIME INSTANCES" in gui
    assert 'return "결과 도착"' in gui
    assert 'return "작업중"' in gui
    assert 'return "Prompt 대기"' in gui


def test_persona_dashboard_inventory_is_fast_and_hydrates_after_render():
    gui = (ROOT / "src/actl/gui.py").read_text(encoding="utf-8")
    tui = (ROOT / "src/actl/tui.py").read_text(encoding="utf-8")
    assert 'hydrate=False' in gui
    assert 'hydrate=True' in gui
    assert 'if self.hydrating' in gui
    assert 'capture_pane(target, history=8)' in tui


def test_persona_windows_remote_copy_prefers_mainpc_clipboard():
    gui = (ROOT / "src/actl/gui.py").read_text(encoding="utf-8")
    assert 'if sys.platform == "win32" and self.ssh_target:' in gui
    assert 'preferred = "local"' in gui


def test_persona_running_motion_stops_at_idle_prompt_phase():
    gui = (ROOT / "src/actl/gui.py").read_text(encoding="utf-8")
    assert "self.motion_phase" in gui
    assert '"RUNNING ◐ · 작업중"' in gui
    assert 'return "Prompt 대기"' in gui
    assert "self.root.after(180, self._motion_tick)" in gui


def test_persona_monitor_uses_one_surface_and_serializes_initial_board_load():
    gui = (ROOT / "src/actl/gui.py").read_text(encoding="utf-8")
    assert "self.resp = self.preview" in gui
    assert 'self.board_opened = False' in gui
    assert "if self.ssh_target and not self.board_opened" in gui
    assert 'self.root.after(80, self.on_board)' in gui
    assert 'height=2' in gui


def test_persona_event_watch_throttles_output_bursts():
    gui = (ROOT / "src/actl/gui.py").read_text(encoding="utf-8")
    assert 'event.startswith("%output")' in gui or '"%output", "%pane-mode-changed"' in gui
    assert 'now - self.last_event_refresh >= 1.0' in gui
    assert 'self.root.after(120 if topology else 450, self._event_refresh)' in gui


def test_persona_pane_event_refresh_is_targeted_not_full_rows_refresh():
    gui = (ROOT / "src/actl/gui.py").read_text(encoding="utf-8")
    assert "self.pending_event_panes" in gui
    assert "self._refresh_event_panes(pane_ids)" in gui
    assert "self._bg(lambda: _pane_preview(target), self._event_pane_done)" in gui


def test_persona_send_failure_is_visible_in_monitor_surface():
    gui = (ROOT / "src/actl/gui.py").read_text(encoding="utf-8")
    assert 'self.set_status("전송 실패")' in gui
    assert "전송 실패\\n\\n{detail}" in gui
    assert "BUSY라면 현재 다른 작업이 pane을 점유 중입니다." in gui


def test_persona_pane_board_supports_confirmed_cross_window_move():
    tmux = (ROOT / "src/actl/core/tmux.py").read_text(encoding="utf-8")
    gui = (ROOT / "src/actl/gui.py").read_text(encoding="utf-8")
    assert '"move-pane"' in tmux
    assert "messagebox.askyesno" in gui
    assert "<ButtonRelease-1>" in gui
    assert "작업 중인 Agent는 입력 상태가 바뀔 수 있습니다." in gui


def test_persona_mapping_stays_fail_closed_for_ambiguous_panes():
    discovery = (ROOT / "src/actl/core/discovery.py").read_text(encoding="utf-8")
    assert 'STRONG_CONFIDENCE = {"exact", "high"}' in discovery
    assert "return None, \"low\", \"multiple Agent process identities in one pane\"" in discovery


def test_persona_no_session_is_a_visible_failure_not_a_new_shell():
    source = (ROOT / "src/actl/core/tmux.py").read_text(encoding="utf-8")
    assert "remote tmux has no existing session; refusing to create one" in source
