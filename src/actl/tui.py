"""MainPC용 에이전트 보드: 선택, 미리보기, 복사, 재매핑, 전송.

curses 없이 ANSI + stdin만 사용. SSH에서도 동작하며, 클립보드가 막히면
``--print``(화면 출력) 경로로 수동 복사 가능.

키:
  숫자        에이전트 선택 + live pane 미리보기
  c / p       마지막 응답 복사 / 화면 출력
  m           재매핑 (빈 live pane 목록에서 선택)
  s           선택한 에이전트 pane에 메시지 전송
  h 또는 ?    도움말 (전체 키 + 첫실행 튜토리얼)
  r           새로고침 (자동 재매핑 + 전체 재탐색)
  q           종료
"""
from __future__ import annotations

import sys
import hashlib
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

from actl.agents.extract import extract_last_response
from actl.core.config import backup_config, get_target, load_config, save_config
from actl.core.discovery import STRONG_CONFIDENCE, discover, manual_map, mapping_state
from actl.core.registry import AGENTS
from actl.core.tmux import capture_pane, pane_field
from actl.core.validation import validate_target
from actl.utils.clipboard import copy_text

CLEAR = "\x1b[2J\x1b[H"
BOLD = "\x1b[1m"
DIM = "\x1b[2m"
RESET = "\x1b[0m"


def _validate_live_pane(agent: str, target: str, pane) -> object:
    try:
        return validate_target(agent, target, pane=pane)
    except TypeError as exc:
        if "unexpected keyword argument 'pane'" not in str(exc):
            raise
        return validate_target(agent, target)


