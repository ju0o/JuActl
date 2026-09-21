# JuActl V2 — Independent QA Handoff

Date: 2026-09-21  
Status target: `V2_QA_HANDOFF_READY`

## Identifiers (fill/confirm at handoff start)

| Field | Value |
|---|---|
| Worktree | `C:\Users\user\Desktop\4_Projects_Ju\juactl-v2-dogfood-01` |
| Branch | `feat/v2-remote-scheduler-dogfood-01` |
| LOCAL_STABLE_SHA | `4899b8562f3b8c3297342b756b80ef4e4b1c3a0e` |
| REMOTE_STABLE_BRANCH_SHA | `7f1d6c4e05b90a8b417c3952207027c4b77b1a69` (`origin/feat/founder-workspace-dashboard-v1`) |
| CANDIDATE_BASE_SHA | `4899b8562f3b8c3297342b756b80ef4e4b1c3a0e` |
| CANDIDATE_HEAD_SHA | `db3b6b2a30252ad225cadcfc9d82753c9ee467e2` |
| Remote candidate | `origin/feat/v2-remote-scheduler-dogfood-01` |

Relationship:

- Local V1 checkout remains at `4899b85` and was not moved.
- Remote `origin/feat/founder-workspace-dashboard-v1` has advanced to `7f1d6c4` independently.
- This V2 candidate was branched from local stable `4899b85` (merge-base = `4899b85`).
- Do **not** force-push or rewrite the remote stable branch for this QA.

## Portable QA build (do not install over V1)

Run the candidate Board **from the portable exe**, not from `%LOCALAPPDATA%\Programs\JuActl`.

Expected artifact location (after handoff build):

- `qa/artifacts/portable/JuActlBoard.exe`
- `qa/artifacts/portable/JuActl.exe`
- `qa/artifacts/portable/SHA256SUMS.txt`

Confirm SHA256 against `SHA256SUMS.txt` before QA.

Installed V1 path (must remain untouched):

- `%LOCALAPPDATA%\Programs\JuActl\JuActlBoard.exe`

## Preconditions for real E2E

1. MainPC can `ssh asus` in BatchMode (no password prompt).
2. ASUS has an existing tmux session (candidate refuses to create one).
3. A live **Codex** pane is mapped / selectable on the Board.
4. Use only a **unique harmless marker** prompt. No destructive commands.
5. Leave background Board polling ON (this is what previously starved SEND).

Suggested marker pattern:

```text
ACTL_V2_QA_<UTC_TIMESTAMP>_<RANDOM6>
```

Example:

```text
ACTL_V2_QA_20260921T150000Z_a1b2c3
```

Ask Codex only to echo/print that exact marker as its final visible result. Do not ask it to edit files, run shell, or change systems.

## Exact E2E sequence

### A. Launch candidate only

1. From the V2 worktree portable folder, start:
   - `.\JuActlBoard.exe`
   - or the documented ssh-baked launch path for this build
2. Confirm the window title / status shows remote ASUS context.
3. Confirm installed V1 was not launched and not replaced.

### B. Select Codex runtime

1. Select the Codex card/runtime that maps to the live ASUS Codex pane.
2. Confirm mapping health is **not** DOWN if the pane is alive.
3. Acceptable living states include: 정상 / 작업 중 / 대기 / 원격 통신 대기 중.
4. If status shows 꺼짐 while the Codex pane is visibly alive on ASUS, **FAIL** this candidate for false DOWN.

### C. Background contention setup

1. Leave auto/event refresh enabled.
2. Click the Codex card so preview/hydration can run.
3. Within a few seconds, proceed to SEND while background work is active.
4. Goal: prove SEND is not blocked merely because preview/refresh holds the transport.

### D. SEND truth

1. Paste the unique harmless marker into SEND PROMPT.
2. Confirm send.
3. Watch Founder-facing Korean status/log for this order (or equivalent evidence):
   - 전송 대기
   - 전송 중
   - 제출 완료 (`SUBMITTED`) — requires paste + Enter evidence
   - Agent 작업 시작 확인 (`START_ACKNOWLEDGED`) — requires runtime processing evidence
4. **PASS criteria**
   - Task is not marked started on paste-only.
   - No Founder message that mapping is dead solely due to transport busy.
   - No instruction to remap while Codex pane remains alive.
5. **FAIL criteria**
   - `remote transport busy` presented as mapping DOWN / 꺼짐
   - SEND times out solely because background polling owns the slot
   - UI claims success without SUBMITTED evidence

### E. Result correlation

1. Wait until Codex finishes and the unique marker appears as the new result.
2. Press COPY RESULT.
3. Required classification:
   - Before the new result exists: 새 결과 기다리는 중 (`RESULT_PENDING`) is acceptable.
   - After the marker is the new result: 새 결과 있음 (`NEW_RESULT`).
4. Clipboard / displayed copy text must match the **exact** new marker string.
5. Must **not** silently treat a previous marker (for example an older `ACTL_LIVE_E2E_OK_…` string) as the new result.

### F. Negative check — failed SEND does not advance correlation

1. Optionally force a failed SEND (disconnect briefly, or cancel before submit if available).
2. COPY RESULT must show 이전 작업 결과 (`STALE_RESULT`) or 결과 없음 — never claim a new result for the failed task.

### G. Busy is not death

During/after polling contention:

1. Temporary 원격 통신 대기 중 is allowed.
2. Mapping must not flip to 꺼짐 solely for transport contention.
3. Founder must not be told to remap for a still-living Codex pane.

## Evidence to capture

Record:

- Candidate HEAD SHA
- Portable exe SHA256
- Marker string
- Screenshots or log lines for SUBMITTED / START_ACKNOWLEDGED / NEW_RESULT
- Exact COPY RESULT text
- Whether any false DOWN appeared
- Whether installed V1 path hash changed (must be unchanged)

## Out of scope for this QA

- Merging to stable
- Installing over V1
- Force-pushing remote stable
- Provider/account/OAuth changes
- Destructive prompts

## Final QA statuses

- Candidate code QA entry: proceed only if portable build + branch SHA match this handoff.
- Real runtime gate: Codex E2E sequence above.
- Report one of:
  - `V2_RUNTIME_QA_PASS`
  - `V2_RUNTIME_QA_FAIL`
