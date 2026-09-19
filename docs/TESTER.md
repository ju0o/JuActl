# JuActl Tester

The tester separates deterministic QA from live evidence. It never creates or
deletes tmux sessions.

## Run

```bash
bash scripts/tester.sh asus
```

The first phase runs the dependency-free regression suite, packaging contracts,
doctor JSON contract, and persona gates. The second phase connects to the
existing ASUS tmux server, performs eight repeated pane reads plus discovery,
and verifies that the session inventory is byte-for-byte unchanged.

## Personas

- Founder: JuActl owns only its `ssh -T` transport and does not touch an
  interactive Windows Terminal SSH process.
- Operator: refresh is event-first with a 60-second health fallback.
- Mapping operator: ambiguous panes stay unmapped; only exact/high confidence
  detections are eligible for automatic application.
- Recovery operator: an absent tmux session is reported as a failure; no
  anonymous shell is created during reconnect.

## Evidence rules

`PASS` means the assertion was executed. `LIVE_BLOCKED` means the external
target was unavailable or had no existing session; it is not a pass. SSH
disconnect and DA-response incidents remain separate until a real MainPC
interactive session proves their outcome.