def _rows(config: dict, detections=None, overlays: dict | None = None, *, hydrate: bool = True) -> list[dict]:
    """Project each verified live detection as its own runtime row."""
    from actl.core.discovery import discover as _disc
    from actl.core.models import PaneInfo

    all_dets = [d for d in (detections if detections is not None else _disc())
                if d.agent and d.confidence in STRONG_CONFIDENCE]
    from actl.core.state import seen_results
    seen = seen_results()

    from actl.core.tmux import REMOTE_SSH_TARGET
    from actl.core.projection import UNKNOWN
    machine = config.get("machine") or REMOTE_SSH_TARGET or "local"

    def verified_project_names(paths: set[str]) -> dict[str, str]:
        import subprocess
        from actl.core.tmux import _no_window, _remote_args

        names: dict[str, str] = {}
        paths = {path for path in paths if path and path not in {"-", UNKNOWN}}
        if REMOTE_SSH_TARGET and paths:
            script = "for p do r=$(git -C \"$p\" rev-parse --show-toplevel 2>/dev/null) && printf '%s\\t%s\\n' \"$p\" \"$r\"; done"
            try:
                result = subprocess.run(
                    _remote_args(["sh", "-c", script, "sh", *sorted(paths)]),
                    capture_output=True, text=True, encoding="utf-8", errors="replace",
                    check=False, timeout=5, **_no_window(),
                )
                for line in result.stdout.splitlines():
                    path, _, root = line.partition("\t")
                    if root:
                        names[path] = Path(root).name or UNKNOWN
                return names
            except (OSError, subprocess.SubprocessError):
                return names
        for path in paths:
            if not path or path in {"-", UNKNOWN}:
                continue
            try:
                result = subprocess.run(
                    _remote_args(["git", "-C", path, "rev-parse", "--show-toplevel"]),
                    capture_output=True, text=True, encoding="utf-8", errors="replace",
                    check=False, timeout=5, **_no_window(),
                )
                root = result.stdout.strip() if result.returncode == 0 else ""
                if root:
                    names[path] = Path(root).name or UNKNOWN
            except (OSError, subprocess.SubprocessError):
                continue
        return names

    project_names = verified_project_names({d.pane.current_path for d in all_dets})

    def runtime_key(name: str, pane: PaneInfo | None = None, *, target: str = "-", path: str = "-") -> str:
        """Projection key from existing runtime evidence, not a new identity."""
        if pane is not None:
            return "|".join((str(machine), name, pane.target, pane.pane_id,
                             str(pane.pane_pid or "?"), pane.current_path or "-"))
        return "|".join((str(machine), name, target, path))

    def tmux_coordinates(target: str) -> dict[str, str]:
        parts = target.split(":", 1)
        if len(parts) != 2 or "." not in parts[1]:
            return {"session": UNKNOWN, "window": UNKNOWN, "pane_index": UNKNOWN}
        window, pane = parts[1].split(".", 1)
        return {"session": parts[0], "window": window, "pane_index": pane}

    instances: list[tuple[str, str, object, object | None]] = []
    for detection in all_dets:
        name = detection.agent
        instances.append((runtime_key(name, detection.pane), name, AGENTS[name], detection))
    def build(item: tuple[str, str, object, object | None]) -> dict:
        key, name, spec, det = item
        target = "-"
        state = "UNMAPPED"
        pane_path = "-"
        control_ready = False
        control_reason = "UNMAPPED"
        control_detail = "strong runtime detected but no validated mapping"
        live_runtime = det is not None
        try:
            mapped_target = get_target(config, name).target
        except ValueError:
            mapped_target = None
        if det is not None:
            target = det.pane.pane_id
            pane_path = det.pane.current_path
            # Validate the selected live pane itself. Another pane using the
            # same Agent family does not make this instance ambiguous.
            validation = _validate_live_pane(name, target, det.pane)
            state = validation.state
            control_ready = validation.valid
            if state == "UP":
                # Refine UP with activity once hydrated; temporary busy stays distinct.
                pass
            control_reason = "READY" if control_ready and state != "TRANSPORT_BUSY" else (
                "TRANSPORT_BUSY" if state == "TRANSPORT_BUSY" else (
                "STALE" if state == "DOWN" else
                "MISMATCH" if state == "MISMATCH" else state
            ))
            control_detail = validation.detail or f"validated selected runtime state is {state}"
            if state == "TRANSPORT_BUSY":
                control_detail = "원격 통신 대기 중 — 매핑은 유지됨 (재매핑 불필요)"
        elif mapped_target:
            target = mapped_target
            validation = validate_target(name, target)
            state = validation.state
            pane_path = validation.path
            control_ready = validation.valid
            control_reason = "READY" if control_ready and state != "TRANSPORT_BUSY" else state
            control_detail = validation.detail or f"validated target state is {state}"
            if state == "TRANSPORT_BUSY":
                control_detail = "원격 통신 대기 중 — 매핑은 유지됨 (재매핑 불필요)"
        preview = ""
        pane_preview = ""
        detail = ""
        busy = "-"
        activity_state = "UNKNOWN"
        result_flag = "-"
        result_hash = ""
        result_state = "UNKNOWN"
        coordinates = tmux_coordinates(det.pane.target if det is not None else target)
        if target != "-" and live_runtime:
            if hydrate:
                try:
                    from actl.core.activity import observe_activity

                    activity_state, _ = observe_activity(target)
                    busy = {"RUNNING": "실행중", "IDLE": "유휴", "WAITING_INPUT": "승인 기다림", "UNKNOWN": "미확인"}[activity_state]
                    if state == "UP":
                        if activity_state == "RUNNING":
                            state = "WORKING"
                        elif activity_state == "IDLE":
                            state = "IDLE"
                        elif activity_state == "WAITING_INPUT":
                            state = "BLOCKED"
                except Exception as exc:
                    from actl.core.remote_scheduler import is_transport_contention

                    if is_transport_contention(exc) and state == "UP":
                        state = "TRANSPORT_BUSY"
                        control_ready = True
                        control_reason = "TRANSPORT_BUSY"
                        control_detail = "원격 통신 대기 중 — 매핑은 유지됨 (재매핑 불필요)"
                    busy = "?"
        if target != "-" and control_ready and hydrate and state != "TRANSPORT_BUSY":
            try:
                result = extract_last_response(name, target, config)
                if result.text:
                    first = result.text.strip().splitlines()[0] if result.text.strip() else ""
                    preview = first[:100]
                    result_flag = f"답 {len(result.text)}자"
                    result_state = "READY"
                    result_hash = hashlib.sha256(result.text.encode("utf-8")).hexdigest()[:16]
                else:
                    detail = result.detail or "no text"
                    result_flag = "답 없음"
                    result_state = "WAITING"
            except Exception as exc:
                from actl.core.remote_scheduler import is_transport_contention

                if is_transport_contention(exc):
                    state = "TRANSPORT_BUSY"
                    control_ready = True
                    control_reason = "TRANSPORT_BUSY"
                    control_detail = "원격 통신 대기 중 — 매핑은 유지됨 (재매핑 불필요)"
                else:
                    detail = str(exc)[:80]
                    result_flag = "?오류"
        from actl.core.projection import project_metadata
        profile = None
        if det is not None and "claude profile " in det.evidence:
            profile = Path(det.evidence.rsplit(" ", 1)[-1]).name
        overlay = None
        if isinstance(overlays, dict):
            overlay = overlays.get(key) or overlays.get(name)
        metadata = project_metadata(
            config, name, pane_path,
            activity_state=activity_state,
            result_state=result_state,
            profile=profile,
            overlay=overlay,
        )
        if metadata.get("project") == UNKNOWN:
            metadata["project"] = project_names.get(pane_path, UNKNOWN)
        return {
            "key": key, "runtime_key": key, "agent": name, "display": spec.display_name,
            "target": target, "state": state, "preview": preview, "detail": detail,
            "pane_preview": pane_preview,
            "busy": busy, "activity_state": activity_state, "result_flag": result_flag,
            "result_state": result_state, "result_hash": result_hash,
            "machine": machine, "pane_path": pane_path, "control_ready": control_ready,
            "control_reason": control_reason, "control_detail": control_detail,
            "live_runtime": live_runtime,
            "runtime_identity": key,
            "pane_id": det.pane.pane_id if det is not None else UNKNOWN,
            "pane_target": det.pane.target if det is not None else target,
            "pane_command": det.pane.current_command if det is not None else UNKNOWN,
            "pane_pid": str(det.pane.pane_pid) if det is not None and det.pane.pane_pid else UNKNOWN,
            "session": coordinates["session"], "window": coordinates["window"],
            "pane_index": coordinates["pane_index"],
            "detection_evidence": det.evidence if det is not None else UNKNOWN,
            **metadata,
            "project": "UNASSIGNED" if metadata.get("project") == "UNKNOWN" else metadata.get("project"),
            "unread": bool(result_hash and seen.get(name) != result_hash),
        }

    # ponytail: bounded workers hide slow independent pane/storage reads;
    # increase only after measuring a real remote saturation problem.
    with ThreadPoolExecutor(max_workers=min(4, max(1, len(instances)))) as pool:
        rows = list(pool.map(build, instances))
    role_order = {"PM": 0, "BUILDER": 1, "QA": 2}
    rows.sort(key=lambda row: (row.get("machine", ""), row.get("project", "UNKNOWN") == "UNASSIGNED",
                               row.get("project", "UNKNOWN"), role_order.get(row.get("role"), 3),
                               row.get("runtime_identity", "")))
    for index, row in enumerate(rows, 1):
        row["key"] = str(index)
        row["key_display"] = str(index)
    return rows


