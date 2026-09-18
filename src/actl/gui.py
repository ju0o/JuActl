"""JuActl GUI: Windows native agent board (tkinter, stdlib only).

No terminal input needed — buttons + clicks only. Reuses the same remote
backend as the TUI (--ssh delegation for tmux/ps/extract).

Layout mirrors board-wireframe.html:
  left    agent list (click = select + preview)
  center  live pane preview + Copy/Print/Remap/Refresh buttons + last response
  right   message composer + Send + event log
"""
from __future__ import annotations

import queue
import threading

from actl.agents.extract import extract_last_response
from actl.core.config import backup_config, get_target, load_config, save_config
from actl.core.discovery import STRONG_CONFIDENCE, discover, manual_map
from actl.core.registry import AGENTS
from actl.core.validation import validate_target
from actl.tui import STATE_KO, _pane_board, _pane_preview, _unmapped_panes, _verify_row
from actl.utils.clipboard import copy_text


def _tk():
    try:
        import tkinter as tk
        from tkinter import scrolledtext, ttk
    except ImportError as exc:
        raise SystemExit(f"tkinter 없음 (python.org python 재설치 시 Tcl/Tk 포함): {exc}")
    return tk, scrolledtext, ttk


class Board:
    def __init__(self, ssh_target: str | None = None) -> None:
        import tkinter as tk

        if ssh_target:
            from actl.core.tmux import set_remote_ssh

            set_remote_ssh(ssh_target)
        self.ssh_target = ssh_target
        self.config = load_config()
        try:
            from actl.cli import _auto_reconcile

            self.config = _auto_reconcile(self.config)
        except Exception:
            pass
        self.root = tk.Tk()
        self.root.title("JuActl — MainPC 에이전트 보드" + (f" (ssh {ssh_target})" if ssh_target else ""))
        self.root.geometry("1100x640")
        self.selected: str | None = None
        self.rows: list[dict] = []
        self.jobs: queue.Queue = queue.Queue()
        self._build()
        self.refresh()
        self.root.after(100, self._drain)

    def _build(self) -> None:
        import tkinter as tk
        from tkinter import ttk

        main = ttk.Frame(self.root, padding=8)
        main.pack(fill="both", expand=True)
        main.columnconfigure(0, weight=1)
        main.columnconfigure(1, weight=2)
        main.columnconfigure(2, weight=1)

        left = ttk.LabelFrame(main, text="에이전트 (클릭=선택+미리보기)", padding=6)
        left.grid(row=0, column=0, sticky="nsew", padx=4)
        self.agent_box = tk.Listbox(left, height=12, font=("Consolas", 11))
        self.agent_box.pack(fill="both", expand=True)
        self.agent_box.bind("<<ListboxSelect>>", lambda _e: self.on_select())

        center = ttk.LabelFrame(main, text="live pane 미리보기", padding=6)
        center.grid(row=0, column=1, sticky="nsew", padx=4)
        self.preview = tk.Text(center, height=16, wrap="word", font=("Consolas", 10))
        self.preview.pack(fill="both", expand=True)
        btns = ttk.Frame(center)
        btns.pack(fill="x", pady=4)
        for label, fn in [("복사", self.on_copy), ("출력", self.on_print),
                          ("재매핑", self.on_remap), ("pane보드", self.on_board),
                          ("새로고침", self.refresh)]:
            ttk.Button(btns, text=label, command=fn).pack(side="left", padx=2)
        self.resp = tk.Text(center, height=8, wrap="word", font=("Consolas", 10))
        self.resp.pack(fill="both", expand=True)

        right = ttk.LabelFrame(main, text="메시지 전송", padding=6)
        right.grid(row=0, column=2, sticky="nsew", padx=4)
        from tkinter import scrolledtext

        self.msg = scrolledtext.ScrolledText(right, height=8, font=("Consolas", 10))
        self.msg.pack(fill="x")
        ttk.Button(right, text="전송", command=self.on_send).pack(pady=4)
        ttk.Label(right, text="이벤트 로그").pack(anchor="w")
        self.logw = scrolledtext.ScrolledText(right, height=14, state="disabled", font=("Consolas", 9))
        self.logw.pack(fill="both", expand=True)

    def log(self, text: str) -> None:
        import datetime

        self.logw.configure(state="normal")
        self.logw.insert("1.0", f"[{datetime.datetime.now().strftime('%H:%M:%S')}] {text}\n")
        self.logw.configure(state="disabled")

    def _bg(self, fn, done) -> None:
        def run() -> None:
            try:
                result = fn()
            except Exception as exc:
                result = exc
            self.jobs.put((done, result))

        threading.Thread(target=run, daemon=True).start()

    def _drain(self) -> None:
        try:
            while True:
                done, result = self.jobs.get_nowait()
                done(result)
        except queue.Empty:
            pass
        self.root.after(100, self._drain)

    def rows_now(self) -> list[dict]:
        from actl.tui import _rows

        return _rows(self.config)

    def refresh(self) -> None:
        self.log("새로고침 중…")
        self._bg(self.rows_now, self._refresh_done)

    def _refresh_done(self, result) -> None:
        if isinstance(result, Exception):
            self.log(f"새로고침 실패: {result}")
            return
        self.rows = result
        self.agent_box.delete(0, "end")
        for i, r in enumerate(self.rows, 1):
            state = STATE_KO.get(r["state"], r["state"])
            self.agent_box.insert("end", f"[{i}] {r['display']} {r['target']} {state}")
        self.log(f"새로고침 완료 ({len(self.rows)} agents)")
        if self.selected is None and self.rows:
            self.agent_box.selection_set(0)
            self.on_select()

    def current(self) -> dict | None:
        try:
            idx = self.agent_box.curselection()[0]
        except IndexError:
            return None
        if idx >= len(self.rows):
            return None
        return self.rows[idx]

    def on_select(self) -> None:
        row = self.current()
        if not row:
            return
        self.selected = row["agent"]
        tgt = row["target"]
        if tgt == "-" or tgt.endswith("?"):
            self.preview.delete("1.0", "end")
            self.preview.insert("end", f"{row['display']}: live pane 없음 — 재매핑 버튼 사용")
            return
        self.log(f"{row['display']} 미리보기 로딩…")

        def work():
            return _verify_row(row["agent"], tgt, self.config) + "\n" + _pane_preview(tgt)

        def done(result) -> None:
            self.preview.delete("1.0", "end")
            self.preview.insert("end", result if isinstance(result, str) else f"실패: {result}")

        self._bg(work, done)

    def on_copy(self) -> None:
        row = self.current()
        if not row:
            return
        tgt = row["target"]
        if tgt == "-" or tgt.endswith("?"):
            self.log(f"{row['display']} live pane 없음")
            return

        def work():
            result = extract_last_response(row["agent"], tgt, self.config)
            if not result.text:
                return ("empty", result.detail)
            try:
                backend = copy_text(result.text, preferred=self.config.get("clipboard_backend", "auto"))
                return ("ok", backend, len(result.text))
            except Exception as exc:
                return ("clip-fail", str(exc), result.text[:2000])

        def done(result) -> None:
            if result[0] == "ok":
                self.log(f"{row['display']} 복사됨 ({result[1]}, {result[2]}자)")
            elif result[0] == "empty":
                self.log(f"응답 없음 ({result[1] or '비어 있음'})")
            else:
                self.resp.delete("1.0", "end")
                self.resp.insert("end", f"클립보드 실패: {result[1]}\n--- 수동 복사 ---\n{result[2]}")
                self.log("클립보드 실패 — 응답을 화면에 출력")

        self._bg(work, done)

    def on_print(self) -> None:
        row = self.current()
        if not row:
            return
        tgt = row["target"]
        if tgt == "-" or tgt.endswith("?"):
            self.log(f"{row['display']} live pane 없음")
            return

        def work():
            return extract_last_response(row["agent"], tgt, self.config)

        def done(result) -> None:
            self.resp.delete("1.0", "end")
            if isinstance(result, Exception):
                self.resp.insert("end", f"실패: {result}")
            elif result.text:
                self.resp.insert("end", result.text[:8000])
            else:
                self.resp.insert("end", f"응답 없음 ({result.detail or '비어 있음'})")

        self._bg(work, done)

    def on_remap(self) -> None:
        import tkinter as tk

        row = self.current()
        if not row:
            return
        dets = _unmapped_panes(self.config, row["agent"])
        if not dets:
            self.log(f"빈 {row['display']} live pane 없음")
            return
        top = tk.Toplevel(self.root)
        top.title(f"{row['display']} 재매핑")
        for i, det in enumerate(dets, 1):
            tk.Button(
                top, anchor="w",
                text=f"[{i}] {det.pane.pane_id} {det.pane.current_path} — {det.evidence}",
                command=lambda d=det: (self._do_remap(row, d), top.destroy()),
            ).pack(fill="x", padx=8, pady=2)

    def _do_remap(self, row: dict, det) -> None:
        try:
            updated = manual_map(self.config, row["agent"], det)
        except ValueError as exc:
            self.log(f"매핑 실패: {exc}")
            return
        backup = backup_config()
        save_config(updated)
        self.config = updated
        self.log(f"{row['display']} → {det.pane.pane_id} 매핑됨 (백업 {backup.name})")
        self.refresh()

    def on_board(self) -> None:
        def work():
            return _pane_board(self.config)

        def done(result) -> None:
            self.preview.delete("1.0", "end")
            self.preview.insert("end", result if isinstance(result, str) else f"실패: {result}")

        self._bg(work, done)

    def on_send(self) -> None:
        row = self.current()
        if not row:
            return
        text = self.msg.get("1.0", "end").strip()
        if not text:
            self.log("빈 메시지 — 전송 안 함")
            return

        def work():
            from actl.cli import _send_to_selected

            _send_to_selected(self.config, row["agent"], text)
            return True

        def done(result) -> None:
            if result is True:
                self.log(f"{row['display']}에 전송됨")
                self.msg.delete("1.0", "end")
            else:
                self.log(f"전송 실패: {result}")

        self._bg(work, done)

    def run(self) -> int:
        self.root.mainloop()
        return 0


def run_gui(ssh_target: str | None = None) -> int:
    return Board(ssh_target=ssh_target).run()
