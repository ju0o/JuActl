#!/usr/bin/env bash
set -euo pipefail

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$root"

if [[ "${1:-}" == "--offline" || "${1:-}" == "offline" ]]; then
  echo "[offline] disposable stub-agent Board SEND -> RESULT -> COPY"
  PYTHONPATH=src python3 tests/run_tests.py
  echo "TESTER_OFFLINE_COMPLETE"
  exit 0
fi

target=${1:-asus}

echo "[1/2] automated persona and regression suite"
bash scripts/qa.sh

echo "[2/2] live ASUS transport gate ($target)"
before=$(ssh -o BatchMode=yes -o ConnectTimeout=5 "$target" \
  'tmux list-sessions -F "#{session_id} #{session_name} #{session_windows}"' 2>&1) || {
  echo "LIVE_BLOCKED: cannot reach SSH target $target"
  exit 2
}
if [[ -z "$before" ]]; then
  echo "LIVE_BLOCKED: no existing tmux session; tester refuses to create one"
  exit 2
fi

PYTHONPATH=src TESTER_TARGET="$target" python3 - <<'PY'
import os
from actl.core import tmux
from actl.core.discovery import discover

target = os.environ["TESTER_TARGET"]
tmux.set_remote_ssh(target)
try:
    snapshots = []
    for _ in range(8):
        snapshots.append(len(tmux.list_panes()))
    detections = discover()
    print("LIVE_PANE_COUNTS=" + ",".join(map(str, snapshots)))
    print("LIVE_TRANSPORT_STATE=" + tmux._REMOTE_TRANSPORT.health())
    print("LIVE_EVENTS=" + str(len(tmux.remote_events())))
    print("LIVE_DETECTIONS=" + str(len(detections)))
    assert snapshots and len(set(snapshots)) == 1
    assert tmux._REMOTE_TRANSPORT.health() == "READY"
finally:
    tmux.set_remote_ssh(None)
PY

after=$(ssh -o BatchMode=yes -o ConnectTimeout=5 "$target" \
  'tmux list-sessions -F "#{session_id} #{session_name} #{session_windows}"')
printf 'LIVE_SESSIONS_BEFORE=%s\n' "$before"
printf 'LIVE_SESSIONS_AFTER=%s\n' "$after"
if [[ "$before" != "$after" ]]; then
  echo "LIVE_FAIL: tmux session inventory changed"
  exit 1
fi
echo "TESTER_COMPLETE"