STATE_KO = {
    "UP": "미확인",
    "WORKING": "일하는 중",
    "IDLE": "쉬는 중",
    "BLOCKED": "승인 기다림",
    "TRANSPORT_BUSY": "미확인",
    "DEGRADED": "미확인",
    "DOWN": "연결 끊김",
    "UNKNOWN": "미확인",
    "MISMATCH": "미확인",
    "UNMAPPED": "미확인",
    "DETECTED": "미확인",
}

HELP_TEXT = """\
actl 에이전트 보드 — 도움말

  1-8     에이전트 선택 + live pane 전체 미리보기 (터미널 크기에 맞춤)
          + 매핑/복사 상태 검증행 (정상·복사 가능/불가 즉시 표시)
  c       선택한 에이전트의 마지막 응답 복사 (SSH=OSC52, 로컬=wl-copy/xclip/xsel)
  p       마지막 응답 화면 출력 (클립보드 막히면 수동 복사)
  m       재매핑: 이 에이전트의 빈 live pane 목록 표시, 번호 또는 %ID 선택
          (예: %69). OpenCode는 TUI만 매핑하며 세션은 별도 확인.
  s       전송: 여러 줄 입력 후 '::send' 줄로 종료 (::cancel은 취소)
  r       새로고침: 오래된 매핑 제거 + 자동 매핑 + 전체 재탐색
  v       전체 live pane 보드 (모든 tmux pane + 감지 에이전트 + 매핑 상태)
  V       v + 전 에이전트 복사 자동검증
  pane키   v 목록의 [번호] 키: 미리보기 + 그 pane로 즉시 매핑
  (c 실패 시 응답 텍스트를 바로 화면에 자동 출력 — 수동 복사 가능)
  h / ?   이 도움말
  q       종료

첫실행 튜토리얼 (MainPC → SSH → asus):
  1. tmux pane에서 에이전트 실행 (grok, opencode, claude, codex …).
  2. 실행: actl tui  (MainPC 로컬이면: actl tui --ssh asus)
  3. 1-8 눌러 에이전트 선택 — live pane 미리보기가 뜸.
     pane이 틀리면 m → 목록에서 올바른 pane 선택.
  4. c 복사 (안 되면 p 출력 후 수동 복사).
  5. s 메시지 전송 (::send로 종료).
  6. 에이전트 켜고 끈 뒤엔 r 새로고침. q 종료.

참고:
  - SSH 클립보드는 OSC52 사용 (Windows Terminal: 켜짐, 일부 SSH
    클라이언트는 차단 — 그땐 p 눌러 수동 복사).
  - 같은 에이전트 pane 여러 개: 가장 최근 시작 프로세스 자동 선택,
    기존 live 매핑은 유지. 동점/판독불가만 직접 질문.
  - OpenCode 서버(`opencode serve`)는 pane 후보에서 제외.
  - `opencode --auto`는 pane 매핑과 세션 바인딩이 별개다. 결과 복사는
    exact 세션이 확인될 때만 가능하며, 필요하면 `actl bind opencode`를 사용.
아무 키나 눌러 돌아가기.
"""


