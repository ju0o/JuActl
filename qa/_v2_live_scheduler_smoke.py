"""Live MainPC->ASUS scheduler smoke (non-destructive)."""
from __future__ import annotations

import threading
import time

from actl.core import tmux
from actl.core.remote_scheduler import (
    KIND_REFRESH,
    KIND_SEND,
    P0_USER,
    P2_BACKGROUND,
    TransportBusyError,
    close_scheduler,
    scheduler_for,
)
from actl.core.validation import validate_target


def main() -> int:
    close_scheduler(None)
    tmux.set_remote_ssh("asus")
    evidence: dict = {"errors": [], "notes": []}
    try:
        panes = tmux.list_panes()
        evidence["pane_count"] = len(panes)
        evidence["pane_commands"] = sorted({p.current_command for p in panes})
        evidence["codex_present"] = any(
            "codex" in (p.current_command or "").lower() for p in panes
        )

        s = scheduler_for("asus")
        bg_done: list = []
        user_result: dict = {}

        def bg() -> None:
            try:
                out = tmux._run(["tmux", "list-panes", "-a", "-F", "#{pane_id}"]).stdout
                bg_done.append(len(out.splitlines()))
            except Exception as exc:  # noqa: BLE001
                bg_done.append(f"err:{exc}")

        def user() -> None:
            def work():
                return tmux._run(
                    ["tmux", "display-message", "-p", "#{session_name}"]
                ).stdout.strip()

            user_result["value"] = s.submit(
                work,
                priority=P0_USER,
                kind=KIND_SEND,
                replaceable=False,
                timeout=30,
            )

        threads = [threading.Thread(target=bg, daemon=True) for _ in range(4)]
        for thread in threads:
            thread.start()
        time.sleep(0.05)
        ut = threading.Thread(target=user, daemon=True)
        ut.start()
        for thread in threads:
            thread.join(timeout=30)
        ut.join(timeout=30)
        evidence["user_priority_result"] = user_result.get("value")
        evidence["bg_results"] = bg_done
        evidence["scheduler_stats"] = s.stats()
        evidence["user_priority_ok"] = bool(user_result.get("value"))

        runs = {"n": 0}
        gate = threading.Event()
        started = threading.Event()

        def refresh():
            def work():
                runs["n"] += 1
                started.set()
                gate.wait(timeout=5)
                return tmux._run(
                    ["tmux", "list-sessions", "-F", "#{session_name}"]
                ).stdout.strip()

            return s.submit(
                work,
                priority=P2_BACKGROUND,
                kind=KIND_REFRESH,
                coalesce_key="e2e-refresh",
                replaceable=True,
            )

        results: list = []

        def launch() -> None:
            results.append(refresh())

        t0 = threading.Thread(target=launch, daemon=True)
        t0.start()
        assert started.wait(timeout=10)
        extras = [threading.Thread(target=launch, daemon=True) for _ in range(2)]
        for thread in extras:
            thread.start()
        time.sleep(0.1)
        gate.set()
        t0.join(timeout=30)
        for thread in extras:
            thread.join(timeout=30)
        evidence["coalesce_runs"] = runs["n"]
        evidence["coalesce_ok"] = runs["n"] == 1 and len(set(results)) == 1

        from actl.core import validation

        old = validation.target_exists

        def boom(_target: str):
            raise TransportBusyError(
                "remote transport busy: maximum in-flight operations is 1"
            )

        validation.target_exists = boom  # type: ignore[assignment]
        try:
            value = validate_target("codex", "%0")
            evidence["busy_state"] = value.state
            evidence["busy_not_down"] = value.state == "TRANSPORT_BUSY"
        finally:
            validation.target_exists = old

        evidence["transport_state"] = tmux._remote_transport().health()
    except Exception as exc:  # noqa: BLE001
        evidence["errors"].append(str(exc))
    finally:
        tmux.set_remote_ssh(None)
        close_scheduler(None)

    print("E2E_EVIDENCE")
    for key, value in evidence.items():
        print(f"{key}={value!r}")
    ok = (
        evidence.get("user_priority_ok")
        and evidence.get("coalesce_ok")
        and evidence.get("busy_not_down")
        and not evidence.get("errors")
    )
    print("SMOKE_VERDICT=" + ("PASS" if ok else "FAIL"))
    print(
        "CODEX_SEND_E2E="
        + ("AVAILABLE" if evidence.get("codex_present") else "NOT_AVAILABLE_NO_CODEX_PANE")
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
