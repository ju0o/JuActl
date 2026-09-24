import io

import actl.cli as cli


def test_send_strips_trailing_newline_but_keeps_inner_newline(monkeypatch):
    sent = []
    monkeypatch.setattr(cli, "ensure_config", lambda: None)
    monkeypatch.setattr(cli, "load_config", lambda: {})
    monkeypatch.setattr(cli, "_send_to_selected", lambda _cfg, _agent, prompt: sent.append(prompt) or "%0")
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("a\nb\n"))

    cli.main(["send", "codex"])

    assert sent == ["a\nb"]
