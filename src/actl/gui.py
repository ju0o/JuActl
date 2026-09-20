"""JuActl GUI: Windows native agent board (tkinter, stdlib only).

Apple-inspired light canvas, dark live-pane tile, status colors, card layout. No terminal input — buttons and
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
import time

from actl.agents.extract import extract_last_response
from actl.core.config import backup_config, get_target, load_config, save_config
from actl.core.discovery import Detection, STRONG_CONFIDENCE, discover, manual_map, reconcile
from actl.core.registry import AGENTS
from actl.core.validation import validate_target
from actl.tui import STATE_KO, _all_panes, _pane_board, _pane_preview, _unmapped_panes, _verify_row
from actl.utils.clipboard import copy_text

BG = "#f5f5f7"
PANEL = "#ffffff"
PANEL2 = "#f2f2f7"
LINE = "#d2d2d7"
TXT = "#1d1d1f"
DIM = "#6e6e73"
NEON = "#0066cc"
MAGENTA = "#1d1d1f"
LIME = "#248a3d"
OK = "#248a3d"
WARN = "#b25000"
BAD = "#c9342f"
ACC = NEON
FONT = ("Segoe UI", 10)
FONT_BIG = ("Segoe UI", 14, "bold")
FONT_HDR = ("Segoe UI", 10, "bold")
GLOBAL_NAV = "#000000"
STATUS_COLOR = {"UP": OK, "DOWN": BAD, "MISMATCH": WARN, "UNMAPPED": DIM, "DETECTED": NEON}
STATUS_GLYPH = {"UP": "●", "DOWN": "✖", "MISMATCH": "◈", "UNMAPPED": "○", "DETECTED": "◉"}


def _preview_text(previous: str | None, result: object) -> tuple[str, bool]:
    text = result if isinstance(result, str) else f"실패: {result}"
    failed = text.startswith("(미리보기 불가:") or text.startswith("실패:")
    if failed and previous:
        return previous + "\n\n[STALE — PREVIEW UNAVAILABLE]\n" + text, False
    return text, not failed


class Board:
    def __init__(self, ssh_target: str | None = None) -> None:
        import tkinter as tk

        if ssh_target:
            from actl.core.tmux import set_remote_ssh

            set_remote_ssh(ssh_target)
        self.ssh_target = ssh_target
        self.config = load_config()
        self.root = tk.Tk()
        self.root.title("JuActl — MainPC 에이전트 보드" + (f" (ssh {ssh_target})" if ssh_target else ""))
        self.root.geometry("1440x900")
        self.root.minsize(1100, 700)
        self.root.configure(bg=BG)
        self.selected: str | None = None
        self.rows: list[dict] = []
        self.jobs: queue.Queue = queue.Queue()
        self.auto_refresh = True
        self.refreshing = False
        self.hydrating = False
        self.preview_inflight: set[str] = set()
        self.last_previews: dict[str, str] = {}
        self.refresh_interval_ms = 12000
        self.event_refresh_scheduled = False
        self.last_event_refresh = 0.0
        self.pending_event_panes: set[str] = set()
        self.pending_topology_refresh = False
        self.board_opened = False
        self.motion_phase = 0
        self.motion_labels: dict[str, object] = {}
        self.previous_rows: dict[str, dict] = {}
        self.log_visible = False
        self._build()
        self.refresh()
        self.root.after(100, self._drain)
        self.root.after(180, self._motion_tick)
        if self.ssh_target:
            self.auto_var.set("◉ 이벤트 감시 ON (health 60s)")
            self.root.after(250, self._event_tick)
            self.root.after(60000, self._health_tick)
        else:
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
        style.configure("Title.TLabel", background=BG, foreground=TXT, font=FONT_HDR)
        style.configure("TButton", font=FONT, padding=4)
        style.configure("Primary.TButton", background=ACC, foreground="white")

    def _btn(self, parent, text: str, fn, primary: bool = False):
        import tkinter as tk

        bg = ACC if primary else PANEL
        fg = "white" if primary else ACC
        return tk.Button(parent, text=text, command=fn, bg=bg, fg=fg,
                         activebackground="#005bb5" if primary else PANEL2,
                         activeforeground="white" if primary else TXT,
                         relief="flat", borderwidth=0, padx=15 if primary else 12,
                         pady=8 if primary else 6, cursor="hand2", font=FONT)

    def _build(self) -> None:
        import tkinter as tk
        from tkinter import ttk

        self._style()
        top = tk.Frame(self.root, bg=GLOBAL_NAV)
        top.pack(fill="x")
        conn = f"SSH · {self.ssh_target}" if self.ssh_target else "LOCAL"
        tk.Label(top, text="JUACTL", bg=GLOBAL_NAV, fg="white",
                 font=("Segoe UI", 12, "bold")).pack(side="left", padx=(22, 8), pady=12)
        tk.Label(top, text=f"AGENT BOARD  ·  {conn}", bg=GLOBAL_NAV, fg="#a1a1a6",
                 font=("Segoe UI", 9)).pack(side="left", pady=12)
        tk.Button(top, text="업데이트", command=self.on_update,
                  bg=GLOBAL_NAV, fg="#a1a1a6", activebackground=GLOBAL_NAV,
                  activeforeground="white", relief="flat", cursor="hand2", font=FONT).pack(side="left", padx=8)
        self.auto_var = tk.StringVar(value="◉ 자동새로고침 ON (12s)")
        tk.Button(top, textvariable=self.auto_var, command=self.toggle_auto,
                  bg=GLOBAL_NAV, fg="#a1a1a6", activebackground=GLOBAL_NAV,
                  activeforeground="white", relief="flat", cursor="hand2", font=FONT).pack(side="left", padx=18)
        self.summary_var = tk.StringVar(value="Runtimes 0 · WORKING 0 · IDLE 0 · BLOCKED 0 · UNKNOWN 0")
        tk.Label(top, textvariable=self.summary_var, bg=GLOBAL_NAV, fg="#a1a1a6",
                 font=FONT_HDR).pack(side="left", padx=8)
        self.status_var = tk.StringVar(value="준비")
        tk.Label(top, textvariable=self.status_var, bg=GLOBAL_NAV, fg="#a1a1a6",
                 font=FONT_HDR).pack(side="right", padx=10)

        main = ttk.Frame(self.root, padding=(16, 14, 16, 16))
        main.pack(fill="both", expand=True)
        main.columnconfigure(0, weight=1)
        main.columnconfigure(1, weight=3)
        main.columnconfigure(2, weight=2)
        main.rowconfigure(0, weight=1)

        sidebar = tk.Frame(main, bg=PANEL, highlightbackground=LINE, highlightthickness=1)
        sidebar.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        tk.Label(sidebar, text="PROJECTS", bg=PANEL, fg=TXT, font=FONT_BIG).pack(anchor="w", padx=12, pady=(14, 8))
        self.project_filter: str | None = None
        self.project_buttons = tk.Frame(sidebar, bg=PANEL)
        self.project_buttons.pack(fill="x", padx=8)
        self.project_counts = tk.StringVar(value="프로젝트를 검색 중…")
        tk.Label(sidebar, textvariable=self.project_counts, bg=PANEL, fg=DIM,
                 font=("Segoe UI", 9), justify="left", anchor="w").pack(fill="x", padx=12, pady=10)
        tk.Label(sidebar, text="NEEDS ATTENTION", bg=PANEL, fg=TXT, font=FONT_HDR).pack(anchor="w", padx=12, pady=(12, 4))
        self.attention_frame = tk.Frame(sidebar, bg=PANEL)
        self.attention_frame.pack(fill="x", padx=8)

        center = tk.Frame(main, bg=BG)
        center.grid(row=0, column=1, sticky="nsew", padx=(0, 10))
        center.rowconfigure(2, weight=1)
        tk.Label(center, text="LIVE RUNTIME INSTANCES", bg=BG, fg=TXT, font=FONT_BIG).grid(row=0, column=0, sticky="w", pady=(0, 10))
        tools = tk.Frame(center, bg=BG)
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
        mode_menu = tk.OptionMenu(tools, self.filter_mode, "전체", "결과 도착", "작업중", "Prompt 대기", "연결됨",
                                  command=lambda _v: self._render_cards(self.selected))
        mode_menu.configure(bg=PANEL, fg=TXT, activebackground=NEON, activeforeground="white",
                            relief="flat", highlightthickness=0)
        mode_menu["menu"].configure(bg=PANEL, fg=TXT, activebackground=NEON, activeforeground="white")
        mode_menu.pack(side="right")
        self.cards: dict[str, tk.Frame] = {}
        self.agent_cards = tk.Frame(center, bg=BG)
        self.agent_cards.grid(row=2, column=0, sticky="nsew")

        right = tk.Frame(main, bg=PANEL, highlightbackground=LINE, highlightthickness=1)
        right.grid(row=0, column=2, sticky="nsew")
        right.rowconfigure(2, weight=1)
        self.pane_title = tk.StringVar(value="RUNTIME INSPECTOR — select a runtime")
        tk.Label(right, textvariable=self.pane_title, bg=PANEL, fg=TXT, font=FONT_BIG,
                 wraplength=330, justify="left").grid(row=0, column=0, sticky="w", padx=14, pady=(14, 4))
        self.detail_var = tk.StringVar(value="Machine · Project · Agent · Role · State · Result")
        tk.Label(right, textvariable=self.detail_var, bg=PANEL, fg=DIM, font=("Segoe UI", 9),
                 anchor="w", justify="left", wraplength=330).grid(row=1, column=0, sticky="ew", padx=14, pady=(0, 8))
        self.preview = tk.Text(right, wrap="none", font=("Cascadia Mono", 10), bg=GLOBAL_NAV, fg="#f5f5f7",
                               insertbackground=NEON, highlightthickness=0, borderwidth=0)
        self.preview.grid(row=2, column=0, sticky="nsew", padx=14)
        self.resp = self.preview
        cmdbar = tk.Frame(right, bg=PANEL)
        cmdbar.grid(row=3, column=0, sticky="ew", pady=8, padx=14)
        self.action_buttons = {}
        for label, fn, primary in [("SEND PROMPT", self.on_send, True), ("COPY RESULT", self.on_copy, False),
                                   ("COLLECT RESULT", self.on_collect, False), ("FOCUS", self.on_focus, False)]:
            button = self._btn(cmdbar, label, fn, primary=primary)
            button.pack(side="left", padx=2)
            self.action_buttons[label] = button
        self.action_buttons["COLLECT RESULT"].configure(state="disabled")
        from tkinter import scrolledtext

        tk.Label(right, text="Prompt · Ctrl+Enter", bg=PANEL, fg=TXT, font=FONT_HDR).grid(row=4, column=0, sticky="w", padx=14, pady=(4, 3))
        self.msg = scrolledtext.ScrolledText(right, height=2, font=FONT, bg="#fafafa", fg=TXT,
                                             insertbackground=NEON, highlightthickness=0, borderwidth=0)
        self.msg.grid(row=5, column=0, sticky="ew", padx=14, pady=2)
        sendrow = tk.Frame(right, bg=PANEL)
        sendrow.grid(row=6, column=0, sticky="ew", pady=2, padx=14)
        self.send_btn = tk.Button(sendrow, text="SEND PROMPT", command=self.on_send, bg=ACC, fg="white",
                                  relief="flat", padx=12, pady=6)
        self.send_btn.pack(side="left")
        self.log_toggle = tk.Button(sendrow, text="▸ diagnostics", command=self.toggle_log,
                                    bg=PANEL, fg=DIM, activebackground=PANEL2, relief="flat", cursor="hand2", font=FONT)
        self.log_toggle.pack(side="left", padx=6)
        self.logw = scrolledtext.ScrolledText(right, height=6, state="disabled", font=("Cascadia Mono", 9),
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
            self.logw.grid(row=7, column=0, sticky="nsew", pady=2)
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
                   ("작업중만 보기", lambda: (self.filter_mode.set("작업중"), self._render_cards(self.selected))),
                   ("결과 도착만 보기", lambda: (self.filter_mode.set("결과 도착"), self._render_cards(self.selected))),
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
        if self.ssh_target:
            self.auto_var.set("◉ 이벤트 감시 ON (health 60s)" if self.auto_refresh else "◌ 이벤트 감시 OFF")
        else:
            self.auto_var.set(f"◉ 자동새로고침 ON ({self.refresh_interval_ms // 1000}s)" if self.auto_refresh else "◌ 자동새로고침 OFF")
        self.log(f"{'이벤트 감시' if self.ssh_target else '자동새로고침'} {'켬' if self.auto_refresh else '끔'}")

    def on_update(self) -> None:
        from tkinter import messagebox

        self.set_status("업데이트 확인 중…")
        self._bg(self._check_update, lambda result: self._update_done(result, messagebox))

    @staticmethod
    def _check_update():
        from actl.core import updater

        release = updater.check_latest()
        if updater._version(release["version"]) <= updater._version(updater.CURRENT_VERSION):
            return None
        return release

    def _update_done(self, result, messagebox) -> None:
        if isinstance(result, Exception):
            self.set_status("업데이트 확인 실패")
            self.log(f"업데이트 확인 실패: {result}")
            return
        if result is None:
            self.set_status("최신 버전")
            self.log("현재 최신 버전입니다")
            return
        from actl.core import updater

        if not messagebox.askyesno("JuActl 업데이트", f"새 버전 {result['version']}을 설치할까요?", parent=self.root):
            self.set_status("준비")
            return
        self.set_status("업데이트 다운로드 중…")

        def work():
            installer = updater.download_verified(result)
            updater.launch_installer(installer)
            return installer

        def done(downloaded) -> None:
            if isinstance(downloaded, Exception):
                self.set_status("업데이트 실패")
                self.log(f"업데이트 실패: {downloaded}")
                return
            self.log("업데이트 설치를 시작했습니다. 프로그램을 종료합니다.")
            self.root.after(200, self.root.destroy)

        self._bg(work, done)

    def _auto_tick(self) -> None:
        if self.auto_refresh:
            self.refresh(quiet=True)
        self.root.after(self.refresh_interval_ms, self._auto_tick)

    def _motion_tick(self) -> None:
        """Animate only visible RUNNING cards; no remote work is performed."""
        running = any(
            row.get("activity_state") == "RUNNING"
            and row.get("target") not in {"-", ""}
            and not row.get("target", "").endswith("?")
            for row in self.rows
        )
        if running:
            self.motion_phase = (self.motion_phase + 1) % 4
            frame = ("◐", "◓", "◑", "◒")[self.motion_phase]
            for agent, label in list(self.motion_labels.items()):
                row = next((item for item in self.rows if item["agent"] == agent), None)
                if row and row.get("activity_state") == "RUNNING":
                    label.configure(text=f"RUNNING {frame} · 작업중")
        self.root.after(180, self._motion_tick)

    @staticmethod
    def _phase(row: dict) -> str:
        if row.get("result_state") == "READY":
            return "결과 도착"
        if row.get("activity_state") == "RUNNING":
            return "작업중"
        if row.get("activity_state") == "IDLE":
            return "Prompt 대기"
        if row.get("state") == "UP":
            return "연결됨"
        return "상태 확인 필요"

    def _event_tick(self) -> None:
        from actl.core.tmux import remote_events

        events = remote_events()
        topology = any(
            event.startswith(("%sessions-changed", "%window-", "%layout-change", "%session-"))
            for event in events
        )
        state_candidate = any(
            event.startswith(("%output", "%pane-mode-changed", "%pause", "%continue"))
            for event in events
        )
        for event in events:
            parts = event.split(maxsplit=2)
            if state_candidate and len(parts) > 1 and parts[1].startswith("%"):
                self.pending_event_panes.add(parts[1])
        now = time.monotonic()
        # tmux emits %output for every streamed character burst. Treat it as
        # a state candidate, not as permission to run a full remote refresh.
        # topology changes remain immediate; output candidates are throttled.
        eligible = topology or (state_candidate and now - self.last_event_refresh >= 1.0)
        if self.auto_refresh and eligible and not self.event_refresh_scheduled:
            self.event_refresh_scheduled = True
            self.pending_topology_refresh = self.pending_topology_refresh or topology
            self.root.after(120 if topology else 450, self._event_refresh)
        if self.ssh_target:
            self.root.after(250, self._event_tick)

    def _event_refresh(self) -> None:
        self.event_refresh_scheduled = False
        if not self.auto_refresh:
            self.pending_event_panes.clear()
            self.pending_topology_refresh = False
            return
        self.last_event_refresh = time.monotonic()
        pane_ids = self.pending_event_panes
        full = self.pending_topology_refresh
        self.pending_event_panes = set()
        self.pending_topology_refresh = False
        if full:
            self.refresh(quiet=True)
        elif pane_ids:
            self._refresh_event_panes(pane_ids)

    def _refresh_event_panes(self, pane_ids: set[str]) -> None:
        """Refresh only the selected pane after a pane-local tmux event."""
        row = self.current()
        if not row or row.get("target") not in pane_ids:
            return
        target = row["target"]
        self._request_preview(row)

    def _request_preview(self, row: dict) -> None:
        key = row["runtime_key"]
        if key in self.preview_inflight:
            return
        self.preview_inflight.add(key)
        target = row["target"]

        def work():
            return _pane_preview(target, lines=12)

        def done(result) -> None:
            self.preview_inflight.discard(key)
            if self.selected != key:
                return
            text, fresh = _preview_text(self.last_previews.get(key), result)
            if fresh:
                self.last_previews[key] = text
            self.preview.delete("1.0", "end")
            self.preview.insert("end", "LIVE PANE PREVIEW\n" + text)
            self.pane_title.set(f"{row['display']} · {row.get('pane_id', target)} — LIVE PANE")
            self.set_status("준비")

        self._bg(work, done)

    def _health_tick(self) -> None:
        if self.auto_refresh:
            self.refresh(quiet=True)
        if self.ssh_target:
            self.root.after(60000, self._health_tick)

    def rows_now(self) -> list[dict]:
        from actl.core.discovery import discover, reconcile
        from actl.tui import _rows

        detections = discover()
        updated, changes = reconcile(self.config, detections, unique_only=True)
        if changes:
            backup_config()
            save_config(updated)
            self.config = updated
        return {"rows": _rows(self.config, detections, hydrate=False), "detections": detections}

    def _start_hydration(self) -> None:
        if self.hydrating or not getattr(self, "_snapshot_detections", None):
            return
        self.hydrating = True
        from actl.tui import _rows

        detections = self._snapshot_detections
        config = self.config
        self._bg(lambda: _rows(config, detections, hydrate=True), self._hydration_done)

    def _hydration_done(self, result) -> None:
        self.hydrating = False
        if isinstance(result, Exception):
            self.log(f"상세 hydration 실패 — 기존 inventory 유지: {result}")
            return
        self.rows = result
        self._render_projects()
        self._render_cards(self.selected)
        self._update_action_state()
        if self.selected:
            self.on_select()
        self.log("runtime detail hydration 완료")

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
        from tkinter import ttk

        if isinstance(result, Exception):
            self.refreshing = False
            self.set_status("새로고침 실패")
            self.log(f"새로고침 실패: {result}")
            return
        prev_sel = self.selected
        from actl.core.events import detect_events

        payload = result if isinstance(result, dict) else {"rows": result, "detections": []}
        self.rows = payload["rows"]
        self._snapshot_detections = payload["detections"]
        if self.previous_rows:
            for event in detect_events(self.previous_rows, self.rows):
                self.log(f"◆ {event['agent']} · {event['detail']}")
        self.previous_rows = {r["runtime_key"]: r for r in self.rows}
        self.refresh_interval_ms = 3000 if any(r.get("activity_state") == "RUNNING" for r in self.rows) else 12000
        if self.ssh_target:
            self.auto_var.set("◉ 이벤트 감시 ON (health 60s)" if self.auto_refresh else "◌ 이벤트 감시 OFF")
        else:
            self.auto_var.set(f"◉ 자동새로고침 ON ({self.refresh_interval_ms // 1000}s)" if self.auto_refresh else "◌ 자동새로고침 OFF")
        self._render_cards(prev_sel)
        from actl.core.projection import runtime_counts

        counts = runtime_counts(self.rows)
        self.summary_var.set(" · ".join([f"Runtimes {len(self.rows)}"] +
                                        [f"{key} {value}" for key, value in counts.items()]))
        self.set_status(f"ASUS ● CONNECTED · {len(self.rows)} runtimes" if self.ssh_target else f"{len(self.rows)} runtimes")
        self._render_projects()
        if not quiet:
            self.log(f"새로고침 완료 ({len(self.rows)} runtime instances)")
        if self.rows:
            keep = prev_sel if any(r["runtime_key"] == prev_sel for r in self.rows) else self.rows[0]["runtime_key"]
            self.selected = keep
            self._highlight(keep)
            self._update_action_state()
            self.on_select()
        self._start_hydration()
        if self.ssh_target and not self.board_opened:
            self.board_opened = True
            self.root.after(80, self.on_board)
        self.refreshing = False

    def _render_projects(self) -> None:
        import tkinter as tk
        from actl.core.projection import attention_rows, project_groups

        for child in self.project_buttons.winfo_children():
            child.destroy()
        counts: dict[str, tuple[int, int, int]] = {}
        for project, project_rows in project_groups(self.rows).items():
            for row in project_rows:
                total, working, attention = counts.get(project, (0, 0, 0))
                counts[project] = (total + 1, working + (row.get("runtime_state") == "WORKING"),
                                   attention + (row.get("runtime_state") in {"BLOCKED", "UNKNOWN"} or
                                               row.get("state") in {"DOWN", "MISMATCH", "DETECTED"}))
        names = sorted(counts, key=lambda name: (name == "UNKNOWN", name))
        names = (["ALL PROJECTS"] if names else []) + names
        for name in names:
            label = name
            if name != "ALL PROJECTS":
                total, working, attention = counts[name]
                label = f"{name}\n  {total} runtime · {working} working · {attention} attention"
            button = tk.Button(self.project_buttons, text=label, anchor="w", justify="left",
                               bg=ACC if ((name == "ALL PROJECTS" and self.project_filter is None) or
                                          name == self.project_filter) else PANEL,
                               fg="white" if ((name == "ALL PROJECTS" and self.project_filter is None) or
                                               name == self.project_filter) else TXT,
                               relief="flat", padx=8, pady=6,
                               command=lambda value=name: self.select_project(value))
            button.pack(fill="x", pady=2)
        self.project_counts.set(f"{len(self.rows)} runtime instances · {len(counts)} projects")
        for child in self.attention_frame.winfo_children():
            child.destroy()
        attention = attention_rows(self.rows)
        for row in attention[:8]:
            label = f"{row.get('project', 'UNASSIGNED')} · {row['display']} · {row.get('role', 'UNKNOWN')} · {row.get('control_reason') or row.get('runtime_state', row.get('state'))}\n  Reason: {row.get('control_detail', 'Founder action may be required')}"
            tk.Button(self.attention_frame, text=label, anchor="w", justify="left", bg="#fff7ed", fg=WARN,
                      relief="flat", padx=6, pady=4,
                      command=lambda key=row["runtime_key"]: self.select_agent(key)).pack(fill="x", pady=1)
        if not attention:
            tk.Label(self.attention_frame, text="없음", bg=PANEL, fg=DIM, font=("Segoe UI", 9)).pack(anchor="w", padx=6)

    def select_project(self, project: str) -> None:
        self.project_filter = None if project == "ALL PROJECTS" else project
        self._render_projects()
        self._render_cards(self.selected)

    def _render_cards(self, selected: str | None = None) -> None:
        import tkinter as tk
        from actl.core.projection import filter_project

        for child in self.agent_cards.winfo_children():
            child.destroy()
        self.cards = {}
        self.motion_labels = {}
        query = self.filter_var.get().strip().lower()
        if query == "검색…":
            query = ""
        mode = self.filter_mode.get()
        visible = []
        for r in filter_project(self.rows, self.project_filter):
            haystack = f"{r['display']} {r['agent']} {r.get('project')} {r.get('role')} {r.get('runtime_state')}".lower()
            matches_mode = mode == "전체" or self._phase(r) == mode
            if matches_mode and (not query or query in haystack):
                visible.append(r)
        visible.sort(key=lambda r: (
            0 if r.get("result_state") == "READY" else 1,
            0 if r.get("activity_state") == "RUNNING" else 1,
            0 if r.get("activity_state") == "IDLE" else 1,
            0 if r.get("unread") else 1,
            r.get("project", "UNKNOWN"), r.get("role", "UNKNOWN"), r.get("runtime_identity", ""),
        ))
        for r in visible:
            state = STATE_KO.get(r["state"], r["state"])
            glyph = STATUS_GLYPH.get(r["state"], "·")
            color = STATUS_COLOR.get(r["state"], TXT)
            card = tk.Frame(self.agent_cards, bg=PANEL, highlightbackground=ACC if r["runtime_key"] == selected else LINE,
                            highlightthickness=2 if r["runtime_key"] == selected else 1,
                            cursor="hand2")
            card.pack(fill="x", pady=4)
            top = tk.Frame(card, bg=PANEL)
            top.pack(fill="x", padx=8, pady=(6, 0))
            tk.Label(top, text=f"{glyph} {r['display']}  ·  {r.get('role', 'UNKNOWN')}", bg=PANEL, fg=color,
                     font=("Segoe UI", 11, "bold")).pack(side="left")
            tk.Label(top, text=r.get("runtime_state", "UNKNOWN"), bg=PANEL, fg=color, font=FONT).pack(side="right")
            pane_tail = (r.get("pane_preview") or r["preview"] or r["detail"] or "—").replace("\n", " ")
            sub = (f"{r.get('project', 'UNKNOWN')} · {r.get('model_profile', 'UNKNOWN')} · "
                   f"Pane {r.get('pane_id', 'UNKNOWN')} {r.get('pane_target', '')} · "
                   f"{r.get('runtime_state', 'UNKNOWN')} · "
                   f"{r.get('current_task', 'UNKNOWN')} · {pane_tail}")[:180]
            phase = self._phase(r)
            activity = phase
            if r.get("activity_state") == "RUNNING":
                activity = "RUNNING ◐ · 작업중"
            phase_label = tk.Label(card, text=f"{state} · {activity} · {sub}", bg=PANEL, fg=DIM,
                                   font=("Segoe UI", 9), anchor="w", justify="left")
            phase_label.pack(fill="x", padx=8, pady=(0, 6))
            self.motion_labels[r["runtime_key"]] = phase_label
            card.bind("<Button-1>", lambda _e, a=r["runtime_key"]: self.select_agent(a))
            for child in (card, top):
                child.bind("<Button-1>", lambda _e, a=r["runtime_key"]: self.select_agent(a))
            for w in top.winfo_children():
                w.bind("<Button-1>", lambda _e, a=r["runtime_key"]: self.select_agent(a))
            self.cards[r["runtime_key"]] = card
        if not visible:
            tk.Label(self.agent_cards, text="조건에 맞는 에이전트 없음", bg=BG, fg=DIM,
                     font=FONT).pack(anchor="w", padx=8, pady=8)

    def _highlight(self, agent: str) -> None:
        import tkinter as tk

        for name, card in self.cards.items():
            row = next((r for r in self.rows if r["runtime_key"] == name), None)
            color = STATUS_COLOR.get(row["state"], TXT) if row else LINE
            card.configure(highlightbackground=color,
                           highlightthickness=2 if name == agent else 0)

    def _update_action_state(self) -> None:
        row = self.current()
        enabled = bool(row and row.get("control_ready"))
        self.send_btn.configure(state="normal" if enabled else "disabled")
        for label in ("SEND PROMPT", "COPY RESULT", "FOCUS"):
            self.action_buttons[label].configure(state="normal" if enabled else "disabled")

    def select_agent(self, agent: str) -> None:
        self.selected = agent
        self._highlight(agent)
        self._update_action_state()
        self.on_select()

    def current(self) -> dict | None:
        if self.selected:
            row = next((r for r in self.rows if r["runtime_key"] == self.selected), None)
            if row:
                return row
        return self.rows[0] if self.rows else None

    def on_select(self) -> None:
        row = self.current()
        if not row:
            return
        self.selected = row["runtime_key"]
        tgt = row["target"]
        result_label = {"READY": "준비됨", "WAITING": "대기", "UNKNOWN": "미확인"}.get(row.get("result_state"), "미확인")
        self.detail_var.set(
            f"Machine {row.get('machine', 'UNKNOWN')} · Project {row.get('project', 'UNKNOWN')}\n"
            f"Agent {row.get('display', row.get('agent'))} · Role {row.get('role', 'UNKNOWN')}\n"
            f"Model/Profile {row.get('model_profile', 'UNKNOWN')} · State {row.get('runtime_state', 'UNKNOWN')}\n"
            f"Pane {row.get('pane_id', 'UNKNOWN')} · Session {row.get('session', 'UNKNOWN')} "
            f"Window {row.get('window', 'UNKNOWN')} Pane {row.get('pane_index', 'UNKNOWN')}\n"
            f"Command {row.get('pane_command', 'UNKNOWN')} · PID {row.get('pane_pid', 'UNKNOWN')}\n"
            f"Task {row.get('current_task', 'UNKNOWN')} · Result {result_label} · Health {row.get('state', 'UNKNOWN')}\n"
            f"Controls {row.get('control_reason', 'UNKNOWN')}: {row.get('control_detail', '')}"
        )
        if tgt == "-":
            self.preview.delete("1.0", "end")
            self.preview.insert("end", f"{row['display']}: live runtime 없음")
            self.pane_title.set(f"{row['display']} — no live runtime")
            return
        self.set_status(f"{row['display']} 로딩…")
        self.log(f"{row['display']} 미리보기 로딩…")

        def work():
            return _verify_row(row["agent"], tgt, self.config) if row.get("control_ready") else "읽기 전용 발견 runtime — 매핑 전 제어 비활성"
        if row.get("control_ready"):
            self._bg(work, lambda result: self.log(result) if isinstance(result, str) else self.log(f"preview diagnostic failed: {result}"))
        self._request_preview(row)

    def on_focus(self) -> None:
        row = self.current()
        if not row or not row.get("control_ready"):
            self.log("FOCUS disabled: validated tmux mapping required")
            return
        from actl.core.tmux import _run

        try:
            _run(["tmux", "select-pane", "-t", row["target"]])
            self.log(f"{row['display']} pane focused")
        except Exception as exc:
            self.log(f"FOCUS failed: {exc}")

    def on_collect(self) -> None:
        row = self.current()
        if not row:
            return
        self.log("COLLECT RESULT disabled: no managed Task/Run command is bound to this runtime")

    def on_copy(self) -> None:
        row = self.current()
        if not row or not row.get("control_ready"):
            self.log("COPY RESULT disabled: validated mapping required")
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
                import sys

                preferred = self.config.get("clipboard_backend", "auto")
                # This GUI owns the MainPC clipboard. Do not send OSC52 back
                # into the remote tmux when running the Windows remote board.
                if sys.platform == "win32" and self.ssh_target:
                    preferred = "local"
                backend = copy_text(result.text, preferred=preferred)
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
        if not row or not row.get("control_ready"):
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
        from tkinter import messagebox

        row = self.current()
        if not row:
            return
        dets = _unmapped_panes(self.config, row["agent"])
        if not dets:
            detail = (
                f"{row['display']}의 안전한 후보 pane을 찾지 못했습니다.\n\n"
                "전체 pane 보드에서 실제 TUI를 확인하세요.\n"
                "OpenCode의 `opencode serve`/ACP pane은 서버라서 결과 복사·입력 대상에서 제외됩니다."
            )
            self.log(f"{row['display']} 매핑 후보 없음 (serve/ACP는 제외)")
            messagebox.showinfo("안전한 매핑 후보 없음", detail, parent=self.root)
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

    def on_auto_map(self) -> None:
        """Apply only unambiguous, high-confidence live detections."""
        self.set_status("확실한 매핑 확인 중…")
        self.log("확실한 매핑 확인 중… (불확실한 pane은 유지)")

        def work():
            return reconcile(self.config, discover())

        def done(result) -> None:
            if isinstance(result, Exception):
                self.set_status("자동 매핑 실패")
                self.log(f"자동 매핑 실패: {result}")
                return
            updated, changes = result
            if changes:
                backup = backup_config()
                save_config(updated)
                self.config = updated
                self.log(f"자동 매핑 적용: {', '.join(changes)} (백업 {backup.name})")
            else:
                self.log("자동 매핑 변경 없음 (확실한 후보만 적용)")
            self.refresh()

        self._bg(work, done)

    def _do_remap(self, row: dict, det) -> bool:
        try:
            updated = manual_map(self.config, row["agent"], det)
        except ValueError as exc:
            self.log(f"매핑 실패: {exc}")
            return False
        backup = backup_config()
        save_config(updated)
        from actl.core.audit import record

        record("map", agent=row["agent"], target=det.pane.pane_id, ok=True)
        self.config = updated
        self.log(f"{row['display']} → {det.pane.pane_id} 매핑됨 (백업 {backup.name})")
        self.refresh()
        return True

    def on_board(self) -> None:
        import tkinter as tk
        from tkinter import messagebox, simpledialog, ttk
        from actl.core import tmux

        self.set_status("pane 보드 로딩…")
        top = tk.Toplevel(self.root)
        top.title("JuActl — 전체 tmux pane 선택")
        top.configure(bg=BG)
        top.geometry("1080x680")
        tk.Label(
            top,
            text="tmux topology · session → window → pane",
            bg=BG, fg=TXT, font=FONT_BIG,
        ).pack(anchor="w", padx=12, pady=10)
        status = tk.Label(top, text="전체 pane 검색 중…", bg=BG, fg=DIM, anchor="w")
        status.pack(fill="x", padx=12)
        body = tk.Frame(top, bg=BG)
        body.pack(fill="both", expand=True, padx=12, pady=8)
        tree = ttk.Treeview(body, columns=("kind", "runtime", "path", "agent", "mapped"), show="tree headings")
        tree.heading("#0", text="tmux 이름 / pane")
        tree.heading("kind", text="유형")
        tree.heading("runtime", text="실행 프로세스")
        tree.heading("path", text="작업 경로")
        tree.heading("agent", text="감지 Agent")
        tree.heading("mapped", text="현재 매핑")
        tree.column("#0", width=270, anchor="w")
        tree.column("kind", width=90, anchor="w")
        tree.column("runtime", width=150, anchor="w")
        tree.column("path", width=390, anchor="w")
        tree.column("agent", width=150, anchor="w")
        tree.column("mapped", width=150, anchor="w")
        scroll = ttk.Scrollbar(body, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        pane_by_item: dict[str, tuple[object, Detection]] = {}
        node_targets: dict[str, str] = {}
        drag_state: dict[str, str | None] = {"source": None}

        def work():
            return _all_panes()

        def done(result) -> None:
            if isinstance(result, Exception):
                status.configure(text=f"검색 실패: {result}")
                self.set_status("pane 보드 실패")
                return
            for item in tree.get_children():
                tree.delete(item)
            pane_by_item.clear()
            node_targets.clear()
            groups: dict[str, dict[str, list]] = {}
            pane_ids = {pane.pane_id for pane, _ in result}
            mapped_by_pane = {
                item.get("target"): AGENTS[name].display_name
                for name, item in self.config.get("agents", {}).items()
                if item.get("target") in pane_ids and name in AGENTS
            }
            for pane, det in result:
                window = pane.target.rsplit(".", 1)[0]
                session = window.split(":", 1)[0]
                groups.setdefault(session, {}).setdefault(window, []).append((pane, det))
            if not groups:
                status.configure(text="live pane 없음")
                self.set_status("준비")
                return
            status.configure(text=f"{len(result)}개 pane · session/window/pane을 선택하세요")
            for session, windows in groups.items():
                sid = tree.insert("", "end", text=session, values=("session", "", "", "", ""), open=True)
                node_targets[sid] = session
                for window, entries in windows.items():
                    wid = tree.insert(sid, "end", text=window.split(":", 1)[1],
                                      values=("window", "", "", "", ""), open=True)
                    node_targets[wid] = window
                    for pane, det in entries:
                        detected = AGENTS[det.agent].display_name if det.agent in AGENTS else "미감지"
                        mapped = mapped_by_pane.get(pane.pane_id, "미매핑")
                        pid = tree.insert(
                            wid, "end", text=f"{pane.pane_id}  {pane.title or '(untitled)'}",
                            values=("pane", pane.current_command, pane.current_path, detected, mapped),
                        )
                        pane_by_item[pid] = (pane, det)
                        node_targets[pid] = pane.target
            self.set_status("준비")

        def selected_target() -> tuple[str, str] | None:
            selection = tree.selection()
            if not selection:
                messagebox.showinfo("선택 필요", "이름을 바꾸거나 매핑할 tmux 노드를 선택하세요", parent=top)
                return None
            item = selection[0]
            return item, tree.set(item, "kind")

        def map_selected() -> None:
            picked = selected_target()
            if not picked:
                return
            item, kind = picked
            if kind != "pane" or item not in pane_by_item:
                messagebox.showinfo("pane 선택 필요", "Agent를 지정할 pane을 선택하세요", parent=top)
                return
            pane, det = pane_by_item[item]
            self._choose_manual_agent(pane, det, top)

        def rename_selected() -> None:
            picked = selected_target()
            if not picked:
                return
            item, kind = picked
            if kind == "pane":
                pane, _ = pane_by_item[item]
                target, initial, rename = node_targets[item], pane.title, tmux.rename_pane
                prompt = f"{pane.pane_id} pane 이름"
            elif kind == "window":
                target = node_targets[item]
                initial, rename, prompt = tree.item(item, "text"), tmux.rename_window, "window 이름"
            else:
                target = node_targets[item]
                initial, rename, prompt = target, tmux.rename_session, "session 이름"
            name = simpledialog.askstring("tmux 이름 변경", prompt, initialvalue=initial, parent=top)
            if name is None:
                return
            status.configure(text=f"{target} 이름 변경 중…")
            self.set_status("이름 변경 중…")

            def done(result) -> None:
                if isinstance(result, Exception):
                    messagebox.showerror("이름 변경 실패", str(result), parent=top)
                    self.set_status("이름 변경 실패")
                    return
                top.destroy()
                self.board_opened = False
                self.refresh()

            self._bg(lambda: rename(target, name), done)

        def create_session() -> None:
            name = simpledialog.askstring("새 session", "session 이름", parent=top)
            if not name:
                return
            try:
                tmux.create_session(name)
            except Exception as exc:
                messagebox.showerror("session 생성 실패", str(exc), parent=top)
                return
            top.destroy()
            self.on_board()

        def create_window() -> None:
            picked = selected_target()
            if not picked:
                return
            item, kind = picked
            if kind == "session":
                session = tree.item(item, "text")
            elif kind == "window":
                session = tree.item(tree.parent(item), "text")
            elif kind == "pane":
                pane, _ = pane_by_item[item]
                session = pane.target.rsplit(".", 1)[0]
            else:
                return
            name = simpledialog.askstring("새 window", f"{session} 안의 window 이름", parent=top)
            if not name:
                return
            try:
                tmux.create_window(session, name)
            except Exception as exc:
                messagebox.showerror("window 생성 실패", str(exc), parent=top)
                return
            top.destroy()
            self.on_board()

        def split_selected() -> None:
            picked = selected_target()
            if not picked:
                return
            item, kind = picked
            if kind != "pane" or item not in pane_by_item:
                messagebox.showinfo("pane 선택 필요", "분할할 pane을 선택하세요", parent=top)
                return
            pane, _ = pane_by_item[item]
            try:
                tmux.split_pane(pane.pane_id)
            except Exception as exc:
                messagebox.showerror("pane 생성 실패", str(exc), parent=top)
                return
            top.destroy()
            self.on_board()

        def window_target(item: str) -> str | None:
            kind = tree.set(item, "kind")
            if kind == "window":
                return node_targets.get(item)
            if kind == "session":
                children = tree.get_children(item)
                return window_target(children[0]) if children else None
            if kind == "pane" and item in pane_by_item:
                pane, _ = pane_by_item[item]
                return node_targets.get(item, pane.target).rsplit(".", 1)[0]
            return None

        def drag_start(event) -> None:
            item = tree.identify_row(event.y)
            drag_state["source"] = item if item in pane_by_item else None

        def drag_drop(event) -> None:
            source_item = drag_state.get("source")
            drag_state["source"] = None
            if not source_item or source_item not in pane_by_item:
                return
            destination_item = tree.identify_row(event.y)
            destination = window_target(destination_item) if destination_item else None
            if not destination:
                return
            pane, _ = pane_by_item[source_item]
            current_window = pane.target.rsplit(".", 1)[0]
            if destination == current_window:
                return
            if not messagebox.askyesno(
                "pane 이동 확인",
                f"{pane.pane_id}를 {destination} 윈도우로 이동할까요?\n\n"
                "작업 중인 Agent는 입력 상태가 바뀔 수 있습니다.",
                parent=top,
            ):
                return
            try:
                status.configure(text=f"{pane.pane_id} → {destination} 이동 중…")
                self.set_status("pane 이동 중…")
            except Exception:
                return

            def done(result) -> None:
                if isinstance(result, Exception):
                    messagebox.showerror("pane 이동 실패", str(result), parent=top)
                    self.set_status("pane 이동 실패")
                    return
                self.log(f"{pane.pane_id} → {destination} 이동됨")
                top.destroy()
                self.board_opened = False
                self.refresh()

            self._bg(lambda: tmux.move_pane(pane.pane_id, destination), done)

        def drag_motion(event) -> None:
            source_item = drag_state.get("source")
            if not source_item:
                return
            item = tree.identify_row(event.y)
            if item:
                tree.selection_set(item)
                kind = tree.set(item, "kind")
                status.configure(text="window에 drop하면 pane 이동 확인창이 표시됩니다" if kind in {"window", "session"} else "pane 이동 대상 window를 선택하세요")

        actions = tk.Frame(top, bg=BG)
        actions.pack(fill="x", padx=12, pady=(0, 12))
        self._btn(actions, "Agent 지정", map_selected, primary=True).pack(side="left")
        self._btn(actions, "이름 변경", rename_selected).pack(side="left", padx=8)
        self._btn(actions, "새 session", create_session).pack(side="left", padx=8)
        self._btn(actions, "새 window", create_window).pack(side="left")
        self._btn(actions, "pane 분할", split_selected).pack(side="left", padx=8)
        self._btn(actions, "새로고침", lambda: (top.destroy(), self.on_board())).pack(side="left")
        tree.bind("<ButtonPress-1>", drag_start, add="+")
        tree.bind("<B1-Motion>", drag_motion, add="+")
        tree.bind("<ButtonRelease-1>", drag_drop, add="+")
        tree.bind("<Double-1>", lambda _e: map_selected() if tree.selection() and tree.set(tree.selection()[0], "kind") == "pane" else rename_selected())
        self._bg(work, done)

    def _choose_manual_agent(self, pane, detected: Detection, board) -> None:
        """Map one explicitly selected tmux pane after validating its process."""
        import tkinter as tk
        from tkinter import ttk

        dialog = tk.Toplevel(board)
        dialog.title(f"{pane.pane_id} Agent 지정")
        dialog.configure(bg=BG)
        dialog.transient(board)
        tk.Label(dialog, text=f"{pane.target} · {pane.current_command}", bg=BG, fg=TXT,
                 font=FONT_HDR).pack(anchor="w", padx=16, pady=(16, 8))
        tk.Label(dialog, text="매핑할 Agent", bg=BG, fg=DIM, font=FONT).pack(anchor="w", padx=16)
        choice = tk.StringVar(value=AGENTS[detected.agent].display_name if detected.agent in AGENTS else next(iter(AGENTS.values())).display_name)
        names = [spec.display_name for spec in AGENTS.values()]
        menu = ttk.Combobox(dialog, textvariable=choice, values=names, state="readonly", width=28)
        menu.pack(fill="x", padx=16, pady=8)

        def apply() -> None:
            agent = next((name for name, spec in AGENTS.items() if spec.display_name == choice.get()), None)
            if not agent:
                return
            det = Detection(pane, agent, "manual", "user-selected tmux pane")
            if not self._do_remap({"agent": agent, "display": AGENTS[agent].display_name}, det):
                return
            dialog.destroy()
            board.destroy()

        tk.Button(dialog, text="검증 후 매핑", command=apply, bg=ACC, fg="white",
                  relief="flat", padx=16, pady=8).pack(anchor="e", padx=16, pady=(4, 16))
        dialog.bind("<Return>", lambda _e: apply())
        dialog.bind("<Escape>", lambda _e: dialog.destroy())
        dialog.grab_set()
        menu.focus_set()

    def on_send(self) -> None:
        from tkinter import messagebox

        row = self.current()
        if not row or not row.get("control_ready"):
            self.log("SEND PROMPT disabled: validated mapping required")
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
                self.preview.delete("1.0", "end")
                self.preview.insert("end", f"✓ {row['display']}에 메시지를 전송했습니다.\n\n작업 결과를 기다리는 중…")
            elif isinstance(result, Exception):
                detail = str(result) or type(result).__name__
                self.set_status("전송 실패")
                self.preview.delete("1.0", "end")
                self.preview.insert(
                    "end",
                    f"✗ {row['display']} 전송 실패\n\n{detail}\n\n"
                    "매핑 상태와 pane 작업 상태를 확인하세요.\n"
                    "BUSY라면 현재 다른 작업이 pane을 점유 중입니다.",
                )
                self.log(f"{row['display']} 전송 실패: {detail}")
            else:
                self.log(f"전송 실패: {result}")

        self._bg(work, done)

    def run(self) -> int:
        self.root.mainloop()
        return 0


def run_gui(ssh_target: str | None = None) -> int:
    return Board(ssh_target=ssh_target).run()