def _render(rows: list[dict], selected: int, message: str = "") -> None:
    sys.stdout.write(CLEAR)
    sys.stdout.write(
        f"{BOLD}actl — 에이전트 보드{DIM}  (자동갱신 5초, 숫자=선택+미리보기, c=복사, p=출력, "
        f"m=재매핑, s=전송, v=pane보드, h=도움말, r=새로고침, q=종료){RESET}\n\n"
    )
    for i, row in enumerate(rows):
        marker = ">" if i == selected else " "
        state_color = "" if row["state"] in {"UP", "IDLE"} else DIM
        state_ko = STATE_KO.get(row["state"], row["state"])
        sys.stdout.write(
            f"{marker} [{row['key']}] {state_color}{row['display']:<12} {row['target']:<6} "
            f"{state_ko:<9}{RESET} "
            f"{row['result_flag']:<6} "
            f"{row['preview'] or ('답 없음' if row['result_flag'] == '답 없음' else row['detail'])}\n"
        )
    if message:
        sys.stdout.write(f"\n{message}\n")
    sys.stdout.flush()


def _term_size() -> tuple[int, int]:
    import shutil

    try:
        size = shutil.get_terminal_size()
        return max(40, size.columns), max(10, size.lines)
    except Exception:
        return 100, 30


def _pane_geometry(target: str) -> tuple[int, int]:
    """실제 pane 크기 (없으면 터미널 크기)."""
    try:
        raw = pane_field(target, "#{pane_width}x#{pane_height}")
        w, _, h = raw.partition("x")
        return max(40, int(w)), max(10, int(h))
    except Exception:
        return _term_size()


def _pane_preview(target: str, lines: int = 0) -> str:
    """실제 pane 화면 그대로: pane 크기 기준 가시 영역, 빈줄·래핑 유지.

    기존 textwrap 재포장은 TUI 레이아웃을 깨뜨려 제거. tmux가 이미 pane
    너비에 맞춰 줄바꿈한 화면을 그대로 보여줌 (tail = 현재 화면).
    """
    from actl.core.tmux import REMOTE_SSH_TARGET, capture_pane as _cap, capture_pane_live

    if lines <= 0:
        lines = 12
    try:
        # Remote Board: bounded one-shot capture so preview never occupies P0 transport.
        if REMOTE_SSH_TARGET:
            text = capture_pane_live(target, history=max(lines, 16), timeout=3.0)
        else:
            text = _cap(target, history=lines)
    except Exception as exc:
        return f"(미리보기 불가: {exc})"
    rows = text.rstrip().splitlines()[-lines:]
    rows = [r.rstrip() for r in rows]
    while rows and not rows[0].strip():
        rows.pop(0)
    return "\n".join(rows) or "(빈 pane)"


