#!/usr/bin/env bash
set -euo pipefail

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$root"

echo "[1/4] compile"
python3 -m py_compile $(find src tests -name '*.py' -print)

echo "[2/4] tests"
PYTHONPATH=src python3 tests/run_tests.py

echo "[3/4] doctor JSON contract"
doctor_file=$(mktemp)
trap 'rm -f "$doctor_file"' EXIT
PYTHONPATH=src python3 -m actl.cli doctor --json >"$doctor_file"
python3 - "$doctor_file" <<'PY'
import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
assert isinstance(payload, dict)
assert isinstance(payload.get("ok"), bool)
assert isinstance(payload.get("checks"), list)
print("doctor JSON: valid; environment_ok=" + str(payload["ok"]))
PY

echo "[4/4] diff whitespace"
git diff --check
echo "QA_COMPLETE"
