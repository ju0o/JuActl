# V2 Dogfood — Remote Operation Scheduler + Result Correlation

Date: 2026-09-21  
Candidate branch: `feat/v2-remote-scheduler-dogfood-01`  
Worktree: `C:\Users\user\Desktop\4_Projects_Ju\juactl-v2-dogfood-01`

## Identifiers

| Item | Value |
|---|---|
| V1 stable SHA | `4899b8562f3b8c3297342b756b80ef4e4b1c3a0e` (`feat/founder-workspace-dashboard-v1`) |
| Candidate SHA | *(filled after commit)* |
| Worktree path | `C:\Users\user\Desktop\4_Projects_Ju\juactl-v2-dogfood-01` |
| V1 worktree | `C:\Users\user\Desktop\4_Projects_Ju\juactl` (left unchanged; dirty build/dist preserved) |

## Exact root cause (dogfood incidents)

1. **Single-slot transport contention**  
   `RemoteTransport.executeTmux` allowed only one in-flight remote op and failed fast with `remote transport busy: maximum in-flight operations is 1` after a 10s lock wait. Board background work (preview / hydration / refresh / validation) occupied that slot while Founder SEND/FOCUS raced it.

2. **Busy presented as Agent DOWN**  
   `validate_target` caught transport exceptions and returned `DOWN`, so the UI treated temporary contention as a dead mapping and pushed remap messaging.

3. **SEND success was paste-level only**  
   Board `on_send` treated `_send_to_selected` / transport return as success without requiring paste+Enter evidence or runtime start acknowledgement.

4. **COPY RESULT had no send correlation**  
   Any extractable pane text (including the previous `ACTL_LIVE_E2E_OK_…` marker) was treated as the current task result even when the new SEND never submitted.

## Before / after behavior

| Area | Before (V1 dogfood) | After (V2 candidate) |
|---|---|---|
| Background vs SEND | SEND/FOCUS timed out or saw `transport busy` | Per-target priority scheduler; P0 user ops preempt queued P2; in-flight P2 finishes safely then P0 runs |
| Duplicate refresh | Multiple refresh/hydration paths could multiply remote work | Same `coalesce_key` joins one execution |
| Transport busy UI | Mapping → `DOWN` / remap pressure | `TRANSPORT_BUSY` (“원격 통신 대기 중”); controls remain valid; remap not required |
| SEND truth | “Pasted/sent” implied Task start | `SEND_QUEUED` → `SENDING` → `SUBMITTED` (paste+Enter evidence) → `START_ACKNOWLEDGED` (RUNNING) or `SEND_FAILED` |
| COPY RESULT | Silent stale marker copy | `NEW_RESULT` / `STALE_RESULT` / `RESULT_PENDING` / `NO_RESULT` with Korean Founder labels; stale not treated as new |

## Changed files

- `src/actl/core/remote_scheduler.py` — priority queue, coalesce, replaceable cancel, re-entrancy guard
- `src/actl/core/send_truth.py` — send lifecycle + result classification
- `src/actl/core/tmux.py` — transport routes through scheduler; scheduler cleanup on SSH target change
- `src/actl/core/validation.py` — `TRANSPORT_BUSY` / `DEGRADED` distinct from `DOWN`; `valid` includes busy
- `src/actl/tui.py` — mapping/activity states + Korean labels; busy ≠ DOWN
- `src/actl/gui.py` — Board P0/P2 scheduling, SEND truth UX, COPY correlation UX
- `tests/test_remote_scheduler_v2.py` — required regressions 1–8 (+ label checks)
- `qa/V2_DOGFOOD_REMOTE_SCHEDULER_REPORT.md` — this report
- `qa/_v2_live_scheduler_smoke.py` — non-destructive MainPC→ASUS smoke helper

## Tests

### Automated (dependency-free `python tests/run_tests.py`)

V2 module results (all PASS):

1. background preview + SEND priority — PASS  
2. duplicated refresh coalesced — PASS  
3. transport busy ≠ mapping DOWN — PASS  
4. failed SEND does not advance correlation — PASS  
5. old result + failed SEND → STALE_RESULT — PASS  
6. successful SEND + changed hash → NEW_RESULT — PASS  
7. successful SEND + unchanged hash → RESULT_PENDING — PASS  
8. no parallel exclusive fan-out — PASS  
9. Korean Founder labels present — PASS  

Suite totals on this Windows host:

| Tree | Result |
|---|---|
| V1 baseline (`4899b85`) | 243 passed, 110 failed |
| V2 candidate | 254 passed, 109 failed |

Notes on suite failures: the large Windows failure cluster (`time.clock_gettime` missing, `cp949` console encoding, absolute pane-path assumptions) is **pre-existing on V1** and not introduced by this candidate. All `test_tmux.test_remote_transport_*` regressions remained PASS. Net: +11 passes / −1 failure vs V1 baseline on the same host.

### MainPC real E2E

Command: `python qa/_v2_live_scheduler_smoke.py` with `PYTHONPATH=src` from the V2 worktree.

Observed evidence:

```text
pane_count=5
pane_commands=['bash', 'node', 'opencode']
codex_present=False
user_priority_result='jucontrol'
user_priority_ok=True
coalesce_runs=1
coalesce_ok=True
busy_state='TRANSPORT_BUSY'
busy_not_down=True
transport_state='READY'
SMOKE_VERDICT=PASS
CODEX_SEND_E2E=NOT_AVAILABLE_NO_CODEX_PANE
```

**Codex Board SEND → SUBMITTED → START_ACKNOWLEDGED → NEW_RESULT COPY** was **not executed**: no Codex pane was present on ASUS at verification time. No destructive prompt was sent. Independent QA must run that path when a live Codex pane exists, using a unique harmless marker.

## Known limitations

- Full Founder Board Codex SEND/COPY E2E is still required (pane absent during this run).
- In-flight background tmux commands are not hard-cancelled mid-frame (safe wait); only queued replaceable P2 work is deferred.
- `START_ACKNOWLEDGED` depends on activity classification (`RUNNING`); some agents may stay `SUBMITTED` until activity evidence appears.
- STALE_RESULT intentionally does **not** push prior text to the clipboard.
- Windows host still has unrelated pre-existing suite failures (`clock_gettime`, cp949).

## Independent QA readiness

Candidate is ready for independent QA on the V2 worktree/branch with V1 left stable.

Required QA checklist:

1. Start / attach Codex on ASUS pane.  
2. Board SEND unique harmless marker.  
3. Confirm Korean states: 전송 대기 → 전송 중 → 제출 완료 → Agent 작업 시작 확인.  
4. Confirm no false DOWN / no Founder-facing `remote transport busy` during background polling.  
5. COPY RESULT → 새 결과 있음 with exact new marker.  
6. Force a failed SEND and confirm COPY shows 이전 작업 결과, not silent success.

## Verdict

`V2_CANDIDATE_READY_FOR_INDEPENDENT_QA`
