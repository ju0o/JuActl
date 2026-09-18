"""JuActl Board exe entry: GUI only, ssh asus baked in.

PyInstaller target: ``pyinstaller --onefile --noconsole src/juactl-board.py``.
Errors surface as a message box (no console to read in --noconsole mode).
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "actl"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

SSH_TARGET = os.environ.get("JUACTL_SSH", "asus")


def main() -> int:
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


if __name__ == "__main__":
    raise SystemExit(main())
