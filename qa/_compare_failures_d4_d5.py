from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def fail_set(cwd: Path) -> set[str]:
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
    (cwd / "qa" / "test-logs").mkdir(parents=True, exist_ok=True)
    return fails, proc.stdout  # type: ignore[return-value]


def main() -> None:
    d4 = Path(r"C:\Users\user\Desktop\4_Projects_Ju\juactl-v2-dogfood-04")
    d5 = Path(r"C:\Users\user\Desktop\4_Projects_Ju\juactl-v2-dogfood-05")
    print("collecting dogfood-04 fails...")
    f4, out4 = fail_set(d4)  # type: ignore[misc]
    (d5 / "qa" / "test-logs" / "dogfood04-baseline-rerun.txt").write_text(out4, encoding="utf-8")
    f5: set[str] = set()
    text = (d5 / "qa" / "test-logs" / "dogfood05-full.txt").read_text(encoding="utf-8", errors="replace")
    for line in text.splitlines():
        if line.startswith("FAILED "):
            f5.add(line[len("FAILED ") :].split(" - ", 1)[0].strip().split("::")[-1])
    new = sorted(f5 - f4)
    print(f"d4_fails={len(f4)} d5_fails={len(f5)}")
    print(f"NEW_FAILURES_VS_DOGFOOD_04={new}")
    print(f"NEW_COUNT={len(new)}")


if __name__ == "__main__":
    main()
