from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def fails_from_log(path: Path) -> set[str]:
    text = path.read_text(encoding="utf-8", errors="replace")
    out: set[str] = set()
    for line in text.splitlines():
        if line.startswith("FAILED "):
            out.add(line[len("FAILED ") :].split(" - ", 1)[0].strip().split("::")[-1])
    return out


def fails_from_pytest(cwd: Path) -> tuple[set[str], str]:
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
            fails.add(line[len("FAILED ") :].split(" - ", 1)[0].strip().split("::")[-1])
    return fails, proc.stdout or ""


def main() -> None:
    d5 = Path(r"C:\Users\user\Desktop\4_Projects_Ju\juactl-v2-dogfood-05")
    d6 = Path(r"C:\Users\user\Desktop\4_Projects_Ju\juactl-v2-dogfood-06")
    print("rerunning dogfood-05 for comparable FAILED set...")
    f5, out5 = fails_from_pytest(d5)
    (d6 / "qa" / "test-logs" / "dogfood05-baseline-rerun.txt").write_text(out5, encoding="utf-8")
    f6 = fails_from_log(d6 / "qa" / "test-logs" / "dogfood06-full.txt")
    new = sorted(f6 - f5)
    print(f"d5_fails={len(f5)} d6_fails={len(f6)}")
    print(f"NEW_FAILURES_VS_DOGFOOD_05={new}")
    print(f"NEW_COUNT={len(new)}")


if __name__ == "__main__":
    main()
