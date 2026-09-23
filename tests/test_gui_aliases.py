from actl import gui


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
    for label in ("", "\n", "bad\rname"):
        try:
            gui._save_pane_board_label({}, "%7", label)
        except ValueError:
            pass
        else:
            raise AssertionError("expected invalid pane alias to fail")