def _verify_row(agent: str, target: str, config: dict) -> str:
    """One-line mapping/copy health check for the selected pane."""
    from actl.core.validation import validate_target

    if target == "-" or target.endswith("?"):
        return "매핑: 없음 — m 눌러 pane 선택"
    validation = validate_target(agent, target)
    if not validation.valid:
        return f"매핑: {validation.state} — {validation.detail} (m 눌러 재매핑)"
    try:
        result = extract_last_response(agent, target, config)
    except Exception as exc:
        return f"매핑: 정상 proc 확인, 추출 실패: {exc}"
    if result.text:
        return f"매핑: 정상 · 복사: 가능 ({len(result.text)}자, {result.source})"
    return f"매핑: 정상 · 복사: 불가 ({result.detail or '응답 없음'})"


def _unmapped_panes(config: dict, agent: str) -> list:
    """Live strong detections for agent on panes not mapped to another agent."""
    from actl.core.discovery import target_matches

    dets = [d for d in discover() if d.agent == agent and d.confidence in STRONG_CONFIDENCE]
    free = []
    for det in dets:
        occupied = False
        for other in AGENTS:
            if other == agent:
                continue
            try:
                other_target = get_target(config, other).target
            except ValueError:
                continue
            if target_matches(det.pane, other_target):
                occupied = True
                break
        if not occupied:
            free.append(det)
    return free


def _all_panes() -> list:
    """Every live tmux pane with agent detection (mapped or not)."""
    from actl.core.discovery import detect_pane
    from actl.core.tmux import list_panes

    # Do not turn SSH/tmux transport failures into an empty board. The GUI
    # needs the exact error so a broken remote relay is not mistaken for zero panes.
    panes = list_panes()
    return [(pane, detect_pane(pane)) for pane in panes]


def _pane_busy(pane_id: str) -> str:
    """보수적 활동 상태: CPU와 pane tail이 모두 불충분하면 미확인."""
    try:
        from actl.core.activity import observe_activity

        state, _ = observe_activity(pane_id)
        return {"RUNNING": "실행중", "IDLE": "유휴", "WAITING_INPUT": "승인 기다림", "UNKNOWN": "미확인"}[state]
    except Exception:
        return "미확인"


def _pane_result_flag(agent: str | None, target: str, config: dict) -> str:
    """결과도착: 추출 가능하면 ●, 없으면 ○, 미매핑은 -."""
    if not agent:
        return "-"
    try:
        result = extract_last_response(agent, target, config)
    except Exception:
        return "?"
    return "●" if result.text else "○"


def _pane_board(config: dict) -> str:
    """실시간 상태 보드: 모든 pane + 유휴/실행중 + 결과도착 + 매핑 상태."""
    from actl.core.discovery import mapping_state

    lines = ["--- 전체 live pane (번호키=미리보기+즉시매핑) ---"]
    board: list[tuple[str, object, object]] = []
    for index, (pane, det) in enumerate(_all_panes(), 1):
        agent = det.agent or "-"
        try:
            state = mapping_state(config, det)
        except Exception:
            state = "-"
        busy = _pane_busy(pane.pane_id)
        flag = _pane_result_flag(det.agent, pane.pane_id, config)
        state_ko = STATE_KO.get(state, state)
        key = str(index)
        lines.append(
            f"  [{key}] {pane.pane_id:<5} {pane.current_command:<12} {agent:<12} "
            f"{busy:<6} 결과{flag} {state_ko:<8} {pane.current_path}"
        )
        board.append((key, pane, det))
    lines.append("번호키: 미리보기 + 그 pane로 즉시 매핑 (OpenCode 서버 제외, 세션은 별도 확인)")
    lines.append("●=결과 있음(복사 가능) ○=결과 없음(아직 응답 전) 활동=CPU+pane tail, 미확인=증거 부족")
    _pane_board_cache(config, board)
    return "\n".join(lines)


