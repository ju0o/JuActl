# JuActl P0 SSH / tmux Transport Incident Report

Date: 2026-09-19  
Scope: Windows MainPC -> SSH -> ASUS Ubuntu -> tmux 3.x

## Executive result

The empty pane board is a confirmed Windows SSH argument-transport defect, not
evidence that the ASUS tmux server has no sessions. The installed Windows build
fails on the remote `tmux list-panes -F ...` call with:

```text
command list-panes: -F expects an argument
```

The confirmed transport fix is now implemented locally for independent QA.
The unrelated pre-existing worktree change remains
`.commandcode/taste/taste.md` and is not part of this change.

The persistent transport now fails closed when no tmux session exists. It
probes `list-sessions` and attaches to an existing session ID; it does not run
bare `tmux -C`, `new-session`, or `new-session -A`. This prevents reconnect
churn from leaving anonymous empty tmux sessions behind. The GUI also offers a
safe “확실한 매핑” action that applies only strong detections.

The two reported incidents remain separate:

| Track | Classification | Evidence |
|---|---|---|
| A. DA / escape response leak | UNKNOWN / not reproduced | clean and normal tmux emit DA queries; the reported response payload was not observed in the bounded harness |
| B. SSH session instability | UNKNOWN | SSH subprocess amplification is confirmed, but direct disconnect causality was not proven |
| Windows pane discovery failure | ROOT_CAUSE confirmed | PowerShell/OpenSSH argument handling drops the `#{...}` format argument |
| Polling fan-out | AMPLIFICATION_FACTOR confirmed | 19 remote operations per measured refresh |

## Reproducer: Windows pane discovery failure

On MainPC after the installed build was launched:

```powershell
& "$env:LOCALAPPDATA\Programs\JuActl\JuActl.exe" discover --ssh asus
```

Observed:

```text
Traceback ...
actl.core.tmux.TmuxError: ...
command list-panes: -F expects an argument
```

The ASUS host still had these tmux sessions:

```text
JuControl: 1 windows
jucontrol: 9 windows (attached)
rwr: 1 windows
```

Therefore the empty GUI pane board was a transport failure hidden as an empty
result, not an empty tmux server.

## Measured refresh fan-out

One measured `_rows(load_config())` with the current eight-agent topology
produced 19 remote wrapper operations:

| Remote operation | Count |
|---|---:|
| `tmux list-panes` | 2 |
| `tmux display-message` | 12 |
| `tmux capture-pane` | 1 |
| `ps -eo` | 1 |
| `ps -o` | 1 |
| `cat /proc/.../environ` | 2 |
| Total | 19 |

Call path:

```text
GUI _auto_tick
  -> refresh
    -> rows_now
      -> tui._rows
        -> discover / list_panes
        -> per-agent validation
          -> target_exists / pane_field / display-message
          -> process-table read
        -> activity observation
          -> ps -o
          -> capture-pane
        -> response extraction / correlation
          -> remote metadata reads
```

The GUI uses 12 seconds while no agent is classified RUNNING and 3 seconds
when any agent is RUNNING. At the measured 19-operation expansion this is:

- idle theoretical upper bound: 5 refreshes/minute = 95 remote operations/minute
- running theoretical upper bound: 20 refreshes/minute = 380 remote operations/minute

This is a confirmed amplification factor. It is not by itself proof of an SSH
disconnect root cause.

## MainPC process measurements

The measurements sampled MainPC every 500 ms.

### A. JuActl off

20 samples over 10 seconds:

```text
ssh.exe: 1 consistently
powershell.exe: 4 consistently
JuActlBoard.exe: 0
```

The one `ssh.exe` was the existing baseline connection/process observed by the
sampling session.

### B. JuActl GUI on, auto-refresh enabled

30 samples over 15 seconds after launch:

```text
ssh.exe: baseline 1, maximum 2
powershell.exe: baseline 4, maximum 5
JuActlBoard.exe: 2 observed during the sample
```

No unbounded accumulation was observed in this short sample. Exact process
creation totals were not claimed because 500 ms polling cannot observe every
short-lived child process.

### C. GUI auto-refresh off

NOT_PROVEN in this run. The test harness did not drive the GUI toggle through a
real Windows desktop input channel.

### D. TUI

NOT_PROVEN in this run. A real interactive terminal capture was not available
without changing the user's active terminal state.

