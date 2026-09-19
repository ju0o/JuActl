# P0 Windows Remote Transport Fix

Status: implemented locally; independent MainPC QA required

## Scope

Windows MainPC runs JuActl and connects to ASUS Ubuntu over SSH. JuActl reads
and controls existing tmux sessions. This change does not modify JuControler,
sshd, Windows Terminal, Wi-Fi, tmux configuration, or terminal escape output.

## Confirmed defects

1. The previous Windows SSH argument path could damage tmux `-F "#{...}"`
   arguments, producing `-F expects an argument` and an empty pane board.
2. A refresh expanded into roughly 19 logical remote operations, each capable
   of creating an SSH transport in the previous design.

The interactive SSH disconnect and DA response leak remain UNKNOWN. They are
not declared fixed by this change.

## Architecture

`RemoteTransport` owns one `ssh.exe -> tmux -C` process per remote target.

```text
GUI/TUI/CLI
    -> actl.core.tmux._run
        -> RemoteTransport.executeTmux(argv)
            -> one persistent ssh.exe
                -> tmux control-mode frames (%begin/%end)
```

Properties:

- direct argv for initial SSH setup; no PowerShell wrapper per tmux operation;
- tmux control-mode argument quoting preserves `#{...}`, tabs, UTF-8, and LF;
- request queue is bounded to one in-flight request per target;
- ten-second request timeout and visible `DEGRADED` state;
- reconnect backoff is bounded and only the JuActl-owned process is closed;
- orderly `exit` on close, with kill fallback for an owned stuck process;
- remote prompt buffers use `set-buffer` in control mode, not a local temp path.

OpenSSH ControlMaster/ControlPersist was rejected because it is not supported
by the Win32-OpenSSH environment. A remote daemon was rejected as unnecessary
deployment and security surface. No DA/escape regex filtering was added.

## Before / after measurements

Before measurement from `ROOT_CAUSE_REPORT.md`:

- 19 logical remote operations per measured refresh;
- about 95 logical operations/minute idle;
- about 380 logical operations/minute while an agent was RUNNING;
- short-lived SSH sessions were observed on ASUS, but exact equivalence to
  logical operations was not proven;
- MainPC sampling saw SSH process count baseline 1, maximum 2 in a short GUI
  sample, so process creation totals were not claimed.

After implementation self-test:

- one fake `Popen` handled two tmux commands in the regression test;
- a real ASUS control-mode smoke opened one persistent session and successfully
  ran `list-panes`, `display-message`, and `capture-pane`;
- 14 live remote panes were returned in that smoke;
- live Windows process/handshake counts are intentionally pending independent
  QA and are not inferred from the Linux smoke.

## Changed files

- `src/actl/core/tmux.py` — persistent control-mode transport and remote
  buffer handling;
- `tests/test_tmux.py` — format-argument and one-process reuse regression;
- `ROOT_CAUSE_REPORT.md` — evidence, classifications, and remaining unknowns.

## Self-test

- `python3 -m py_compile $(find src tests -name '*.py' -print)` — PASS
- dependency-free suite — 314 passed, 0 failed
- real Linux -> ASUS tmux control smoke — PASS
- no DA filtering or interactive SSH process manipulation — confirmed

## Independent QA gates

The following remain deliberately open until run on the actual MainPC:

- Windows installer build and installation of this exact commit;
- `discover --ssh asus` and GUI pane-board verification;
- before/after `ssh.exe`, PowerShell, connection, reconnect, and sshd counts;
- 1/2/4/8-agent refresh matrix and auto-refresh OFF/TUI cases;
- owned transport kill, ASUS unreachable, and restore/recovery tests;
- Founder interactive SSH + tmux coexistence for 30 minutes;
- DA/escape correlation, reported only as `NOT REPRODUCED AFTER FIX` if absent.

This document does not close the wider SSH incident.
