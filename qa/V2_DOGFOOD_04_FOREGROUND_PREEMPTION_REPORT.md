# JuActl V2 Dogfood 04 — Foreground Preemption + Responsive User Actions

Date: 2026-09-22 (Night Shift)

## Final status

`V2_DOGFOOD_04_READY_FOR_FOUNDER_E2E`

## Identifiers

```text
BASE_SHA=af1ab6da2d0a70d94ab36d5930be451996d6ef0b
STABLE=7f1d6c4e05b90a8b417c3952207027c4b77b1a69
NEW_CANDIDATE_SHA=PENDING_COMMIT
branch=feat/v2-foreground-preemption-dogfood-04
worktree=C:\Users\user\Desktop\4_Projects_Ju\juactl-v2-dogfood-04
```

Preserved (untouched): Stable 7f1d6c4, dogfood-02 a059f9f, dogfood-03 af1ab6d.

## Fixes

- A: active replaceable P2 preemption via cancel_event + aggressive SSH control close (no thread kill)
- B: FOREGROUND_ACQUIRE_MAX_S=2.0; live ASUS measured acquire ~0.307s
- C: truthful SEND/COPY UI phases; SENDING only after P0 owns transport
- D: live preemption smoke PASS; full SEND/COPY NEW_RESULT = FOUNDER_E2E_REQUIRED

## Changed files

- src/actl/core/remote_scheduler.py
- src/actl/core/tmux.py
- src/actl/core/send_truth.py
- src/actl/gui.py
- tests/test_foreground_preemption_v2.py
- qa/V2_DOGFOOD_04_FOREGROUND_PREEMPTION_REPORT.md
- qa/_compare_failures_d3_d4.py

## Tests

Focused preemption+scheduler+permit: PASS
Full suite: PASSED=289 FAILED=97
NEW_FAILURES_VS_DOGFOOD_03=[]

## Portable (not installed)

```text
portable Board path=C:\Users\user\Desktop\4_Projects_Ju\juactl-v2-dogfood-04\qa\artifacts\portable\JuActlBoard.exe
portable Board SHA256=EB32D023BF0C8D0B097B5691EAE7593F7104C5833BB25FAB71D394089D7CFF5B
portable CLI path=C:\Users\user\Desktop\4_Projects_Ju\juactl-v2-dogfood-04\qa\artifacts\portable\JuActl.exe
portable CLI SHA256=F3249B83EA963AC5AE8C3E8F3E009A110FB4822944A7D9459CAA25D5239604B3
```

## Explicit

- active P2 cancellable safely: YES
- measured P0 acquisition: ~0.307s
- orphan SSH after preempt: none observed
- real SEND->COPY NEW_RESULT: FOUNDER_E2E_REQUIRED
- V1/dogfood-02/dogfood-03 untouched: YES
