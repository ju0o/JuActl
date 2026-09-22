# JuActl V2 Dogfood 05 — SEND Commit Boundary + COPY Result Truth

Date: 2026-09-22

## Final status

`V2_DOGFOOD_05_READY_FOR_FOUNDER_E2E`

## Identifiers

```text
BASE_SHA=eedb2a81e96fdcb1d930630262ad6eca4f4a486c
STABLE=7f1d6c4e05b90a8b417c3952207027c4b77b1a69
NEW_CANDIDATE_SHA=(git tip after commit)
branch=feat/v2-send-commit-copy-truth-dogfood-05
worktree=C:\Users\user\Desktop\4_Projects_Ju\juactl-v2-dogfood-05
```

Preserved untouched:

- Stable `7f1d6c4`
- dogfood-03 `af1ab6d`
- dogfood-04 `eedb2a8`

## Exact authoritative SUBMITTED commit point

Managed runtime `send` response returns `ok` with command target.

At that instant:

- `on_committed(ManagedSendDelivery)` fires
- GUI `set_send_state(..., SUBMITTED)`
- Founder UI may show `제출 완료`

**Not** after reserve release/reconcile.

## Cleanup after commit

Yes. `ManagedSendDelivery.cleanup()` is separate best-effort release/reconcile.

GUI uses `auto_cleanup=False` and runs cleanup on a daemon thread **off the P0 path**.

Cleanup failure is recorded (`cleanup_ok=False`, audit `managed_cleanup`) and **does not** convert SEND to `SEND_FAILED`.

## Can cleanup block COPY?

No. P0 SEND returns after commit (cleanup deferred). COPY acquires the scheduler slot without waiting for lease release SSH. Regression: deferred cleanup + COPY acquire ≤2s.

## Clipboard behavior

| Class | Clipboard |
|---|---|
| NEW_RESULT | write exact text once |
| RESULT_PENDING | no write |
| STALE_RESULT | no write |
| NO_RESULT | no write |
| failed SEND | no write (STALE/NO only) |

Duplicate COPY after NEW observation → STALE_RESULT → no clipboard write.

## Changed files

- `src/actl/core/remote.py`
- `src/actl/core/send_truth.py`
- `src/actl/gui.py`
- `src/actl/cli.py`
- `tests/test_send_commit_copy_truth.py`
- `qa/V2_DOGFOOD_05_SEND_COMMIT_COPY_TRUTH_REPORT.md`
- `qa/_compare_failures_d4_d5.py`

## Tests

Focused (commit/copy + preemption + permit + scheduler + instance-control): **42 passed**

Full suite dogfood-05: `PASSED=299 FAILED=97`

Comparable dogfood-04: `FAILED=97`

```text
NEW_FAILURES_VS_DOGFOOD_04=[]
```

## Portable (not installed)

```text
portable Board path=C:\Users\user\Desktop\4_Projects_Ju\juactl-v2-dogfood-05\qa\artifacts\portable\JuActlBoard.exe
portable Board SHA256=3F29FC4DDD8082108F01776E445F2140C71D7992504B519CC94CDEAB2F385779
portable CLI path=C:\Users\user\Desktop\4_Projects_Ju\juactl-v2-dogfood-05\qa\artifacts\portable\JuActl.exe
portable CLI SHA256=4FB97EB5B281F7A076C1200DAD67A18A1098277FD90435F452A078F0C8575719
```

## Explicit answers

- Authoritative SUBMITTED commit point: **managed `send` response success** (`commitPoint=managed_send_response`)
- Cleanup after commit: **YES** (deferred / best-effort)
- Cleanup can block COPY: **NO**
- Clipboard NEW/PENDING/STALE/NO: **only NEW writes**
- Failed SEND can modify clipboard: **NO**
- Stable / dogfood-03 / dogfood-04 untouched: **YES**

## Recorded but not fixed (out of scope)

- slow runtime discovery after ASUS boot
- repeated preview 10s timeout
- updater HTTP 404

## Founder E2E

1. Launch **this** portable Board only (do not install over V1).
2. Unique marker SEND → Codex reply.
3. COPY RESULT → NEW_RESULT; clipboard exact marker.
4. Direct `actl extract codex <pane>` matches marker.
5. PENDING/STALE must not overwrite a sentinel clipboard value.
