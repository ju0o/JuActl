# JuActl V2 Dogfood 03 — Input Permit Clock Domain + Selected Runtime Truth

Date: 2026-09-22

## Final status

`V2_DOGFOOD_03_READY_FOR_E2E`

## Identifiers

```text
BASE_SHA=a059f9faff86bd21b96c5fa43f4a6a74ffbd439e
STABLE=7f1d6c4e05b90a8b417c3952207027c4b77b1a69
NEW_CANDIDATE_SHA=<git rev-parse HEAD on feat/v2-input-permit-dogfood-03>
branch=feat/v2-input-permit-dogfood-03
worktree=C:\Users\user\Desktop\4_Projects_Ju\juactl-v2-dogfood-03
```

Preserved evidence (untouched):

- Stable `7f1d6c4`
- dogfood-02 `a059f9f` / `feat/v2-remote-scheduler-dogfood-02`

## Real E2E failure addressed

Founder portable Board → ASUS Codex managed SEND failed with:

`INPUT_STATE_UNKNOWN: inputPermit expired or not yet valid`

Measured: SSH RTT ~298ms, MainPC−ASUS clock delta ~2.494s (not a ≥10s config error).

Root cause: `remote_managed_send` stamped `inputPermit.confirmedAt` from **MainPC** `datetime.now(timezone.utc)` while ASUS runtime validates age against **ASUS** wall clock and correlates snapshot with the reserve observation.

## Fix A — server-issued freshness

`src/actl/core/remote.py` now sets:

- `inputPermit.confirmedAt = reserve_response["observedAt"]`
- `inputPermit.snapshotHash = grant["currentSnapshotHash"]`

Same remote observation for both fields. MainPC wall clock is not an authority.

Fail-closed if `observedAt` or `currentSnapshotHash` missing.  
`INPUT_PERMIT_MAX_AGE_S` remains **10 seconds**. TTL was not increased.

## Fix B — selected runtime truth

`inspector_truth(row)` derives title/detail/agent/pane/session/target from one `runtime_key`.

`on_select` applies that truth **before** async preview. Preview completion re-resolves the same key and refuses to paint a stale title over a newer selection.

## Changed files

- `src/actl/core/remote.py`
- `src/actl/gui.py`
- `tests/test_input_permit_clock_domain.py`
- `qa/V2_DOGFOOD_03_INPUT_PERMIT_REPORT.md`

## Tests

Input-permit / inspector module: all PASS (8).

V2 scheduler module: `PASSED=10 FAILED=0 SKIPPED=0 XFAILED=0 ERRORS=0`

instance-control: `PASSED=3 FAILED=0 SKIPPED=0 XFAILED=0 ERRORS=0`

Full suite (dogfood-03):

```text
PASSED=264
FAILED=111
SKIPPED=0
XFAILED=0
ERRORS=0
```

Full suite (dogfood-02 rerun, same host):

```text
PASSED=256
FAILED=111
SKIPPED=0
XFAILED=0
ERRORS=0
```

```text
NEW_FAILURES_VS_DOGFOOD_02=[]
```

(+8 passes = new dogfood-03 regressions). Full suite not claimed green; remaining failures match dogfood-02 Windows baseline.

## Explicit confirmations

| Question | Answer |
|---|---|
| MainPC wall clock removed from inputPermit freshness authority? | **Yes** |
| ASUS/server `observedAt` and snapshot correlated? | **Yes** (same reserve envelope) |
| Stale inspector title bug fixed? | **Yes** (`inspector_truth` + immediate sync) |
| V1 Stable untouched? | **Yes** (`7f1d6c4`) |
| dogfood-02 untouched? | **Yes** (`a059f9f`) |

## Portable artifacts (not installed)

```text
portable Board path=C:\Users\user\Desktop\4_Projects_Ju\juactl-v2-dogfood-03\qa\artifacts\portable\JuActlBoard.exe
portable Board SHA256=E7C547A4F9706FF357707A0D7E34D5A5E7E4EC0B053E3669E6A3202B4EA5371B
portable CLI path=C:\Users\user\Desktop\4_Projects_Ju\juactl-v2-dogfood-03\qa\artifacts\portable\JuActl.exe
portable CLI SHA256=9690F4C27714398F41F752972ECF4260B09DE2B36BBC94A9AF627BDCBBB944D4
```

Do not install over `%LOCALAPPDATA%\Programs\JuActl`.

## E2E retest focus

1. Launch this portable Board only.
2. Select Codex runtime; confirm title + detail + SEND target match.
3. Switch Claude → Codex after refresh; confirm no mixed identity.
4. SEND unique harmless marker under background polling.
5. Expect no `inputPermit expired or not yet valid` from ~2.5s skew.
6. Expect SUBMITTED → START_ACKNOWLEDGED → NEW_RESULT path as before.
