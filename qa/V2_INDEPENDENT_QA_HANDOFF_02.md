# JuActl V2 — Independent QA Handoff 02 (Stable-integrated)

Date: 2026-09-22  
Purpose: Corrected integration candidate based on **actual remote Stable**.

## Status target

`V2_REBASED_CANDIDATE_READY_FOR_E2E`

## Why this candidate exists

Independent QA found the previous candidate (`feat/v2-remote-scheduler-dogfood-01` @ `2242280`) was based on old local `4899b85`, while remote Stable had moved to `7f1d6c4`. The trees were **diverged** (merge-base `4899b85`).

This branch transplants V2 onto the real Stable tip without modifying Stable or force-pushing the old candidate.

## Exact SHAs

```text
STABLE_BASE_SHA=7f1d6c4e05b90a8b417c3952207027c4b77b1a69
OLD_V2_SHA=22422803a852f6b3bf882dbc04bb883124052308
NEW_V2_IMPLEMENTATION_SHA=549870d40e941e84c02a43051009a2b5b2570cb2
NEW_V2_SHA=confirm with `git rev-parse HEAD` / `origin/feat/v2-remote-scheduler-dogfood-02`
MERGE_BASE_SHA=7f1d6c4e05b90a8b417c3952207027c4b77b1a69
```

Required check (must succeed):

```powershell
git merge-base --is-ancestor 7f1d6c4e05b90a8b417c3952207027c4b77b1a69 HEAD
echo $LASTEXITCODE   # expect 0
```

## Locations

| Item | Value |
|---|---|
| Worktree | `C:\Users\user\Desktop\4_Projects_Ju\juactl-v2-dogfood-02` |
| Branch | `feat/v2-remote-scheduler-dogfood-02` |
| Old evidence branch (unchanged) | `feat/v2-remote-scheduler-dogfood-01` @ `2242280` |
| Portable Board | `qa\artifacts\portable\JuActlBoard.exe` (or `dist\JuActlBoard.exe`) |
| Portable CLI | `qa\artifacts\portable\JuActl.exe` (or `dist\JuActl.exe`) |

Do **not** install over `%LOCALAPPDATA%\Programs\JuActl`.

## Integration notes

Cherry-picked V2 scheduler/send-truth/result-correlation onto Stable `7f1d6c4`.

File sets did not conflict (Stable touched `cli.py` / `remote.py` / `runtime.py` + related tests; V2 touched scheduler/tmux/validation/gui/tui).

Semantic merge in Board SEND:

1. Prefer Stable `remote_managed_send` when remote + managed.send available.
2. On success → V2 `SUBMITTED`, then activity evidence → `START_ACKNOWLEDGED`.
3. On `ManagedUnsupported` → V2 staged paste+Enter path (same evidence rules).
4. Always under V2 P0 remote scheduler (background deferred).

## Tests vs ACTUAL Stable `7f1d6c4`

V2 module:

```text
PASSED=10 FAILED=0 SKIPPED=0 XFAILED=0 ERRORS=0
```

instance-control:

```text
PASSED=3 FAILED=0 SKIPPED=0 XFAILED=0 ERRORS=0
```

Full suite (candidate):

```text
PASSED=256 FAILED=111 SKIPPED=0 XFAILED=0 ERRORS=0
```

Full suite (Stable `7f1d6c4` baseline, same host):

```text
PASSED=244 FAILED=113 SKIPPED=0 XFAILED=0 ERRORS=0
```

`NEW_FAILURES_VS_STABLE=[]` (blocker gate clear).  
Full suite is not claimed green; remaining failures match Windows Stable baseline (`clock_gettime`, etc.).

## V2 behavior checklist (automated)

- user SEND > background preview/refresh — covered by scheduler tests
- duplicate refresh coalesced — covered
- TRANSPORT_BUSY != DOWN — covered
- failed SEND does not advance correlation — covered
- stale old result not copied as new — covered
- successful SEND → SUBMITTED — code path + labels
- processing evidence → START_ACKNOWLEDGED — code path + labels
- result change → NEW_RESULT — covered
- no SSH/tmux fan-out regression — covered (serial exclusive)

## Real E2E sequence (Founder / independent QA)

Use **this** portable Board only.

1. Ensure ASUS has a live Codex pane.
2. Launch portable `JuActlBoard.exe` (ssh asus).
3. Leave background refresh ON.
4. Select Codex runtime.
5. SEND unique harmless marker, e.g. `ACTL_V2_QA02_<UTC>_<RANDOM6>`.
6. Expect Korean flow: 전송 대기 → 전송 중 → 제출 완료 → Agent 작업 시작 확인.
7. Wait for marker as new result.
8. COPY RESULT → 새 결과 있음; clipboard/text exact match.
9. Confirm no false 꺼짐 / no remap-for-busy while pane alive.
10. Optional: failed SEND then COPY → 이전 작업 결과 (not silent new).

Do not run destructive prompts. Do not merge. Do not move Stable.

## Out of scope

- Promoting / merging this branch
- Force-pushing `feat/v2-remote-scheduler-dogfood-01`
- Replacing installed V1