_BOARD: list = []


def _event_scope(events: list[str]) -> tuple[bool, set[str]]:
    """Classify remote tmux notifications without turning output into a scan."""
    topology = any(
        event.startswith(("%sessions-changed", "%window-", "%layout-change", "%session-"))
        for event in events
    )
    pane_events = ("%output", "%pane-mode-changed", "%pause", "%continue")
    pane_ids = {
        parts[1]
        for event in events
        if event.startswith(pane_events)
        for parts in [event.split(maxsplit=2)]
        if len(parts) > 1 and parts[1].startswith("%")
    }
    return topology, pane_ids


def _pane_board_cache(config: dict, board: list | None = None) -> list:
    global _BOARD
    if board is not None:
        _BOARD = board
    return _BOARD


class _Cbreak:
    """cbreak 래퍼: POSIX는 termios, Windows는 msvcrt 폴백."""

    def __init__(self, fd: int) -> None:
        import sys as _sys

        self.fd = fd
        self.old = None
        self.windows = _sys.platform == "win32"

    def __enter__(self) -> "_Cbreak":
        if not self.windows:
            import termios
            import tty

            self.old = termios.tcgetattr(self.fd)
            tty.setcbreak(self.fd)
        return self

    def __exit__(self, *exc: object) -> None:
        if not self.windows and self.old is not None:
            import termios

            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.old)

    def restore(self) -> None:
        if not self.windows and self.old is not None:
            import termios

            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.old)

    def raw(self) -> None:
        if not self.windows and self.old is not None:
            import tty

            tty.setcbreak(self.fd)

    def read_key(self, timeout: float | None = None) -> str | None:
        import os
        import time
        import sys as _sys

        if self.windows:
            import msvcrt

            sys.stdout.flush()
            deadline = None if timeout is None else time.monotonic() + timeout
            while True:
                if not msvcrt.kbhit():
                    if deadline is not None and time.monotonic() >= deadline:
                        return None
                    time.sleep(0.05)
                    continue
                try:
                    ch = msvcrt.getwch()
                except OSError:
                    import time as _time

                    _time.sleep(0.05)
                    continue
                if ch in {"\x00", "\xe0"}:
                    try:
                        msvcrt.getwch()
                    except OSError:
                        pass
                    continue
                return "\r" if ch == "\r" else ch
        if timeout is not None:
            import select

            ready, _, _ = select.select([self.fd], [], [], timeout)
            if not ready:
                return None
        raw = os.read(self.fd, 1).decode("utf-8", "replace")
        if raw == "\r":
            return "\r"
        return raw

    def read_line_cooked(self) -> str:
        self.restore()
        try:
            return sys.stdin.readline()
        finally:
            self.raw()


