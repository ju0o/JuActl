from pathlib import Path

from actl import gui


ROOT = Path(__file__).resolve().parents[1]


def test_preview_timeout_word_in_successful_pane_text_is_fresh():
    text, fresh = gui._preview_text("LAST_GOOD", "request timeout recovered\nready")
    assert fresh is True
    assert text == "request timeout recovered\nready"


def test_pane_board_alias_is_persistent_and_falls_back_to_tmux_title(tmp_path, monkeypatch):
    config = {"agents": {}, "pane_board_labels": {"%7": "Codex desk"}}
    assert gui._pane_board_label(config, "%7", "old tmux title") == "Codex desk"
    assert gui._pane_board_label({"agents": {}}, "%7", "old tmux title") == "old tmux title"

    saved = []
    monkeypatch.setattr(gui, "save_config", lambda value: saved.append(value))
    gui._save_pane_board_label(config, "%7", "New label")
    assert config["pane_board_labels"]["%7"] == "New label"
    assert saved == [config]


def test_pane_board_alias_rejects_multiline_or_empty_names(monkeypatch):
    monkeypatch.setattr(gui, "save_config", lambda _value: (_ for _ in ()).throw(AssertionError("must not save")))
    for label in ("", "\n", "bad\rname", "   ", "\t"):
        try:
            gui._save_pane_board_label({}, "%7", label)
        except ValueError:
            pass
        else:
            raise AssertionError("expected invalid pane alias to fail")


def test_pane_board_rename_keeps_tmux_names_and_uses_display_aliases():
    source = (ROOT / "src/actl/gui.py").read_text(encoding="utf-8")
    rename = source.split("        def rename_selected()", 1)[1].split("        def create_session()", 1)[0]
    pane = rename.split('            if kind == "pane":', 1)[1].split('            elif kind == "window":', 1)[0]
    assert "_save_pane_board_label(self.config, pane.pane_id, name)" in pane
    assert "tmux.rename_pane" not in pane
    assert 'tmux.rename_window' in rename
    assert 'tmux.rename_session' in rename
    assert 'text=f"{pane.pane_id}  {_pane_board_label(self.config, pane.pane_id, pane.title or \'(untitled)\')}"' in source


def test_ssh_refresh_does_not_open_pane_board_without_user_action():
    source = (ROOT / "src/actl/gui.py").read_text(encoding="utf-8")
    refresh_done = source.split("    def _refresh_done", 1)[1].split("    def _render_projects", 1)[0]
    assert "self.on_board" not in refresh_done
    action_buttons = source.split("        self.action_buttons = {}", 1)[1].split("        from tkinter import scrolledtext", 1)[0]
    assert '("PANE BOARD", self.on_board, False)' in action_buttons
