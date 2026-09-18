"""Dependency-free test runner; pytest remains optional."""
from __future__ import annotations

import importlib
import inspect
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]


class MonkeyPatch:
    def __init__(self): self.undo = []
    def setattr(self, obj, name, value):
        self.undo.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)
    def close(self):
        for obj, name, old in reversed(self.undo): setattr(obj, name, old)


def main() -> int:
    passed = failed = 0
    for file in sorted((ROOT / "tests").glob("test_*.py")):
        mod = importlib.import_module(file.stem)
        for name, fn in inspect.getmembers(mod, inspect.isfunction):
            if not name.startswith("test_"):
                continue
            args = []
            tmps: list[Path] = []
            patch = MonkeyPatch()
            try:
                for param in inspect.signature(fn).parameters:
                    if param == "tmp_path":
                        tmp = Path(tempfile.mkdtemp(prefix="actl-test-"))
                        tmps.append(tmp)
                        args.append(tmp)
                    elif param == "monkeypatch": args.append(patch)
                    else: raise RuntimeError(f"unsupported fixture: {param}")
                fn(*args); passed += 1; print(f"PASS {file.stem}.{name}")
            except Exception as exc:
                failed += 1; print(f"FAIL {file.stem}.{name}: {exc}")
            finally:
                patch.close()
                for tmp in tmps:
                    shutil.rmtree(tmp, ignore_errors=True)
    print(f"{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__": raise SystemExit(main())