def run_tui() -> int:
    import sys as _sys

    config = load_config()
    try:
        from actl.cli import _auto_reconcile

        config = _auto_reconcile(config)
    except Exception:
        pass
    rows = _rows(config)
    selected = 0
    message = ""
    if not _sys.stdin.isatty():
        _render(rows, selected, "stdin이 터미널이 아님 — 출력 전용 모드")
        return 2
    _render(rows, selected)
    try:
        fd = sys.stdin.fileno()
    except Exception:
        print("TUI는 터미널에서 실행하세요: actl tui")
        return 2
    with _Cbreak(fd) as cb:
        import time

        next_refresh = time.monotonic() + 12.0
        next_health = time.monotonic() + 60.0
        next_event_refresh = 0.0
        pending_event_panes: set[str] = set()
        while True:
            ch = cb.read_key(timeout=0.5)
            if ch is None:
                from actl.core.tmux import REMOTE_SSH_TARGET, remote_events

                event_mode = bool(REMOTE_SSH_TARGET)
                now = time.monotonic()
                events = remote_events() if event_mode else []
                topology, pane_ids = _event_scope(events)
                pending_event_panes.update(pane_ids)
                local_due = pending_event_panes and now >= next_event_refresh
                due = now >= (next_health if event_mode else next_refresh)
                if topology or due:
                    config = load_config()
                    try:
                        from actl.cli import _auto_reconcile

                        config = _auto_reconcile(config, announce=False)
                    except Exception:
                        pass
                    rows = _rows(config)
                    interval = 3.0 if any(r.get("activity_state") == "RUNNING" for r in rows) else 12.0
                    next_refresh = time.monotonic() + interval
                    next_health = time.monotonic() + 60.0
                    pending_event_panes.clear()
                    _render(rows, selected, "자동 새로고침 완료")
                elif local_due:
                    target = rows[selected].get("target") if rows else None
                    if target in pending_event_panes:
                        message = f"자동 pane 갱신\n{_pane_preview(target)}"
                        _render(rows, selected, message)
                    pending_event_panes.clear()
                    next_event_refresh = now + 1.0
                continue
            if ch in {"q", "\x03"}:
                sys.stdout.write("\n")
                return 0
            if ch in {"h", "?"}:
                sys.stdout.write(CLEAR + HELP_TEXT)
                sys.stdout.flush()
                cb.read_line_cooked()
                _render(rows, selected, message)
                continue
            board = _pane_board_cache(config)
            hit = next((entry for entry in board if entry[0] == ch), None)
            if hit is not None:
                _, pane, det = hit
                if det.agent:
                    try:
                        updated = manual_map(config, det.agent, det)
                    except ValueError as exc:
                        message = f"✗ {pane.pane_id} 매핑 실패: {exc}\n{_pane_preview(pane.pane_id)}"
                    else:
                        backup = backup_config()
                        save_config(updated)
                        config = updated
                        rows = _rows(config)
                        message = (
                            f"✓ {pane.pane_id} → {det.agent} 매핑됨\n"
                            f"{_verify_row(det.agent, pane.pane_id, config)}\n"
                            f"{_pane_preview(pane.pane_id)}"
                        )
                else:
                    message = (
                        f"--- {pane.pane_id} ({pane.current_command}) 미리보기 — "
                        f"에이전트 미감지 ---\n{_pane_preview(pane.pane_id)}"
                    )
                _render(rows, selected, message)
                continue
            if ch.isdigit():
                idx = int(ch) - 1
                if 0 <= idx < len(rows):
                    selected = idx
                    row = rows[selected]
                    tgt = row["target"]
                    if tgt != "-" and not tgt.endswith("?"):
                        message = (
                            f"--- {row['display']} {tgt} live pane ---\n"
                            f"{_verify_row(row['agent'], tgt, config)}\n"
                            f"{_pane_preview(tgt)}"
                        )
                    else:
                        state_ko = STATE_KO.get(row["state"], row["state"])
                        message = (
                            f"{row['display']}: live pane 없음 ({state_ko}). "
                            "m 눌러 pane 선택, r 눌러 새로고침."
                        )
                    _render(rows, selected, message)
                continue
            if ch == "r":
                config = load_config()
                try:
                    from actl.cli import _auto_reconcile

                    config = _auto_reconcile(config)
                except Exception:
                    pass
                rows = _rows(config)
                message = "새로고침 완료"
                _render(rows, selected, message)
                continue
            if ch == "v":
                message = _pane_board(config)
                _render(rows, selected, message)
                continue
            if ch == "V":
                message = _pane_board(config) + "\n복사 자동검증:"
                for row in rows:
                    tgt = row["target"]
                    if tgt == "-" or tgt.endswith("?"):
                        message += f"\n  {row['display']}: 미매핑"
                        continue
                    try:
                        result = extract_last_response(row["agent"], tgt, config)
                    except Exception as exc:
                        message += f"\n  {row['display']}: 추출 실패 {exc}"
                        continue
                    if result.text:
                        message += f"\n  {row['display']}: 복사 가능 ({len(result.text)}자)"
                    else:
                        message += f"\n  {row['display']}: 복사 불가 ({result.detail or '응답 없음'})"
                _render(rows, selected, message)
                continue
            if ch == "m":
                row = rows[selected]
                cb.restore()
                try:
                    sys.stdout.write(f"\n{row['display']} live pane 목록 (0 = 취소):\n")
                    candidates = _unmapped_panes(config, row["agent"])
                    if not candidates:
                        message = f"빈 {row['display']} live pane 없음"
                    else:
                        for i, det in enumerate(candidates, 1):
                            sys.stdout.write(
                                f"  [{i}] {det.pane.pane_id} {det.pane.current_path} — {det.evidence}\n"
                            )
                        sys.stdout.write("pane 선택 (번호 또는 %ID): ")
                        sys.stdout.flush()
                        choice = sys.stdin.readline().strip()
                        if choice == "0" or not choice:
                            message = "재매핑 취소"
                        else:
                            det = None
                            if choice.startswith("%"):
                                det = next((d for d in candidates if d.pane.pane_id == choice), None)
                            else:
                                try:
                                    det = candidates[int(choice) - 1]
                                except (ValueError, IndexError):
                                    det = None
                            if det is None:
                                message = "✗ 잘못된 pane 선택"
                            else:
                                try:
                                    updated = manual_map(config, row["agent"], det)
                                except ValueError as exc:
                                    message = f"✗ {exc}"
                                else:
                                    backup = backup_config()
                                    save_config(updated)
                                    config = updated
                                    rows = _rows(config)
                                    message = (
                                        f"✓ {row['display']} → {det.pane.pane_id} 매핑됨 "
                                        f"(백업 {backup.name})"
                                    )
                finally:
                    cb.raw()
                _render(rows, selected, message)
                continue
            if ch == "s":
                row = rows[selected]
                tgt = row["target"]
                if tgt == "-" or not row.get("control_ready"):
                    message = f"✗ {row['display']} 제어 비활성 (validated mapping 필요)"
                    _render(rows, selected, message)
                    continue
                cb.restore()
                send_message = ""
                try:
                    sys.stdout.write(f"\n{row['display']} ({tgt})에 보낼 메시지, '::send' 줄로 종료:\n")
                    sys.stdout.flush()
                    lines: list[str] = []
                    cancelled = False
                    while True:
                        line = sys.stdin.readline()
                        if not line:
                            send_message = "✗ 전송 취소 (EOF)"
                            cancelled = True
                            break
                        line = line.rstrip("\n")
                        if line == "::send":
                            break
                        if line == "::cancel":
                            send_message = "✗ 전송 취소"
                            cancelled = True
                            break
                        lines.append(line)
                    if not cancelled:
                        prompt = "\n".join(lines)
                        if not prompt:
                            send_message = "✗ 빈 메시지"
                        else:
                            from actl.core.activity import send_blocked_reason
                            from actl.cli import _send_to_selected

                            reason = send_blocked_reason(tgt)
                            if reason:
                                send_message = f"✗ {reason}"
                            else:
                                try:
                                    _send_to_selected(config, row["agent"], prompt)
                                    send_message = f"✓ {row['display']}에 전송됨"
                                except Exception as exc:
                                    send_message = f"✗ {exc}"
                finally:
                    cb.raw()
                message = send_message
                _render(rows, selected, message)
                continue
            if ch in {"c", "p"}:
                row = rows[selected]
                tgt = row["target"]
                if tgt == "-" or not row.get("control_ready"):
                    message = f"✗ {row['display']} 복사 비활성 (validated mapping 필요)"
                    _render(rows, selected, message)
                    continue
                try:
                    result = extract_last_response(row["agent"], tgt, config)
                except Exception as exc:
                    message = f"✗ {exc}"
                    _render(rows, selected, message)
                    continue
                if not result.text:
                    message = f"✗ 응답 텍스트 없음 ({result.detail or '비어 있음'})"
                    _render(rows, selected, message)
                    continue
                if ch == "p":
                    message = f"--- {row['display']} 마지막 응답 ---\n{result.text[:2000]}"
                    _render(rows, selected, message)
                    continue
                try:
                    backend = copy_text(result.text, preferred=config.get("clipboard_backend", "auto"))
                    message = f"✓ {row['display']} 복사됨 ({backend}, {len(result.text)}자)"
                except Exception as exc:
                    message = (
                        f"✗ 클립보드 실패: {exc}\n"
                        f"--- {row['display']} 마지막 응답 (수동 복사) ---\n{result.text[:2000]}"
                    )
                _render(rows, selected, message)
