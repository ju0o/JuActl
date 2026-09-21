# JuActl V2 Dogfood 04 — Foreground Preemption + Responsive User Actions

Date: 2026-09-22 (Night Shift)

## Final status

`V2_DOGFOOD_04_READY_FOR_FOUNDER_E2E`

## Identifiers

```text
BASE_SHA=af1ab6da2d0a70d94ab36d5930be451996d6ef0b
STABLE=7f1d6c4e05b90a8b417c3952207027c4b77b1a69
NEW_CANDIDATE_SHA=7bbdbb00e82a70a4bc66539199187eeb7f3d0d16
branch=feat/v2-foreground-preemption-dogfood-04
worktree=C:\Users\user\Desktop\4_Projects_Ju\juactl-v2-dogfood-04
```

Preserved untouched: Stable 7f1d6c4, dogfood-02 a059f9f, dogfood-03 af1ab6d.

## Exact changed files

- src/actl/core/remote_scheduler.py
- src/actl/core/tmux.py
- src/actl/core/send_truth.py
- src/actl/gui.py
- tests/test_foreground_preemption_v2.py
- qa/V2_DOGFOOD_04_FOREGROUND_PREEMPTION_REPORT.md
- qa/_compare_failures_d3_d4.py

## Tests

Focused preemption module: PASS
Focused bundle (preemption+scheduler+permit): PASS (29–32)
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
- measured P0 acquisition latency (live ASUS): ~0.307s
- orphan SSH after preempt: none observed
- real SEND->Codex reply->COPY NEW_RESULT: FOUNDER_E2E_REQUIRED
- V1 / dogfood-02 / dogfood-03 untouched: YES
