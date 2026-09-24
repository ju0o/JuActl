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
STATUS_COLOR = {
    "UP": OK, "WORKING": OK, "IDLE": OK, "TRANSPORT_BUSY": WARN,
    "DEGRADED": WARN, "DOWN": BAD, "MISMATCH": WARN, "UNMAPPED": DIM,
    "DETECTED": NEON, "UNKNOWN": DIM,
}
STATUS_GLYPH = {
    "UP": "●", "WORKING": "◐", "IDLE": "●", "TRANSPORT_BUSY": "…",
    "DEGRADED": "◈", "DOWN": "✖", "MISMATCH": "◈", "UNMAPPED": "○",
    "DETECTED": "◉", "UNKNOWN": "·",
}
PANE_BOARD_LABELS_KEY = "pane_board_labels"


def _state_message(kind: str, detail: str = "") -> str:
    messages = {
        "loading": "ASUS에서 에이전트를 찾는 중…",
        "unreachable": "ASUS에 연결할 수 없습니다. ASUS 전원과 네트워크를 확인한 뒤 [다시 시도]를 누르세요.",
        "empty": "ASUS에서 실행 중인 에이전트가 없습니다. ASUS tmux에서 에이전트를 시작하면 자동으로 나타납니다.",
    }
    message = messages[kind]
    return f"{message} ({detail})" if kind == "unreachable" and detail else message


def _pane_board_label(config: dict, pane_id: str, fallback: str) -> str:
    labels = config.get(PANE_BOARD_LABELS_KEY, {})
    label = labels.get(pane_id) if isinstance(labels, dict) else None
    return str(label) if label else fallback


def _save_pane_board_label(config: dict, pane_id: str, label: str) -> None:
    if not label or "\n" in label or "\r" in label:
        raise ValueError("pane 별칭은 한 줄의 비어 있지 않은 이름이어야 합니다")
    label = label.strip()
    if not label:
        raise ValueError("pane 별칭은 한 줄의 비어 있지 않은 이름이어야 합니다")
    labels = config.setdefault(PANE_BOARD_LABELS_KEY, {})
    if not isinstance(labels, dict):
        labels = config[PANE_BOARD_LABELS_KEY] = {}
    labels[pane_id] = label
    save_config(config)


def _preview_text(previous: str | None, result: object) -> tuple[str, bool]:
    """Return (display_text, is_fresh). Never wipe last-good pane with a timeout wall."""
    text = result if isinstance(result, str) else f"실패: {result}"
    failed = (
        text.startswith("(미리보기 불가:")
        or text.startswith("실패:")
        or text.startswith("remote tmux command timed out")
        or "미리보기 보류" in text
    )
    if failed and previous:
        # Keep last known-good content; soft degrade only.
        return previous, False
    if failed and not previous:
        return "(pane 읽는 중… 잠시 후 자동 갱신)", False
    return text, True


def _summary_text(rows: list[dict]) -> str:
    from actl.core.projection import board_counts, runtime_counts

    board = board_counts(rows)
    runtime = runtime_counts(rows)
    return (f"에이전트 {len(rows)} · 작업 중 {runtime['WORKING']} · 대기 {runtime['IDLE']} · "
            f"문제 {board['error']} · 확인 중 {board['unknown']}")


