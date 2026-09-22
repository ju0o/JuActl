# JuActl V2 Dogfood 06 — Founder UX Closeout

Date: 2026-09-22

## Final status

`V2_DOGFOOD_06_READY_FOR_FOUNDER_E2E`

## Identifiers

```text
BASE_SHA=0ca82b9eb47e66a03726a9051fe7229b5f605790
STABLE=7f1d6c4e05b90a8b417c3952207027c4b77b1a69
NEW_CANDIDATE_SHA=PENDING_COMMIT
branch=feat/v2-founder-ux-closeout-dogfood-06
worktree=C:\Users\user\Desktop\4_Projects_Ju\juactl-v2-dogfood-06
```

Preserved untouched: Stable, dogfood-03/04/05 (dogfood-05 not promoted).

## Explicit answers

- Prompt clears: at authoritative SUBMITTED (on_committed / staged SUBMITTED) via `_clear_prompt_at_submitted` — before activity/done().
- Pre-commit failure: Prompt kept for retry; never cleared.
- Live transport: `capture_pane_live` one-shot SSH tmux capture (3s), not control-mode / not scheduler P2.
- First-preview latency: <=3s + SSH RTT.
- Cadence while working: ~1.8s (`_live_preview_tick`); follow ~90s after SEND.
- One timeout: retain last-good; header may show 갱신 지연; no timeout wall.
- Preview blocks SEND/COPY: NO.
- RESULT READY: loop phases READY→SENDING→SUBMITTED→WORKING→RESULT_READY/COPIED; COPY NEW sets 복사 완료.
- Board-only SEND→RESULT→COPY: designed yes; Founder E2E still required.

## Changed files

- src/actl/gui.py
- src/actl/tui.py
- src/actl/core/tmux.py
- src/actl/core/send_truth.py
- tests/test_founder_ux_closeout.py
- tests/test_personas.py
- tests/test_remote_scheduler_v2.py
- qa/V2_DOGFOOD_06_FOUNDER_UX_CLOSEOUT_REPORT.md
- qa/_compare_failures_d5_d6.py

## Tests

Focused: 49 passed
Full: PASSED=306 FAILED=97
NEW_FAILURES_VS_DOGFOOD_05=[]

## Portable (not installed)

```text
portable Board path=C:\Users\user\Desktop\4_Projects_Ju\juactl-v2-dogfood-06\qa\artifacts\portable\JuActlBoard.exe
portable Board SHA256=0A5D9EBA5B93CFC86E552CC9E4BB66FB63132ADA20C312732B234C90A61ED8F4
portable CLI path=C:\Users\user\Desktop\4_Projects_Ju\juactl-v2-dogfood-06\qa\artifacts\portable\JuActl.exe
portable CLI SHA256=4D1E5619DB47F3D98E67AA3FFEEA6FC69CFD24F504FE9A7E92893A4553C3E20E
```
