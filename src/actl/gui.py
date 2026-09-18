"""JuActl GUI: Windows native agent board (tkinter, stdlib only).

Dark theme, status colors, card layout. No terminal input — buttons and
clicks only. Reuses the same remote backend as the TUI (--ssh delegation
for tmux/ps/extract).

Layout mirrors board-wireframe.html:
  left    agent cards (click = select + preview)
  center  live pane preview + Copy/Print/Remap/Board/Refresh + last response
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

BG = "#0a0a12"
PANEL = "#12121f"
PANEL2 = "#1a1a2e"
LINE = "#2a2a45"
TXT = "#e8e8f2"
DIM = "#6e6e8c"
NEON = "#00f0ff"
MAGENTA = "#ff2fb3"
LIME = "#a6ff00"
OK = "#00ff9d"
WARN = "#ffb300"
BAD = "#ff3355"
ACC = NEON
FONT = ("Consolas", 10)
FONT_BIG = ("Consolas", 11, "bold")
FONT_HDR = ("Consolas", 9, "bold")
GLITCH_A = "▓▒░"
STATUS_COLOR = {"UP": OK, "DOWN": BAD, "MISMATCH": WARN, "UNMAPPED": DIM, "DETECTED": NEON}
STATUS_GLYPH = {"UP": "●", "DOWN": "✖", "MISMATCH": "◈", "UNMAPPED": "○", "DETECTED": "◉"}


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
        self.root.geometry("1600x950")
        self.root.minsize(1100, 700)
        self.root.configure(bg=BG)
        self.selected: str | None = None
        self.rows: list[dict] = []
        self.jobs: queue.Queue = queue.Queue()
        self.auto_refresh = True
        self.refreshing = False
        self.refresh_interval_ms = 12000
        self.previous_rows: dict[str, dict] = {}
        self.log_visible = False
        self._build()
        self.refresh()
        self.root.after(100, self._drain)
        self.root.after(self.refresh_interval_ms, self._auto_tick)

    def _style(self) -> None:
        from tkinter import ttk

        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("TFrame", background=BG)
        style.configure("Card.TFrame", background=PANEL, borderwidth=1, relief="solid")
        style.configure("TLabel", background=PANEL, foreground=TXT, font=FONT)
        style.configure("Title.TLabel", background=BG, foreground=NEON, font=FONT_HDR)
        style.configure("TButton", font=FONT, padding=4)
        style.configure("Primary.TButton", background=ACC, foreground="white")

    def _btn(self, parent, text: str, fn, primary: bool = False):
        import tkinter as tk

        bg = "#003844" if primary else "#1e1e35"
        fg = NEON if primary else TXT
        return tk.Button(parent, text=text, command=fn, bg=bg, fg=fg,
                         activebackground="#005566", activeforeground="#ffffff",
                         relief="flat", padx=12, pady=5, cursor="hand2",
                         font=("Consolas", 10, "bold" if primary else "normal"))

    def _build(self) -> None:
        import tkinter as tk
        from tkinter import ttk

        self._style()
        top = tk.Frame(self.root, bg="#05050c", highlightbackground=NEON, highlightthickness=1)
        top.pack(fill="x", padx=6, pady=(6, 0))
        conn = f"◈ ssh {self.ssh_target}" if self.ssh_target else "◈ 로컬"
        tk.Label(top, text=f"▓▒░ JUACTL // SIGNAL BOARD ░▒▓  {conn}", bg="#05050c", fg=NEON,
                 font=("Consolas", 12, "bold")).pack(side="left", padx=10, pady=6)
        self.auto_var = tk.StringVar(value="◉ 자동새로고침 ON (12s)")
        tk.Button(top, textvariable=self.auto_var, command=self.toggle_auto,
                  bg="#05050c", fg=DIM, relief="flat", cursor="hand2").pack(side="left", padx=12)
        self.summary_var = tk.StringVar(value="정상 0 · 문제 0 · 미확인 0")
        tk.Label(top, textvariable=self.summary_var, bg="#05050c", fg=DIM,
                 font=FONT_HDR).pack(side="left", padx=8)
        self.status_var = tk.StringVar(value="준비")
        tk.Label(top, textvariable=self.status_var, bg="#05050c", fg=MAGENTA,
                 font=FONT_HDR).pack(side="right", padx=10)

        main = ttk.Frame(self.root, padding=6)
        main.pack(fill="both", expand=True)
        main.columnconfigure(0, weight=3)
        main.columnconfigure(1, weight=2)
        main.rowconfigure(0, weight=1)

        left = tk.Frame(main, bg=PANEL, highlightbackground=NEON, highlightthickness=1)
        left.grid(row=0, column=0, sticky="nsew", padx=4)
        left.rowconfigure(2, weight=5)
        left.rowconfigure(5, weight=3)
        self.pane_title = tk.StringVar(value="▚ live pane — 에이전트 클릭")
        tk.Label(left, textvariable=self.pane_title, bg=PANEL, fg=NEON, font=FONT_HDR).grid(row=0, column=0, sticky="w", padx=6, pady=4)
        self.detail_var = tk.StringVar(value="대상을 선택하면 상태와 작업 가능 여부가 표시됩니다")
        tk.Label(left, textvariable=self.detail_var, bg=PANEL, fg=DIM, font=("Consolas", 9),
                 anchor="w").grid(row=0, column=0, sticky="e", padx=6, pady=4)
        self.preview = tk.Text(left, wrap="none", font=("Consolas", 13), bg="#05050c", fg="#d8ffd8",
                               insertbackground=NEON, highlightthickness=0, borderwidth=0)
        self.preview.grid(row=2, column=0, sticky="nsew", padx=6)
        cmdbar = tk.Frame(left, bg=PANEL)
        cmdbar.grid(row=1, column=0, sticky="ew", pady=4, padx=6)
        for label, primary in [("⧉ 복사", True), ("⎙ 출력", False), ("⇄ 재매핑", False),
                               ("▦ pane보드", False), ("↻ 새로고침", False)]:
            fn = {"⧉ 복사": self.on_copy, "⎙ 출력": self.on_print, "⇄ 재매핑": self.on_remap,
                  "▦ pane보드": self.on_board, "↻ 새로고침": self.refresh}[label]
            self._btn(cmdbar, label, fn, primary=primary).pack(side="left", padx=3)
        tk.Label(left, text="▚ 마지막 응답", bg=PANEL, fg=MAGENTA, font=FONT_HDR).grid(row=4, column=0, sticky="w", padx=6)
        self.resp = tk.Text(left, wrap="word", font=FONT, bg="#05050c", fg=TXT,
                            highlightthickness=0, borderwidth=0)
        self.resp.grid(row=5, column=0, sticky="nsew", padx=6, pady=4)

        right = tk.Frame(main, bg=BG, highlightthickness=0)
        right.grid(row=0, column=1, sticky="nsew", padx=4)
        right.rowconfigure(2, weight=1)
        right.rowconfigure(6, weight=1)
        tk.Label(right, text="▚ 에이전트", bg=BG, fg=DIM, font=FONT_HDR).grid(row=0, column=0, sticky="w", padx=2, pady=2)
        tools = tk.Frame(right, bg=BG)
        tools.grid(row=1, column=0, sticky="ew", pady=(0, 4))
        self.filter_var = tk.StringVar()
        search = tk.Entry(tools, textvariable=self.filter_var, bg=PANEL, fg=TXT,
                          insertbackground=NEON, relief="flat", highlightthickness=1,
                          highlightbackground=LINE, highlightcolor=NEON)
        search.pack(side="left", fill="x", expand=True, padx=(0, 5))
        self.search = search
        search.insert(0, "검색…")
        search.bind("<FocusIn>", lambda _e: search.delete(0, "end") if search.get() == "검색…" else None)
        search.bind("<KeyRelease>", lambda _e: self._render_cards(self.selected))
        self.filter_mode = tk.StringVar(value="전체")
        mode_menu = tk.OptionMenu(tools, self.filter_mode, "전체", "정상", "문제", "미확인",
                                  command=lambda _v: self._render_cards(self.selected))
        mode_menu.configure(bg=PANEL2, fg=TXT, activebackground=NEON, activeforeground="#05050c",
                            relief="flat", highlightthickness=0)
        mode_menu["menu"].configure(bg=PANEL2, fg=TXT, activebackground=NEON, activeforeground="#05050c")
        mode_menu.pack(side="right")
        self.cards: dict[str, tk.Frame] = {}
        self.agent_cards = tk.Frame(right, bg=BG)
        self.agent_cards.grid(row=2, column=0, sticky="nsew")
        tk.Label(right, text="▚ 메시지 전송", bg=BG, fg=DIM, font=FONT_HDR).grid(row=3, column=0, sticky="w", padx=2, pady=2)
        from tkinter import scrolledtext

        self.msg = scrolledtext.ScrolledText(right, height=5, font=FONT, bg=PANEL, fg=TXT,
                                             insertbackground=NEON, highlightthickness=0, borderwidth=0)
        self.msg.grid(row=4, column=0, sticky="ew", pady=2)
        sendrow = tk.Frame(right, bg=BG)
        sendrow.grid(row=5, column=0, sticky="nsew", pady=2)
        self.send_btn = self._btn(sendrow, "➤ 전송", self.on_send, primary=True)
        self.send_btn.pack(side="left")
        self.log_toggle = tk.Button(sendrow, text="▸ 로그", command=self.toggle_log,
                                    bg=BG, fg=DIM, relief="flat", cursor="hand2")
        self.log_toggle.pack(side="left", padx=6)
        self.logw = scrolledtext.ScrolledText(right, height=8, state="disabled", font=("Consolas", 9),
                                              bg=PANEL, fg=DIM, highlightthickness=0, borderwidth=0)
        self.root.bind("<F5>", lambda _e: self.refresh())
        self.root.bind("<Control-k>", lambda _e: self.command_palette())
        self.root.bind("<Control-l>", lambda _e: search.focus_set())
        self.root.bind("<Control-Return>", lambda _e: self.on_send())
        self.root.bind("<Escape>", lambda _e: self.root.focus_set())

    def toggle_log(self) -> None:
        if self.log_visible:
            self.logw.grid_forget()
            self.log_toggle.configure(text="▸ 로그")
        else:
            self.logw.grid(row=6, column=0, sticky="nsew", pady=2)
            self.log_toggle.configure(text="▾ 로그")
        self.log_visible = not self.log_visible

    def command_palette(self) -> None:
        import tkinter as tk

        win = tk.Toplevel(self.root)
        win.title("JuActl Command Palette")
        win.configure(bg=BG)
        win.transient(self.root)
        win.geometry("430x250")
        tk.Label(win, text="COMMAND // 실행할 작업 선택", bg=BG, fg=NEON, font=FONT_HDR).pack(anchor="w", padx=12, pady=10)
        actions = [("새로고침", self.refresh), ("자동새로고침 전환", self.toggle_auto),
                   ("문제만 보기", lambda: (self.filter_mode.set("문제"), self._render_cards(self.selected))),
                   ("전체 보기", lambda: (self.filter_mode.set("전체"), self._render_cards(self.selected))),
                   ("검색창 포커스", lambda: self.search.focus_set())]
        for label, action in actions:
            tk.Button(win, text=label, anchor="w", command=lambda a=action: (a(), win.destroy()),
                      bg=PANEL, fg=TXT, activebackground=NEON, activeforeground=BG,
                      relief="flat", font=FONT, padx=10, pady=6).pack(fill="x", padx=12, pady=2)
        win.bind("<Escape>", lambda _e: win.destroy())
        win.focus_force()

    def set_status(self, text: str) -> None:
        self.status_var.set(text)

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

    def toggle_auto(self) -> None:
        self.auto_refresh = not self.auto_refresh
        self.auto_var.set(f"◉ 자동새로고침 ON ({self.refresh_interval_ms // 1000}s)" if self.auto_refresh else "◌ 자동새로고침 OFF")
        self.log(f"자동새로고침 {'켬' if self.auto_refresh else '끔'}")

    def _auto_tick(self) -> None:
        if self.auto_refresh:
            self.refresh(quiet=True)
        self.root.after(self.refresh_interval_ms, self._auto_tick)

    def rows_now(self) -> list[dict]:
        from actl.tui import _rows

        return _rows(self.config)

    def refresh(self, quiet: bool = False) -> None:
        if self.refreshing:
            return
        self.refreshing = True
        self.set_status("새로고침 중…")
        if not quiet:
            self.log("새로고침 중…")
        self._bg(self.rows_now, lambda r: self._refresh_done(r, quiet))

    def _refresh_done(self, result, quiet: bool = False) -> None:
        import tkinter as tk

        if isinstance(result, Exception):
            self.refreshing = False
            self.set_status("새로고침 실패")
            self.log(f"새로고침 실패: {result}")
            return
        prev_sel = self.selected
        from actl.core.events import detect_events

        self.rows = result
        if self.previous_rows:
            for event in detect_events(self.previous_rows, self.rows):
                self.log(f"◆ {event['agent']} · {event['detail']}")
        self.previous_rows = {r["agent"]: r for r in self.rows}
        self.refresh_interval_ms = 3000 if any(r.get("activity_state") == "RUNNING" for r in self.rows) else 12000
        self.auto_var.set(f"◉ 자동새로고침 ON ({self.refresh_interval_ms // 1000}s)" if self.auto_refresh else "◌ 자동새로고침 OFF")
        self._render_cards(prev_sel)
        counts = {
            "정상": sum(r["state"] == "UP" for r in self.rows),
            "문제": sum(r["state"] in {"DOWN", "MISMATCH"} for r in self.rows),
            "미확인": sum(r["state"] not in {"UP", "DOWN", "MISMATCH"} for r in self.rows),
        }
        self.summary_var.set(" · ".join(f"{key} {value}" for key, value in counts.items()))
        live = counts["정상"]
        self.set_status(f"정상 {live}/{len(self.rows)}")
        if not quiet:
            self.log(f"새로고침 완료 ({len(self.rows)} agents, 정상 {live})")
        if self.rows:
            keep = prev_sel if any(r["agent"] == prev_sel for r in self.rows) else self.rows[0]["agent"]
            self.selected = keep
            self._highlight(keep)
            self._update_action_state()
        self.refreshing = False

    def _render_cards(self, selected: str | None = None) -> None:
        import tkinter as tk

        for child in self.agent_cards.winfo_children():
            child.destroy()
        self.cards = {}
        query = self.filter_var.get().strip().lower()
        if query == "검색…":
            query = ""
        mode = self.filter_mode.get()
        visible = []
        for r in self.rows:
            haystack = f"{r['display']} {r['agent']} {r['target']}".lower()
            matches_mode = (mode == "전체" or
                            (mode == "정상" and r["state"] == "UP") or
                            (mode == "문제" and r["state"] in {"DOWN", "MISMATCH"}) or
                            (mode == "미확인" and r["state"] not in {"UP", "DOWN", "MISMATCH"}))
            if matches_mode and (not query or query in haystack):
                visible.append(r)
        visible.sort(key=lambda r: (
            0 if r.get("unread") else 1,
            0 if r.get("activity_state") == "RUNNING" else 1,
            0 if r.get("state") in {"DOWN", "MISMATCH"} else 1,
            r.get("display", ""),
        ))
        for r in visible:
            state = STATE_KO.get(r["state"], r["state"])
            glyph = STATUS_GLYPH.get(r["state"], "·")
            color = STATUS_COLOR.get(r["state"], TXT)
            card = tk.Frame(self.agent_cards, bg=PANEL, highlightbackground=color,
                            highlightthickness=1 if r["agent"] == selected else 0,
                            cursor="hand2")
            card.pack(fill="x", pady=2)
            top = tk.Frame(card, bg=PANEL)
            top.pack(fill="x", padx=8, pady=(6, 0))
            tk.Label(top, text=f"{glyph} {r['display']}", bg=PANEL, fg=color,
                     font=("Consolas", 11, "bold")).pack(side="left")
            tk.Label(top, text=r["target"], bg=PANEL, fg=DIM, font=FONT).pack(side="right")
            sub = (r["preview"] or r["detail"] or "—")[:60]
            result_label = {"READY": "결과 준비", "WAITING": "결과 대기", "UNKNOWN": "결과 미확인"}.get(r.get("result_state"), "결과 미확인")
            activity = f"{r.get('busy', '-')} · {result_label}"
            tk.Label(card, text=f"{state} · {activity} · {sub}", bg=PANEL, fg=DIM, font=("Consolas", 9),
                     anchor="w", justify="left").pack(fill="x", padx=8, pady=(0, 6))
            card.bind("<Button-1>", lambda _e, a=r["agent"]: self.select_agent(a))
            for child in (card, top):
                child.bind("<Button-1>", lambda _e, a=r["agent"]: self.select_agent(a))
            for w in top.winfo_children():
                w.bind("<Button-1>", lambda _e, a=r["agent"]: self.select_agent(a))
            self.cards[r["agent"]] = card
        if not visible:
            tk.Label(self.agent_cards, text="조건에 맞는 에이전트 없음", bg=BG, fg=DIM,
                     font=FONT).pack(anchor="w", padx=8, pady=8)

    def _highlight(self, agent: str) -> None:
        import tkinter as tk

        for name, card in self.cards.items():
            row = next((r for r in self.rows if r["agent"] == name), None)
            color = STATUS_COLOR.get(row["state"], TXT) if row else LINE
            card.configure(highlightbackground=color,
                           highlightthickness=2 if name == agent else 0)

    def _update_action_state(self) -> None:
        row = self.current()
        enabled = bool(row and row["target"] not in {"-", ""} and not row["target"].endswith("?"))
        self.send_btn.configure(state="normal" if enabled else "disabled")

    def select_agent(self, agent: str) -> None:
        self.selected = agent
        self._highlight(agent)
        self._update_action_state()
        self.on_select()

    def current(self) -> dict | None:
        if self.selected:
            row = next((r for r in self.rows if r["agent"] == self.selected), None)
            if row:
                return row
        return self.rows[0] if self.rows else None

    def on_select(self) -> None:
        row = self.current()
        if not row:
            return
        self.selected = row["agent"]
        tgt = row["target"]
        result_label = {"READY": "준비됨", "WAITING": "대기", "UNKNOWN": "미확인"}.get(row.get("result_state"), "미확인")
        self.detail_var.set(f"상태 {STATE_KO.get(row['state'], row['state'])} · {row.get('busy', '활동 미확인')} · 결과 {result_label} · 대상 {tgt}")
        if tgt == "-" or tgt.endswith("?"):
            self.preview.delete("1.0", "end")
            self.preview.insert("end", f"{row['display']}: live pane 없음 — 재매핑 버튼 사용")
            self.pane_title.set(f"▚ {row['display']} — 연결할 live pane 없음")
            return
        self.set_status(f"{row['display']} 로딩…")
        self.log(f"{row['display']} 미리보기 로딩…")

        def work():
            return _verify_row(row["agent"], tgt, self.config) + "\n" + _pane_preview(tgt)

        def done(result) -> None:
            self.preview.delete("1.0", "end")
            self.preview.insert("end", result if isinstance(result, str) else f"실패: {result}")
            self.pane_title.set(f"▚ {row['display']} {tgt} — live")
            self.set_status("준비")

        self._bg(work, done)

    def on_copy(self) -> None:
        row = self.current()
        if not row:
            return
        tgt = row["target"]
        if tgt == "-" or tgt.endswith("?"):
            self.log(f"{row['display']} live pane 없음")
            return
        self.set_status("복사 중…")

        def work():
            from actl.core.audit import record
            import hashlib

            result = extract_last_response(row["agent"], tgt, self.config)
            if not result.text:
                record("copy", agent=row["agent"], target=tgt, ok=False,
                       source=result.source, confidence=result.confidence)
                return ("empty", result.detail)
            try:
                backend = copy_text(result.text, preferred=self.config.get("clipboard_backend", "auto"))
                result_hash = hashlib.sha256(result.text.encode("utf-8")).hexdigest()[:16]
                record("copy", agent=row["agent"], target=tgt, ok=True, mode=backend,
                       source=result.source, confidence=result.confidence, chars=len(result.text),
                       result_hash=result_hash)
                return ("ok", backend, len(result.text), result_hash)
            except Exception as exc:
                record("copy", agent=row["agent"], target=tgt, ok=False,
                       source=result.source, confidence=result.confidence, error=type(exc).__name__)
                return ("clip-fail", str(exc), result.text[:2000])

        def done(result) -> None:
            self.set_status("준비")
            if result[0] == "ok":
                from actl.core.state import acknowledge

                acknowledge(row["agent"], result[3])
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
                import hashlib
                from actl.core.state import acknowledge

                acknowledge(row["agent"], hashlib.sha256(result.text.encode("utf-8")).hexdigest()[:16])
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
        top.configure(bg=BG)
        tk.Label(top, text="pane 선택 (클릭=즉시 매핑)", bg=BG, fg=DIM).pack(padx=8, pady=4)
        for i, det in enumerate(dets, 1):
            tk.Button(
                top, anchor="w", bg=PANEL, fg=TXT, relief="flat",
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
        from actl.core.audit import record

        record("map", agent=row["agent"], target=det.pane.pane_id, ok=True)
        self.config = updated
        self.log(f"{row['display']} → {det.pane.pane_id} 매핑됨 (백업 {backup.name})")
        self.refresh()

    def on_board(self) -> None:
        self.set_status("pane 보드 로딩…")

        def work():
            return _pane_board(self.config)

        def done(result) -> None:
            self.preview.delete("1.0", "end")
            self.preview.insert("end", result if isinstance(result, str) else f"실패: {result}")
            self.set_status("준비")

        self._bg(work, done)

    def on_send(self) -> None:
        from tkinter import messagebox

        row = self.current()
        if not row:
            return
        text = self.msg.get("1.0", "end").strip()
        if not text:
            self.log("빈 메시지 — 전송 안 함")
            return
        if not messagebox.askyesno("전송 확인", f"{row['display']} ({row['target']})에 메시지를 전송할까요?\n\n{text[:240]}{'…' if len(text) > 240 else ''}"):
            return
        self.set_status("전송 중…")

        def work():
            from actl.cli import _send_to_selected

            _send_to_selected(self.config, row["agent"], text)
            return True

        def done(result) -> None:
            self.set_status("준비")
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