## Bounded SSH disconnect diagnosis matrix

This matrix records only what the existing measurements establish. It does not
convert transport evidence into a Founder-only interactive-session result.

| Diagnosis question | Existing evidence | Outcome | Boundary |
|---|---|---|---|
| Did the old Windows pane-discovery path fail? | Installed build returned `command list-panes: -F expects an argument`; the ASUS tmux sessions still existed. | **OBSERVED** | Confirms the discovery transport defect, not disconnect causality. |
| Was SSH/session churn observed during the invalidated coexistence run? | ASUS journal recorded 210 accepted and 210 disconnected sessions from MainPC `100.86.210.95` between 00:28 and 00:47. | **OBSERVED** | The concurrent old MainPC client was not isolated from the run. |
| Did tmux topology change while that churn was active? | The prior `$1 jucontrol 5` inventory was replaced by `$0 0 1` after a new tmux server started. | **OBSERVED** | The readable journal slice had no tmux crash/OOM record. |
| Did MainPC SSH churn directly cause the tmux restart or disconnects? | Churn and topology change were concurrent, but no causal crash/OOM or packet-loss evidence was available. | **UNKNOWN** | Do not label the Founder interactive disconnect root cause. |
| Did the short process sample prove unbounded SSH amplification? | GUI-on sampling saw SSH count baseline 1, maximum 2; short-lived child totals were not observable at 500 ms. | **UNKNOWN** | The 19-operation refresh fan-out is an amplification factor, not a measured disconnect cause. |
| Was a long-duration before/after interactive A/B run completed? | The coexistence harness was invalidated by the tmux restart; the GUI-off, TUI, and real interactive cases were not driven. | **NOT_PROVEN** | No post-fix disconnect rate or absence may be claimed. |
| Was the Founder-only interactive SSH gate closed? | No valid Founder interactive SSH + tmux coexistence evidence is present in this report. | **NOT_PROVEN** | Remains an independent gate; this task does not run or claim it. |

## DA / escape response matrix

Bounded SSH/PTY captures were run against ASUS.

| Environment | Bytes | ESC bytes | Observed reported DA response |
|---|---:|---:|---|
| SSH -> bash only | 24 | 1 | not observed |
| SSH -> normal tmux | 1268 | 166 | not observed |
| SSH -> `tmux -L actl-da-clean -f /dev/null` | 1139 | 158 | not observed |
| JuActl off + interactive SSH | NOT_PROVEN | NOT_PROVEN | NOT_PROVEN |
| JuActl on + interactive SSH | NOT_PROVEN | NOT_PROVEN | NOT_PROVEN |

Normal and clean tmux both emitted terminal capability queries such as
`ESC[c` and `ESC[>c`. Those are queries, not the user's reported capability
response payload. The harness did not include a real Windows Terminal emulator
responding to those queries, so it cannot certify the leak absent or present.

## Root-cause classification

1. `ROOT_CAUSE`: Windows PowerShell/OpenSSH argument handling corrupts the tmux
   `-F "#{...}"` argument in the current Windows transport path.
2. `AMPLIFICATION_FACTOR`: one GUI refresh expands to 19 independent remote
   operations in the measured eight-agent state.
3. `UNKNOWN`: SSH disconnect causality. The observed process maximum was low,
   but the experiment did not measure packet loss, sshd child churn, CPU/RAM,
   or long-duration disconnect rate under A/B conditions.
4. `UNKNOWN`: DA response leak causality. tmux capability queries are normal;
   the reported response leak was not reproduced in a real Windows Terminal
   interactive session.

## Implemented P0 transport design

The selected design is a JuActl-owned persistent `tmux -C` control session
over one `ssh.exe` process per target. Each request is a quoted tmux control
command and each response is read from tmux's `%begin/%end` frame. The
transport has `DISCONNECTED`, `CONNECTING`, `READY`, and `DEGRADED` states,
one in-flight request, a ten-second request timeout, bounded reconnect
backoff, and owned-process shutdown. The GUI already coalesces refreshes with
its `refreshing` guard; no new polling storm or escape filtering was added.

Rejected alternatives:

- OpenSSH ControlMaster/ControlPersist: not supported by the Founder's
  Win32-OpenSSH environment and therefore not selected.
- A remote daemon: unnecessary deployment and security surface for this
  confirmed defect.
