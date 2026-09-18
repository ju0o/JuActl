import os

from actl.core import state


def test_acknowledgement_is_durable_and_hash_only(monkeypatch, tmp_path):
    path = tmp_path / "state.json"
    env = dict(os.environ)
    env["ACTL_STATE_PATH"] = str(path)
    monkeypatch.setattr(state.os, "environ", env)

    assert state.unread("codex", "abc123")
    state.acknowledge("codex", "abc123")

    assert state.load() == {"seen_results": {"codex": "abc123"}}
    assert not state.unread("codex", "abc123")
    assert state.unread("codex", "new456")
    assert "text" not in path.read_text(encoding="utf-8")
