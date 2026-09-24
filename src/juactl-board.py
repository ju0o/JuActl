"""JuActl Board exe entry: GUI only, ssh asus baked in.

PyInstaller target: ``pyinstaller --onefile --noconsole src/juactl-board.py``.
Errors surface as a message box (no console to read in --noconsole mode).
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "actl"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

SSH_TARGET = os.environ.get("JUACTL_SSH", "asus")
BOARD_ALREADY_RUNNING = "JuActl Board가 이미 실행 중입니다."


def _acquire_instance_lock():
    path = Path(os.environ.get("ACTL_STATE_PATH", "~/.local/state/actl/state.json")).expanduser().parent / "juactl-board.lock"
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    handle = path.open("a+b")
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        handle.close()
        return None
    return handle


def main() -> int:
    lock = _acquire_instance_lock()
    if lock is None:
        try:
            import tkinter as tk
            from tkinter import messagebox
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror("JuActl Board", BOARD_ALREADY_RUNNING)
            root.destroy()
        except Exception:
            pass
        return 1
    try:
        from actl.gui import run_gui

        return run_gui(ssh_target=SSH_TARGET)
    except Exception as exc:
        try:
            import tkinter as tk
            from tkinter import messagebox

            root = tk.Tk()
            root.withdraw()
            messagebox.showerror("JuActl Board", f"시작 실패:\n{exc}")
            root.destroy()
        except Exception:
            pass
        return 1
    finally:
        if os.name == "nt":
            try:
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        lock.close()


if __name__ == "__main__":
    raise SystemExit(main())
