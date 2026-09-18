"""V11: Direct CLI / paste / send_keys share Managed writer guard (pane-matched BUSY)."""
from __future__ import annotations

import builtins
import os
import uuid
from pathlib import Path

from actl import cli
from actl.core import runtime, tmux

V11_SOCK = "/tmp/actl-v11.sock"


def _scope():
    return {"hostKey": "hk-test", "uid": "1000", "socketPath": V11_SOCK}


def _patch_v11_scope(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(runtime, "_journal_root_override", tmp_path)
    monkeypatch.setattr(runtime, "local_host_key", lambda: "hk-test")
    monkeypatch.setattr(runtime, "local_uid", lambda: "1000")
    monkeypatch.setattr(
        runtime,
        "canonical_tmux_socket_path",
        lambda socket_path=None: socket_path or V11_SOCK,
    )


def _acquire_managed_pane(monkeypatch, tmp_path: Path, *, runtime_id: str, pane_id: str, **ctx_extra):
    _patch_v11_scope(monkeypatch, tmp_path)
    ctx = {"agentKind": "codex", "workspaceRoot": str(tmp_path / "ws"), "paneId": pane_id}
    ctx.update(ctx_extra)
    body = {
        "contractVersion": 1,
        "requestId": str(uuid.uuid4()),
        "operation": "reserve",
        "action": "acquire",
        "runtimeId": runtime_id,
        "mode": "MANAGED",
        "expectedContext": ctx,
        "serverScope": _scope(),
    }
    resp, code = runtime.handle_runtime_request(body)
    assert code == 0 and resp["ok"] is True
    return resp["data"], ctx


def _release(grant):
    body = {
        "contractVersion": 1,
        "requestId": str(uuid.uuid4()),
        "operation": "reserve",
        "action": "release",
        "reservationId": grant["reservationId"],
        "leaseToken": grant["leaseToken"],
        "fence": grant["fence"],
        "serverScope": _scope(),
    }
    resp, code = runtime.handle_runtime_request(body)
    assert code == 0 and resp["data"]["state"] == "RELEASED"
    return resp


def _count_runs(monkeypatch):
    calls: list = []

    def fake_run(args, **kwargs):
        calls.append(list(args))

        class Result:
            stdout = ""

        return Result()

    monkeypatch.setattr(tmux, "target_exists", lambda target, socket_path=None: True)
    monkeypatch.setattr(tmux, "_run", fake_run)
    monkeypatch.setattr(
        tmux.subprocess,
        "run",
        lambda *a, **k: type("Result", (), {"returncode": 0, "stdout": ""})(),
    )
    return calls


def test_direct_send_busy_when_managed_holds_matching_pane(monkeypatch, tmp_path):
    _acquire_managed_pane(monkeypatch, tmp_path, runtime_id="rt_v11_send", pane_id="%9")
    calls = _count_runs(monkeypatch)
    monkeypatch.setattr(cli, "_resolve_live_target", lambda config, agent: "%9")
    try:
        cli._send_to_selected({"agents": {"codex": {"target": "%9"}}}, "codex", "should-block")
    except RuntimeError as exc:
        assert "BUSY" in str(exc)
    else:
        raise AssertionError("expected BUSY from Direct send while Managed holds pane")
    assert calls == []


def test_paste_mode_busy_zero_keys(monkeypatch, tmp_path):
    _acquire_managed_pane(monkeypatch, tmp_path, runtime_id="rt_v11_paste", pane_id="%9")
    calls = _count_runs(monkeypatch)
    lines = iter(["line-one", "::send"])
    monkeypatch.setattr(builtins, "input", lambda: next(lines))
    cli._paste_mode("codex", "%9")
    assert calls == []


def test_send_keys_busy_zero_keys(monkeypatch, tmp_path):
    _acquire_managed_pane(monkeypatch, tmp_path, runtime_id="rt_v11_keys", pane_id="%9")
    calls = _count_runs(monkeypatch)
    try:
        tmux.send_keys("%9", "C-c", socket_path=V11_SOCK)
    except runtime.WriterDenied as denied:
        assert denied.code == "BUSY"
    else:
        raise AssertionError("expected WriterDenied BUSY from send_keys")
    assert calls == []


def test_other_pane_not_blocked_by_managed_hold(monkeypatch, tmp_path):
    """Only the matching paneId/tmuxTarget is blocked; other panes on the socket stay FREE."""
    _acquire_managed_pane(monkeypatch, tmp_path, runtime_id="rt_v11_other", pane_id="%9")
    calls = _count_runs(monkeypatch)
    tmux.send_prompt("%8", "ok-other-pane", socket_path=V11_SOCK)
    assert calls, "Direct send to a different pane must be allowed"
    assert any("paste-buffer" in c or "load-buffer" in c for c in calls)


def test_direct_allowed_after_managed_release(monkeypatch, tmp_path):
    grant, _ = _acquire_managed_pane(monkeypatch, tmp_path, runtime_id="rt_v11_rel", pane_id="%9")
    calls = _count_runs(monkeypatch)
    try:
        tmux.send_prompt("%9", "blocked", socket_path=V11_SOCK)
    except runtime.WriterDenied as denied:
        assert denied.code == "BUSY"
    else:
        raise AssertionError("expected BUSY before release")
    assert calls == []
    _release(grant)
    tmux.send_prompt("%9", "after-release", socket_path=V11_SOCK)
    assert calls, "Direct send must work after side-effect-free Managed release"
    # Returning from send must not auto-release; reservation already RELEASED above.
    summary_path = runtime.journal_path_for_scope(
        runtime.scope_id_for_socket("hk-test", "1000", V11_SOCK)
    )
    assert summary_path.exists()


def test_managed_transport_with_pre_send_hook_still_works(monkeypatch, tmp_path):
    """Managed path supplies pre_send_hook — Direct auto-guard must not self-BUSY."""
    grant, ctx = _acquire_managed_pane(monkeypatch, tmp_path, runtime_id="rt_v11_managed", pane_id="%9")
    calls = _count_runs(monkeypatch)
    hook = runtime.make_writer_pre_send_hook(
        runtime_id=grant["runtimeId"],
        socket_path=V11_SOCK,
        mode="MANAGED",
        reservation_id=grant["reservationId"],
        lease_token=grant["leaseToken"],
        fence=grant["fence"],
        host_key="hk-test",
        uid="1000",
    )
    result = tmux.send_prompt_staged(
        "%9",
        "managed-ok",
        socket_path=V11_SOCK,
        pre_send_hook=hook,
    )
    assert result["ok"] is True
    assert result["sideEffect"] == tmux.SIDE_EFFECT_OBSERVED
    assert calls
    assert ctx["paneId"] == "%9"


def test_tmux_target_context_key_also_blocks(monkeypatch, tmp_path):
    _acquire_managed_pane(
        monkeypatch,
        tmp_path,
        runtime_id="rt_v11_tmux_target",
        pane_id="%1",
        tmuxTarget="%9",
    )
    # paneId is %1 but tmuxTarget %9 must still block Direct to %9
    calls = _count_runs(monkeypatch)
    try:
        tmux.send_keys("%9", "Enter", socket_path=V11_SOCK)
    except runtime.WriterDenied as denied:
        assert denied.code == "BUSY"
    else:
        raise AssertionError("expected BUSY via tmuxTarget")
    assert calls == []


def test_canonical_tmux_socket_path_from_env_and_default(monkeypatch):
    saved = os.environ.pop("TMUX", None)
    try:
        assert runtime.canonical_tmux_socket_path("/tmp/actl-v11.sock") == "/tmp/actl-v11.sock"
        assert runtime.canonical_tmux_socket_path(None) == f"/tmp/tmux-{os.getuid()}/default"
        os.environ["TMUX"] = "/tmp/custom.sock,1234,0"
        assert runtime.canonical_tmux_socket_path(None) == "/tmp/custom.sock"
    finally:
        if saved is None:
            os.environ.pop("TMUX", None)
        else:
            os.environ["TMUX"] = saved
