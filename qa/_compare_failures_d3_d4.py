from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path


def fail_set_from_pytest(cwd: Path) -> set[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = "src"
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "tests", "--tb=no", "-q"],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    fails: set[str] = set()
    for line in (proc.stdout or "").splitlines():
        if line.startswith("FAILED "):
            fails.add(line[len("FAILED ") :].split(" - ", 1)[0].strip())
    (cwd / "qa" / "test-logs").mkdir(parents=True, exist_ok=True)
    return fails, proc.stdout, proc.returncode  # type: ignore[return-value]


def fail_set_from_log(path: Path) -> set[str]:
    text = path.read_text(encoding="utf-8", errors="replace")
    fails: set[str] = set()
    for line in text.splitlines():
        if line.startswith("FAILED "):
            fails.add(line[len("FAILED ") :].split(" - ", 1)[0].strip())
    return fails


def main() -> None:
    d3 = Path(r"C:\Users\user\Desktop\4_Projects_Ju\juactl-v2-dogfood-03")
    d4 = Path(r"C:\Users\user\Desktop\4_Projects_Ju\juactl-v2-dogfood-04")
    f4 = fail_set_from_log(d4 / "qa" / "test-logs" / "dogfood04-full.txt")
    print("re-running dogfood-03 full suite for comparable FAILED lines...")
    f3, out3, _rc = fail_set_from_pytest(d3)  # type: ignore[misc]
    (d4 / "qa" / "test-logs" / "dogfood03-baseline-rerun.txt").write_text(out3, encoding="utf-8")
    # Normalize node ids to function name for loose compare if paths differ
    def norm(s: str) -> str:
        return s.replace("\\", "/").split("::")[-1] if "::" in s else s

    f3n = {norm(x) for x in f3}
    f4n = {norm(x) for x in f4}
    new = sorted(f4n - f3n)
    gone = sorted(f3n - f4n)
    print(f"d3_fails={len(f3)} d4_fails={len(f4)}")
    print(f"NEW_FAILURES_VS_DOGFOOD_03={new}")
    print(f"RESOLVED_COUNT={len(gone)}")
    print(f"NEW_COUNT={len(new)}")


if __name__ == "__main__":
    main()