- Regex removal of DA responses: unrelated to the confirmed transport defects
  and deliberately not implemented.

The remote path now routes `list-panes`, `display-message`, `capture-pane`,
validation target checks, pane naming/topology commands, and send/paste tmux
commands through the transport. Remote prompt loading uses tmux `set-buffer`
inside the control session, so it does not depend on a MainPC temporary file
being visible on ASUS.

Source-level before/after reconciliation:

| Metric | Before | After implementation | Status |
|---|---:|---:|---|
| Logical remote operations/idle refresh | ~19 | unchanged logical fan-out | amplification remains visible |
| Logical operations/min idle | ~95 | ~95 | no polling concealment |
| Logical operations/min RUNNING | ~380 | ~380 | no polling concealment |
| SSH process spawns per warm refresh | one-shot path per operation | 0 expected | MainPC E2E pending |
| SSH handshakes/min after warm-up | unknown | 0 expected while READY | MainPC E2E pending |
| PowerShell process spawns for tmux | not isolated | 0 for transport | MainPC E2E pending |
| In-flight tmux requests | unbounded fan-out | 1 per target | source/test proven |

The after values marked pending are intentionally not presented as live
Windows measurements. The ASUS smoke test opened one control session and
successfully executed `list-panes`, `display-message`, and `capture-pane`; the
unit transport test executed two commands with one `Popen`.

## MainPC verification after investigation

The currently installed MainPC build was tested before this local fix:

```text
doctor --ssh asus: exit 0, live mapping 0/8
discover --ssh asus: exit 1
GUI process: started and remained alive for 5 seconds
```

The failing `discover` error was the same `list-panes -F expects an argument`
transport failure above. The GUI test processes were stopped after the check.
That old installation is not evidence for the new transport. A new Windows
installer/MainPC E2E run remains an independent-QA gate.

## Remaining UNKNOWNs

- The Founder interactive SSH disconnect root cause remains UNKNOWN. If the
  post-fix 30-minute E2E run has no disconnect, report only
  `NOT REPRODUCED AFTER FIX`.
- The DA/escape response leak remains UNKNOWN and was not filtered or altered.
- Windows process/handshake counts, fault recovery, and GUI visual behavior
  require the independent MainPC QA run.

## Independent QA collision observed during post-fix verification

The Linux-side coexistence harness was stopped after the ASUS tmux server
changed underneath it. This is not a PASS for the 30-minute gate.

Observed on 2026-09-20 Asia/Seoul:

- Baseline before the run: `$1 jucontrol 5` with two attached clients.
- The fixed transport, in one process, completed five discoveries with one
  SSH process and no session inventory change. Five separate CLI invocations
  also completed with no new tmux session.
- During the coexistence run, the remote journal recorded 210 accepted and 210
  disconnected SSH sessions from MainPC `100.86.210.95` between 00:28 and
  00:47. This is the same reconnect-churn signature as the incident report;
  the Linux harness originated from `100.82.108.31`.
- At 00:45:36 a new tmux server was started. The previous `$1 jucontrol 5`
  inventory was replaced by `$0 0 1`; the remaining `$0` control client was
  associated with an SSH session from `100.86.210.95`.
- The PTY capture before interruption contained 6.9 MB and zero occurrences
  of both reported response patterns: `ESC[?61;4;6...c` and
  `ESC[>0;10;1c`. This is not sufficient to close the DA gate because the
  coexistence run was invalidated by the tmux restart.

Classification:

- `OBSERVED`: concurrent MainPC SSH churn remained active during the test.
- `OBSERVED`: the tmux server/session topology changed during that churn.
- `UNKNOWN`: whether the MainPC churn directly killed the prior tmux server;
  no tmux crash/oom record was available in the readable journal slice.
- `NOT_PROVEN`: 30-minute coexistence gate and MainPC-installed-build result.

Safety change after this observation: `RemoteTransport` now selects only a
listed session with a non-numeric session name and `session_windows > 0`;
numeric auto-numbered sessions such as `$0` are rejected fail-closed instead
of being adopted. A live check against the resulting `$0 0 2 2` session
returned `remote tmux has no existing session; refusing to create one` and
left the inventory unchanged. This prevents the fixed client from attaching
to the post-restart numeric session, but cannot control an older MainPC binary
that is still generating SSH/tmux churn. The MainPC old client must be stopped
before repeating the independent gate.