def inspector_truth(row: dict) -> dict[str, str]:
    """Derive all Founder-facing inspector fields from one runtime row.

    Title, detail, and action target must always share the same runtime_key.
    """
    target = str(row.get("target") or "-")
    display = str(row.get("display") or row.get("agent") or "UNKNOWN")
    pane_id = str(row.get("pane_id") or target)
    result_label = {"READY": "준비됨", "WAITING": "대기", "UNKNOWN": "미확인"}.get(
        row.get("result_state"), "미확인"
    )
    if target == "-":
        title = f"{display} — no live runtime"
    else:
        title = f"{display} · {pane_id} — LIVE PANE"
    detail = (
        f"Machine {row.get('machine', 'UNKNOWN')} · Project {row.get('project', 'UNKNOWN')}\n"
        f"Agent {display} · Role {row.get('role', 'UNKNOWN')}\n"
        f"Model/Profile {row.get('model_profile', 'UNKNOWN')} · State {row.get('runtime_state', 'UNKNOWN')}\n"
        f"Pane {pane_id} · Session {row.get('session', 'UNKNOWN')} "
        f"Window {row.get('window', 'UNKNOWN')} Pane {row.get('pane_index', 'UNKNOWN')}\n"
        f"Command {row.get('pane_command', 'UNKNOWN')} · PID {row.get('pane_pid', 'UNKNOWN')}\n"
        f"Task {row.get('current_task', 'UNKNOWN')} · Result {result_label} · Health {row.get('state', 'UNKNOWN')}\n"
        f"Controls {row.get('control_reason', 'UNKNOWN')}: {row.get('control_detail', '')}"
    )
    return {
        "runtime_key": str(row.get("runtime_key") or ""),
        "agent": str(row.get("agent") or ""),
        "display": display,
        "target": target,
        "pane_id": pane_id,
        "session": str(row.get("session") or "UNKNOWN"),
        "title": title,
        "detail": detail,
    }


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
        self.preview_degraded: set[str] = set()
        self.preview_hold_until = 0.0
        self.send_inflight = False
        self.last_submitted_prompt: str | None = None
        self.loop_phase = "READY"
        self.follow_preview_key: str | None = None
        self.follow_preview_until = 0.0
        self.refresh_interval_ms = 12000
        self.live_preview_interval_ms = 1800
        self.event_refresh_scheduled = False
        self.last_event_refresh = 0.0
        self.pending_event_panes: set[str] = set()
        self.pending_topology_refresh = False
        self.motion_phase = 0
        self.motion_labels: dict[str, object] = {}
        self.previous_rows: dict[str, dict] = {}
        self.log_visible = False
        self._build()
        self.refresh()
        self.root.after(100, self._drain)
        self.root.after(180, self._motion_tick)
        self.root.after(self.live_preview_interval_ms, self._live_preview_tick)
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
        self.summary_var = tk.StringVar(value="에이전트 0 · 작업 중 0 · 대기 0 · 문제 0 · 확인 중 0")
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
        self.project_counts = tk.StringVar(value=_state_message("loading"))
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
                                   ("COLLECT RESULT", self.on_collect, False), ("FOCUS", self.on_focus, False),
                                   ("PANE BOARD", self.on_board, False)]:
            button = self._btn(cmdbar, label, fn, primary=primary)
            button.pack(side="left", padx=2)
            self.action_buttons[label] = button
        self.action_buttons["COLLECT RESULT"].configure(state="disabled")
        from tkinter import scrolledtext

        self.notice_var = tk.StringVar(value="")
        self.notice_label = tk.Label(right, textvariable=self.notice_var, bg=PANEL, fg=DIM,
                                     font=FONT_HDR, justify="left", anchor="w", wraplength=330)
        self.notice_label.grid(row=4, column=0, sticky="ew", padx=14, pady=(0, 4))
        tk.Label(right, text="Prompt · Ctrl+Enter", bg=PANEL, fg=TXT, font=FONT_HDR).grid(row=5, column=0, sticky="w", padx=14, pady=(4, 3))
        self.msg = scrolledtext.ScrolledText(right, height=2, font=FONT, bg="#fafafa", fg=TXT,
                                             insertbackground=NEON, highlightthickness=0, borderwidth=0)
        self.msg.grid(row=6, column=0, sticky="ew", padx=14, pady=2)
        sendrow = tk.Frame(right, bg=PANEL)
        sendrow.grid(row=7, column=0, sticky="ew", pady=2, padx=14)
        self.send_btn = tk.Button(sendrow, text="SEND PROMPT", command=self.on_send, bg=ACC, fg="white",
                                  relief="flat", padx=12, pady=6)
        self.send_btn.pack(side="left")
        self.log_toggle = tk.Button(sendrow, text="▸ diagnostics", command=self.toggle_log,
                                    bg=PANEL, fg=DIM, activebackground=PANEL2, relief="flat", cursor="hand2", font=FONT)
        self.log_toggle.pack(side="left", padx=6)
        tk.Button(sendrow, text="다시 시도", command=self.refresh,
                  bg=PANEL, fg=ACC, activebackground=PANEL2, relief="flat", cursor="hand2",
                  font=FONT).pack(side="left", padx=6)
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
            self.logw.grid(row=8, column=0, sticky="nsew", pady=2)
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

    def notify(self, text: str, level: str = "ok") -> None:
        color = {"ok": OK, "warn": WARN, "bad": BAD}.get(level, DIM)
        self.notice_var.set(text)
        if hasattr(self, "notice_label"):
            self.notice_label.configure(fg=color)
        self.status_var.set(text)
        self.log(text)

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

    def _request_preview(self, row: dict, *, force: bool = False) -> None:
        key = row["runtime_key"]
        if key in self.preview_inflight and not force:
            return
        # Live preview uses one-shot SSH capture and must not wait on / block P0.
        # Still skip starting new polls while a user action owns the scheduler intent.
        if self.ssh_target and self.send_inflight:
            return
        self.preview_inflight.add(key)
        target = row["target"]

        def work():
            # Remote Board: do NOT queue through RemoteOpScheduler — oneshot live
            # capture keeps SEND/COPY free and still yields to send_inflight above.
            return _pane_preview(target, lines=14)

        def done(result) -> None:
            self.preview_inflight.discard(key)
            if self.selected != key:
                return
            if time.monotonic() < self.preview_hold_until:
                return
            live = next((r for r in self.rows if r.get("runtime_key") == key), None)
            if live is None:
                return
            truth = inspector_truth(live)
            previous = self.last_previews.get(key)
            text, fresh = _preview_text(previous, result)
            degraded = not fresh
            if fresh:
                self.last_previews[key] = text
                self.preview_degraded.discard(key)
            elif degraded:
                self.preview_degraded.add(key)
            header = "LIVE PANE PREVIEW"
            if key in self.preview_degraded and previous:
                header = "LIVE PANE PREVIEW · 갱신 지연"
            # Preserve scroll context: only rewrite body; never replace with raw timeout.
            body = self.last_previews.get(key) or text
            self.preview.delete("1.0", "end")
            self.preview.insert("end", f"{header}\n{body}")
            self.pane_title.set(truth["title"])
            self.detail_var.set(truth["detail"])
            # Do not clobber Founder loop status (제출 완료 / 작업 중 / 결과 준비됨).
            if self.loop_phase in {"READY"} and not self.send_inflight:
                self.set_status("준비")

        self._bg(work, done)

    def _live_preview_tick(self) -> None:
        """Periodic live-ish refresh for the selected runtime while Board is open."""
        try:
            row = self.current()
            if row and row.get("target") not in (None, "-", "") and not str(row.get("target")).endswith("?"):
                follow = self.follow_preview_key == row.get("runtime_key")
                import time as _time

                if follow and _time.monotonic() > self.follow_preview_until:
                    self.follow_preview_key = None
                if follow or self.auto_refresh:
                    self._request_preview(row)
        finally:
            self.root.after(self.live_preview_interval_ms, self._live_preview_tick)

    def _start_follow_preview(self, runtime_key: str, *, seconds: float = 90.0) -> None:
        import time as _time

        self.follow_preview_key = runtime_key
        self.follow_preview_until = _time.monotonic() + seconds
        row = next((r for r in self.rows if r.get("runtime_key") == runtime_key), None)
        if row:
            self._request_preview(row, force=True)

    def _set_loop_phase(self, phase: str, *, status: str | None = None) -> None:
        from actl.core.send_truth import LOOP_STATE_KO

        self.loop_phase = phase
        if status is not None:
            self.set_status(status)
        else:
            self.set_status(LOOP_STATE_KO.get(phase, phase))

    def _clear_prompt_at_submitted(self, submitted_text: str) -> None:
        """Clear composer at authoritative SUBMITTED; keep receipt for history."""
        self.last_submitted_prompt = submitted_text
        try:
            self.msg.delete("1.0", "end")
        except Exception:
            pass

    def _set_send_inflight(self, active: bool) -> None:
        self.send_inflight = active
        self._update_action_state()

    def _health_tick(self) -> None:
        if self.auto_refresh:
            self.refresh(quiet=True)
        if self.ssh_target:
            self.root.after(60000, self._health_tick)

    def rows_now(self) -> list[dict]:
        from actl.core.discovery import discover, reconcile
        from actl.core.remote_scheduler import KIND_REFRESH, P2_BACKGROUND, scheduler_for
        from actl.tui import _rows

        def work():
            detections = discover()
            updated, changes = reconcile(self.config, detections, unique_only=True)
            if changes:
                backup_config()
                save_config(updated)
                self.config = updated
            return {"rows": _rows(self.config, detections, hydrate=False), "detections": detections}

        if self.ssh_target:
            return scheduler_for(self.ssh_target).submit(
                work,
                priority=P2_BACKGROUND,
                kind=KIND_REFRESH,
                coalesce_key=f"refresh:{self.ssh_target}",
                replaceable=True,
                timeout=90.0,
            )
        return work()

    def _start_hydration(self) -> None:
        if self.hydrating or not getattr(self, "_snapshot_detections", None):
            return
        if self.ssh_target:
            from actl.core.remote_scheduler import scheduler_for

            if scheduler_for(self.ssh_target).user_action_inflight():
                return
        self.hydrating = True
        from actl.tui import _rows

        detections = self._snapshot_detections
        config = self.config

        def work():
            from actl.core.remote_scheduler import (
                KIND_HYDRATE,
                P2_BACKGROUND,
                RemoteOpCancelled,
                scheduler_for,
            )

            try:
                if self.ssh_target:
                    return scheduler_for(self.ssh_target).submit(
                        lambda: _rows(config, detections, hydrate=True),
                        priority=P2_BACKGROUND,
                        kind=KIND_HYDRATE,
                        coalesce_key=f"hydrate:{self.ssh_target}",
                        replaceable=True,
                        timeout=90.0,
                    )
                return _rows(config, detections, hydrate=True)
            except RemoteOpCancelled as exc:
                return exc

        self._bg(work, self._hydration_done)

    def _hydration_done(self, result) -> None:
        self.hydrating = False
        from actl.core.remote_scheduler import RemoteOpCancelled

        if isinstance(result, RemoteOpCancelled):
            self.log("상세 hydration 보류 — 사용자 작업 우선")
            return
        if isinstance(result, Exception):
            self.log(f"상세 hydration 실패 — 기존 inventory 유지: {result}")
            return
        self.rows = result
        self.refresh_interval_ms = 3000 if any(r.get("activity_state") == "RUNNING" for r in self.rows) else 12000
        self.summary_var.set(_summary_text(self.rows))
        self._render_projects()
        self._render_cards(self.selected)
        self._update_action_state()
        if self.selected:
            self.on_select()
        self.log("runtime detail hydration 완료")

    def refresh(self, quiet: bool = False) -> None:
        if self.refreshing:
            return
        if self.ssh_target:
            from actl.core.remote_scheduler import scheduler_for

            if scheduler_for(self.ssh_target).user_action_inflight():
                if not quiet:
                    self.log("새로고침 보류 — 사용자 작업 우선")
                return
        self.refreshing = True
        self.set_status("새로고침 중…")
        self.project_counts.set(_state_message("loading"))
        if not quiet:
            self.log("새로고침 중…")
        self._bg(self.rows_now, lambda r: self._refresh_done(r, quiet))

    def _refresh_done(self, result, quiet: bool = False) -> None:
        if isinstance(result, Exception):
            self.refreshing = False
            self.rows = []
            self.selected = None
            message = _state_message("unreachable", str(result))
            self.preview.delete("1.0", "end")
            self.preview.insert("end", message)
            self.project_counts.set(message)
            self._render_cards()
            self._render_projects()
            self.set_status("ASUS 연결 안 됨")
            self.log(f"새로고침 실패: {result}")
            self._update_action_state()
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
        self.summary_var.set(_summary_text(self.rows))
        self.set_status("ASUS 연결됨 · 에이전트 0" if not self.rows else
                        (f"ASUS ● CONNECTED · {len(self.rows)} runtimes" if self.ssh_target else f"{len(self.rows)} runtimes"))
        self._render_projects()
        if not self.rows:
            message = _state_message("empty")
            self.preview.delete("1.0", "end")
            self.preview.insert("end", message)
            self.project_counts.set(message)
        if not quiet:
            self.log(f"새로고침 완료 ({len(self.rows)} runtime instances)")
        if self.rows:
            keep = prev_sel if any(r["runtime_key"] == prev_sel for r in self.rows) else self.rows[0]["runtime_key"]
            self.selected = keep
            self._highlight(keep)
            self.on_select()
        self._update_action_state()
        self._start_hydration()
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
        self.project_counts.set(_state_message("empty") if not self.rows else
                                f"{len(self.rows)} runtime instances · {len(counts)} projects")
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
            no_match = _state_message("empty") if not self.project_filter and not query and mode == "전체" else "조건에 맞는 에이전트 없음"
            tk.Label(self.agent_cards, text=no_match, bg=BG, fg=DIM,
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
        enabled = bool(row and row.get("control_ready")) and not self.send_inflight
        self.send_btn.configure(state="normal" if enabled else "disabled")
        for label in ("SEND PROMPT", "COPY RESULT", "FOCUS"):
            if label == "SEND PROMPT":
                self.action_buttons[label].configure(state="normal" if enabled else "disabled")
            else:
                ready = bool(row is not None and row.get("control_ready"))
                self.action_buttons[label].configure(state="normal" if ready else "disabled")

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
        truth = inspector_truth(row)
        # Synchronize title + detail + action identity before any async preview.
        self.pane_title.set(truth["title"])
        self.detail_var.set(truth["detail"])
        tgt = truth["target"]
        if tgt == "-":
            self.preview.delete("1.0", "end")
            self.preview.insert("end", f"{truth['display']}: live runtime 없음")
            return
        self.set_status(f"{truth['display']} 로딩…")
        self.log(f"{truth['display']} 미리보기 로딩…")

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
        from actl.core.remote_scheduler import KIND_FOCUS, P0_USER, scheduler_for
        from actl.core.tmux import _run

        target_host = self.ssh_target
        if target_host:
            scheduler_for(target_host).pause_background()

        def work():
            try:
                def focus():
                    _run(["tmux", "select-pane", "-t", row["target"]])
                    return True

                if target_host:
                    return scheduler_for(target_host).submit(
                        focus,
                        priority=P0_USER,
                        kind=KIND_FOCUS,
                        replaceable=False,
                        timeout=90.0,
                    )
                return focus()
            finally:
                if target_host:
                    scheduler_for(target_host).resume_background()

        def done(result) -> None:
            if result is True:
                self.log(f"{row['display']} pane focused")
            else:
                self.log(f"FOCUS failed: {result}")

        self._bg(work, done)

    def on_collect(self) -> None:
        row = self.current()
        if not row:
            return
        self.log("COLLECT RESULT disabled: no managed Task/Run command is bound to this runtime")

    def on_copy(self) -> None:
        row = self.current()
        if not row or not row.get("control_ready"):
            self.notify("에이전트를 먼저 선택하세요", "warn")
            return
        tgt = row["target"]
        if tgt == "-" or tgt.endswith("?"):
            self.notify("에이전트를 먼저 선택하세요", "warn")
            return
        from actl.core.remote_scheduler import KIND_COPY, P0_USER, FOREGROUND_ACQUIRE_MAX_S, scheduler_for
        from actl.core.send_truth import (
            COPY_ACQUIRE_TIMEOUT,
            COPY_CLEARING,
            COPY_QUEUED,
            COPY_READING,
            COPY_STATE_KO,
            RESULT_CLASS_KO,
            classify_result,
            result_hash as hash_result,
        )

        target_host = self.ssh_target
        self.set_status(COPY_STATE_KO[COPY_QUEUED])
        self.log(f"{row['display']} · {COPY_STATE_KO[COPY_QUEUED]}")
        if target_host:
            sched = scheduler_for(target_host)
            sched.pause_background()
            self.set_status(COPY_STATE_KO[COPY_CLEARING])
            self.log(f"{row['display']} · {COPY_STATE_KO[COPY_CLEARING]}")

        def work():
            from actl.core.audit import record
            import hashlib

            owned = threading.Event()

            def copy_body():
                owned.set()
                # UI phase: only claim reading once P0 owns the transport.

                def mark_reading():
                    self.set_status(COPY_STATE_KO[COPY_READING])

                try:
                    self.root.after(0, mark_reading)
                except Exception:
                    pass
                result = extract_last_response(row["agent"], tgt, self.config)
                current_hash = hash_result(result.text)
                result_class, corr = classify_result(row["agent"], tgt, current_hash, text=result.text)
                if not result.text:
                    record("copy", agent=row["agent"], target=tgt, ok=False,
                           source=result.source, confidence=result.confidence,
                           result_class=result_class)
                    return ("empty", result.detail, result_class, corr)
                # Clipboard write only after classification authorizes NEW_RESULT.
                from actl.core.send_truth import clipboard_write_allowed

                if not clipboard_write_allowed(result_class):
                    record(
                        "copy",
                        agent=row["agent"],
                        target=tgt,
                        ok=False,
                        source=result.source,
                        confidence=result.confidence,
                        chars=len(result.text),
                        result_class=result_class,
                        clipboard="skipped",
                    )
                    return ("no-clip", result_class, len(result.text), corr, result.text)
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
                           result_hash=result_hash, result_class=result_class)
                    return ("ok", backend, len(result.text), result_hash, result_class, corr, result.text)
                except Exception as exc:
                    record("copy", agent=row["agent"], target=tgt, ok=False,
                           source=result.source, confidence=result.confidence, error=type(exc).__name__,
                           result_class=result_class)
                    return ("clip-fail", str(exc), result.text[:2000], result_class, corr)

            try:
                if target_host:
                    def watchdog():
                        if not owned.wait(FOREGROUND_ACQUIRE_MAX_S):
                            try:
                                self.root.after(
                                    0,
                                    lambda: self.set_status(COPY_STATE_KO[COPY_ACQUIRE_TIMEOUT]),
                                )
                            except Exception:
                                pass

                    threading.Thread(target=watchdog, daemon=True).start()
                    return scheduler_for(target_host).submit(
                        copy_body,
                        priority=P0_USER,
                        kind=KIND_COPY,
                        replaceable=False,
                        timeout=90.0,
                    )
                return copy_body()
            finally:
                if target_host:
                    scheduler_for(target_host).resume_background()

        def done(result) -> None:
            from actl.core.send_truth import LOOP_COPIED, LOOP_RESULT_READY, LOOP_STATE_KO, LOOP_WORKING

            if isinstance(result, Exception):
                self.notify("클립보드 복사 실패", "bad")
                return
            if result[0] == "ok":
                from actl.core.state import acknowledge

                result_class = result[4]
                label = RESULT_CLASS_KO.get(result_class, result_class)
                if result_class == "NEW_RESULT":
                    acknowledge(row["agent"], result[3])
                    self._set_loop_phase(LOOP_COPIED, status=LOOP_STATE_KO[LOOP_COPIED])
                    self.notify(f"복사 완료 {result[2]}자", "ok")
                    self.preview_hold_until = time.monotonic() + 8
                    # Keep live pane visible; append concise confirmation above last preview.
                    prior = self.last_previews.get(row["runtime_key"]) or ""
                    self.preview.delete("1.0", "end")
                    self.preview.insert(
                        "end",
                        f"✓ {LOOP_STATE_KO[LOOP_COPIED]} · {label}\n\n"
                        f"{result[6][:4000]}\n\n--- LIVE PANE ---\n{prior}",
                    )
                else:
                    self.notify("이전 결과와 같아서 복사하지 않았습니다", "warn")
            elif result[0] == "no-clip":
                result_class = result[1]
                label = RESULT_CLASS_KO.get(result_class, result_class)
                text = result[4] if len(result) > 4 else ""
                if result_class == "RESULT_PENDING":
                    self._set_loop_phase(LOOP_WORKING, status=LOOP_STATE_KO[LOOP_WORKING])
                    self.notify("아직 새 결과가 없습니다 — 작업이 끝나면 다시 눌러 주세요", "warn")
                elif result_class == "STALE_RESULT":
                    self.notify("이전 결과와 같아서 복사하지 않았습니다", "warn")
                elif result_class == "NEW_RESULT":
                    self._set_loop_phase(LOOP_RESULT_READY, status=LOOP_STATE_KO[LOOP_RESULT_READY])
                    self.notify("아직 새 결과가 없습니다 — 작업이 끝나면 다시 눌러 주세요", "warn")
                else:
                    self.notify("이전 결과와 같아서 복사하지 않았습니다", "warn")
                # Do not overwrite live pane with diagnostic walls.
                if result_class == "STALE_RESULT" and text:
                    prior = self.last_previews.get(row["runtime_key"]) or text
                    self.preview.delete("1.0", "end")
                    self.preview.insert(
                        "end",
                        f"⚠ {label} — 클립보드 미변경\n\n--- LIVE PANE ---\n{prior[:4000]}",
                    )
            elif result[0] == "empty":
                self.notify("아직 새 결과가 없습니다 — 작업이 끝나면 다시 눌러 주세요", "warn")
            else:
                self.notify("클립보드 복사 실패", "bad")
                # Keep Founder able to see pane; show error in diagnostics log only.

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
                            wid, "end", text=f"{pane.pane_id}  {_pane_board_label(self.config, pane.pane_id, pane.title or '(untitled)')}",
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
                initial = _pane_board_label(self.config, pane.pane_id, pane.title or "(untitled)")
                name = simpledialog.askstring("pane 별칭 변경", f"{pane.pane_id} 표시 이름", initialvalue=initial, parent=top)
                if name is None:
                    return
                try:
                    _save_pane_board_label(self.config, pane.pane_id, name)
                except ValueError as exc:
                    messagebox.showerror("pane 별칭 실패", str(exc), parent=top)
                    return
                top.destroy()
                self.refresh()
                return
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
            self.notify("에이전트를 먼저 선택하세요", "warn")
            return
        if self.send_inflight:
            self.notify("이미 전송 중 — 완료될 때까지 대기", "warn")
            return
        text = self.msg.get("1.0", "end").strip()
        if not text:
            self.notify("보낼 내용을 입력하세요", "warn")
            return
        if not messagebox.askyesno("전송 확인", f"{row['display']} ({row['target']})에 메시지를 전송할까요?\n\n{text[:240]}{'…' if len(text) > 240 else ''}"):
            return
        from actl.core.remote_scheduler import (
            KIND_SEND,
            P0_USER,
            FOREGROUND_ACQUIRE_MAX_S,
            scheduler_for,
        )
        from actl.core.send_truth import (
            CLEARING_BACKGROUND,
            FOREGROUND_ACQUIRE_TIMEOUT,
            LOOP_SENDING,
            LOOP_SUBMITTED,
            LOOP_WORKING,
            SEND_FAILED,
            SEND_QUEUED,
            SENDING,
            SEND_STATE_KO,
            START_ACKNOWLEDGED,
            SUBMITTED,
            begin_send,
            set_send_state,
        )

        previous_hash = row.get("result_hash") or None
        begin_send(row["agent"], row["target"], previous_result_hash=previous_hash)
        set_send_state(row["agent"], row["target"], SEND_QUEUED)
        self._set_send_inflight(True)
        self._set_loop_phase(LOOP_SENDING, status=SEND_STATE_KO[SEND_QUEUED])
        self.log(f"{row['display']} · {SEND_STATE_KO[SEND_QUEUED]}")
        target_host = self.ssh_target
        if target_host:
            scheduler_for(target_host).pause_background()
            set_send_state(row["agent"], row["target"], CLEARING_BACKGROUND)
            self.set_status(SEND_STATE_KO[CLEARING_BACKGROUND])
            self.log(f"{row['display']} · {SEND_STATE_KO[CLEARING_BACKGROUND]}")

        def work():
            from actl.core.activity import observe_activity
            from actl.core.remote import ManagedUnsupported, is_remote, remote_managed_send
            from actl.core.tmux import send_prompt_staged
            import threading

            owned = threading.Event()

            def _ack_from_activity(evidence: dict):
                activity, detail = observe_activity(row["target"])
                if activity == "RUNNING":
                    set_send_state(
                        row["agent"],
                        row["target"],
                        START_ACKNOWLEDGED,
                        evidence={**evidence, "activity": activity, "detail": detail},
                    )
                    return ("acked", activity, evidence)
                import time as _time

                for _ in range(3):
                    _time.sleep(0.35)
                    activity, detail = observe_activity(row["target"])
                    if activity == "RUNNING":
                        set_send_state(
                            row["agent"],
                            row["target"],
                            START_ACKNOWLEDGED,
                            evidence={**evidence, "activity": activity, "detail": detail},
                        )
                        return ("acked", activity, evidence)
                return ("submitted", activity, evidence)

            def send_body():
                owned.set()
                set_send_state(row["agent"], row["target"], SENDING)

                def mark_sending():
                    self._set_loop_phase(LOOP_SENDING, status=SEND_STATE_KO[SENDING])

                try:
                    self.root.after(0, mark_sending)
                except Exception:
                    pass
                if is_remote():
                    try:
                        from actl.core.remote import ManagedSendDelivery

                        committed = threading.Event()

                        def on_committed(delivery: ManagedSendDelivery) -> None:
                            set_send_state(
                                row["agent"],
                                row["target"],
                                SUBMITTED,
                                evidence=dict(delivery.evidence),
                            )
                            committed.set()

                            def mark_submitted():
                                # Authoritative SUBMITTED: clear Prompt immediately.
                                self._clear_prompt_at_submitted(text)
                                self._set_loop_phase(
                                    LOOP_SUBMITTED, status=SEND_STATE_KO[SUBMITTED]
                                )
                                self._start_follow_preview(row["runtime_key"])

                            try:
                                self.root.after(0, mark_submitted)
                            except Exception:
                                pass

                        delivery = remote_managed_send(
                            row["agent"],
                            row["target"],
                            text,
                            on_committed=on_committed,
                            auto_cleanup=False,
                        )
                        if not committed.is_set():
                            set_send_state(
                                row["agent"],
                                row["target"],
                                SUBMITTED,
                                evidence=dict(delivery.evidence),
                            )

                            def mark_submitted_fallback():
                                self._clear_prompt_at_submitted(text)
                                self._set_loop_phase(
                                    LOOP_SUBMITTED, status=SEND_STATE_KO[SUBMITTED]
                                )
                                self._start_follow_preview(row["runtime_key"])

                            try:
                                self.root.after(0, mark_submitted_fallback)
                            except Exception:
                                pass

                        def _cleanup_lease() -> None:
                            ok = delivery.cleanup()
                            if not ok and delivery.cleanup_error:
                                from actl.core.audit import record

                                record(
                                    "managed_cleanup",
                                    agent=row["agent"],
                                    target=row["target"],
                                    ok=False,
                                    error=delivery.cleanup_error[:300],
                                    command_id=delivery.command_id,
                                )

                        threading.Thread(
                            target=_cleanup_lease,
                            name="actl-managed-cleanup",
                            daemon=True,
                        ).start()
                        return _ack_from_activity(dict(delivery.evidence))
                    except ManagedUnsupported:
                        pass
                staged = send_prompt_staged(row["target"], text, press_enter=True)
                if not staged.get("ok"):
                    set_send_state(
                        row["agent"],
                        row["target"],
                        SEND_FAILED,
                        error=str(staged.get("error") or "send failed"),
                        evidence={"stages": staged.get("stages"), "path": "staged"},
                    )
                    return ("failed", staged.get("error") or "send failed", staged)
                completed = staged.get("completedStages") or []
                if "paste_buffer" not in completed or "enter" not in completed:
                    set_send_state(
                        row["agent"],
                        row["target"],
                        SEND_FAILED,
                        error="paste/enter evidence missing",
                        evidence={"stages": staged.get("stages"), "path": "staged"},
                    )
                    return ("failed", "paste/enter evidence missing", staged)
                evidence = {
                    "path": "staged",
                    "stages": staged.get("stages"),
                    "disposition": staged.get("deliveryDisposition"),
                }
                set_send_state(
                    row["agent"],
                    row["target"],
                    SUBMITTED,
                    evidence=evidence,
                )

                def mark_staged_submitted():
                    self._clear_prompt_at_submitted(text)
                    self._set_loop_phase(LOOP_SUBMITTED, status=SEND_STATE_KO[SUBMITTED])
                    self._start_follow_preview(row["runtime_key"])

                try:
                    self.root.after(0, mark_staged_submitted)
                except Exception:
                    pass
                return _ack_from_activity(evidence)

            try:
                if target_host:
                    def watchdog():
                        if not owned.wait(FOREGROUND_ACQUIRE_MAX_S):
                            set_send_state(
                                row["agent"],
                                row["target"],
                                FOREGROUND_ACQUIRE_TIMEOUT,
                            )
                            try:
                                self.root.after(
                                    0,
                                    lambda: self.set_status(
                                        SEND_STATE_KO[FOREGROUND_ACQUIRE_TIMEOUT]
                                    ),
                                )
                            except Exception:
                                pass

                    threading.Thread(target=watchdog, daemon=True).start()
                    return scheduler_for(target_host).submit(
                        send_body,
                        priority=P0_USER,
                        kind=KIND_SEND,
                        replaceable=False,
                        timeout=90.0,
                    )
                return send_body()
            except Exception as exc:
                set_send_state(row["agent"], row["target"], SEND_FAILED, error=str(exc))
                return ("failed", str(exc), None)
            finally:
                if target_host:
                    scheduler_for(target_host).resume_background()

        def done(result) -> None:
            self._set_send_inflight(False)
            if isinstance(result, Exception):
                set_send_state(row["agent"], row["target"], SEND_FAILED, error=str(result))
                self.set_status(SEND_STATE_KO[SEND_FAILED])
                self.loop_phase = "READY"
                # Pre-commit failure: Prompt text is preserved for retry.
                detail = str(result) or type(result).__name__
                from actl.core.remote_scheduler import is_transport_contention

                if is_transport_contention(result):
                    self.notify(f"전송 실패: 원격 통신 대기 중 (매핑 DOWN 아님): {detail[:120]}", "bad")
                else:
                    self.notify(f"전송 실패: {detail[:120]}", "bad")
                return
            status, detail, staged = result
            if status == "acked":
                self._set_loop_phase(LOOP_WORKING, status=SEND_STATE_KO[START_ACKNOWLEDGED])
                self.notify("작업 시작 확인", "ok")
                self._start_follow_preview(row["runtime_key"])
            elif status == "submitted":
                self._set_loop_phase(LOOP_SUBMITTED, status=SEND_STATE_KO[SUBMITTED])
                self.notify("제출 완료", "ok")
                self._start_follow_preview(row["runtime_key"])
            else:
                self.set_status(SEND_STATE_KO[SEND_FAILED])
                self.loop_phase = "READY"
                self.notify(f"전송 실패: {str(detail)[:120]}", "bad")
                # Failed after possible commit: do not restore Prompt (avoid duplicate).
                # Failed before commit: Prompt was never cleared.

        self._bg(work, done)

    def run(self) -> int:
        self.root.mainloop()
        return 0


def run_gui(ssh_target: str | None = None) -> int:
    return Board(ssh_target=ssh_target).run()
