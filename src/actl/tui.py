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

from actl.agents.extract import extract_last_response
from actl.core.config import backup_config, get_target, load_config, save_config
from actl.core.discovery import STRONG_CONFIDENCE, discover, manual_map, mapping_state
from actl.core.registry import AGENTS
from actl.core.tmux import capture_pane
from actl.core.validation import validate_target
from actl.utils.clipboard import copy_text

CLEAR = "\x1b[2J\x1b[H"
BOLD = "\x1b[1m"
DIM = "\x1b[2m"
RESET = "\x1b[0m"


def _rows(config: dict) -> list[dict]:
    """One row per agent: live target, status, and last response preview."""
    detections = {d.agent: d for d in discover() if d.agent}
    rows: list[dict] = []
    for idx, name in enumerate(AGENTS, 1):
        spec = AGENTS[name]
        target = "-"
        state = "UNMAPPED"
        try:
            target = get_target(config, name).target
            state = validate_target(name, target).state
        except ValueError:
            det = detections.get(name)
            if det and det.confidence in STRONG_CONFIDENCE:
                target = f"{det.pane.pane_id}?"
                state = "DETECTED"
        preview = ""
        detail = ""
        if target != "-" and not target.endswith("?"):
            try:
                result = extract_last_response(name, target, config)
                if result.text:
                    first = result.text.strip().splitlines()[0] if result.text.strip() else ""
                    preview = first[:100]
                else:
                    detail = result.detail or "no text"
            except Exception as exc:
                detail = str(exc)[:80]
        rows.append(
            {
                "key": str(idx),
                "agent": name,
                "display": spec.display_name,
                "target": target,
                "state": state,
                "preview": preview,
                "detail": detail,
            }
        )
    return rows


STATE_KO = {"UP": "정상", "DOWN": "꺼짐", "MISMATCH": "불일치", "UNMAPPED": "미매핑", "DETECTED": "감지됨"}

HELP_TEXT = """\
actl 에이전트 보드 — 도움말

  1-8     에이전트 선택 + live pane 전체 미리보기 (터미널 크기에 맞춤)
          + 매핑/복사 상태 검증행 (정상·복사 가능/불가 즉시 표시)
  c       선택한 에이전트의 마지막 응답 복사 (SSH=OSC52, 로컬=wl-copy/xclip/xsel)
  p       마지막 응답 화면 출력 (클립보드 막히면 수동 복사)
  m       재매핑: 이 에이전트의 빈 live pane 목록 표시, 번호 또는 %ID 선택
          (예: %69). OpenCode 세션은 재매핑 시 자동 바인딩.
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
  - OpenCode는 별도 bind 불필요: 매핑 시 세션 자동 바인딩.
아무 키나 눌러 돌아가기.
"""


def _render(rows: list[dict], selected: int, message: str = "") -> None:
    sys.stdout.write(CLEAR)
    sys.stdout.write(
        f"{BOLD}actl — 에이전트 보드{DIM}  (숫자=선택+미리보기, c=복사, p=출력, "
        f"m=재매핑, s=전송, v=pane보드, h=도움말, r=새로고침, q=종료){RESET}\n\n"
    )
    for i, row in enumerate(rows):
        marker = ">" if i == selected else " "
        state_color = "" if row["state"] == "UP" else DIM
        state_ko = STATE_KO.get(row["state"], row["state"])
        sys.stdout.write(
            f"{marker} [{row['key']}] {state_color}{row['display']:<12} {row['target']:<6} {state_ko:<9}{RESET} {row['preview'] or row['detail']}\n"
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


def _pane_preview(target: str, lines: int = 0) -> str:
    """Full-terminal pane preview: wrap to width, fill available height.

    lines=0 (default) auto-sizes to the terminal minus board chrome so the
    preview reads like the real pane, not a 12-line snippet.
    """
    cols, rows = _term_size()
    if lines <= 0:
        lines = max(10, rows - 16)
    width = max(40, cols - 4)
    try:
        text = capture_pane(target, history=max(200, lines * 3))
    except Exception as exc:
        return f"(미리보기 불가: {exc})"
    import textwrap

    out: list[str] = []
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        if len(line) <= width:
            out.append(line)
        else:
            out.extend(textwrap.wrap(line, width=width, replace_whitespace=False, drop_whitespace=False))
    kept = out[-lines:] if out else ["(빈 pane)"]
    return "\n".join(kept)


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

    try:
        panes = list_panes()
    except Exception:
        return []
    return [(pane, detect_pane(pane)) for pane in panes]


def _pane_busy(pane_id: str) -> str:
    """유휴/실행중: pane 프로세스 그룹 CPU 합으로 판정. --ssh 원격도 지원."""
    try:
        import subprocess

        from actl.core.tmux import _remote_args, pane_field

        pane_pid = int(pane_field(pane_id, "#{pane_pid}"))
        proc = subprocess.run(
            _remote_args(["ps", "-o", "%cpu=", "-g", str(pane_pid)]),
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=5,
        )
        total = sum(float(x) for x in proc.stdout.split() if x.strip())
        return "실행중" if total > 5.0 else "유휴"
    except Exception:
        return "?"


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
    for pane, det in _all_panes():
        agent = det.agent or "-"
        try:
            state = mapping_state(config, det)
        except Exception:
            state = "-"
        busy = _pane_busy(pane.pane_id)
        flag = _pane_result_flag(det.agent, pane.pane_id, config)
        state_ko = STATE_KO.get(state, state)
        key = pane.pane_id.lstrip("%")
        lines.append(
            f"  [{key}] {pane.pane_id:<5} {pane.current_command:<12} {agent:<12} "
            f"{busy:<6} 결과{flag} {state_ko:<8} {pane.current_path}"
        )
        board.append((key, pane, det))
    lines.append("번호키: 미리보기 + 그 pane로 즉시 매핑 (OpenCode 세션 자동바인딩)")
    lines.append("●=결과 있음(복사 가능) ○=결과 없음(아직 응답 전) 유휴/실행중=CPU 기준")
    _pane_board_cache(config, board)
    return "\n".join(lines)


_BOARD: list = []


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

    def read_key(self) -> str:
        import os
        import sys as _sys

        if self.windows:
            import msvcrt

            while True:
                ch = msvcrt.getwch()
                if ch in {"\x00", "\xe0"}:
                    msvcrt.getwch()
                    continue
                return "\r" if ch == "\r" else ch
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
    _render(rows, selected)
    if not _sys.stdin.isatty():
        print("TUI는 터미널에서 실행하세요: actl tui")
        return 2
    fd = sys.stdin.fileno()
    with _Cbreak(fd) as cb:
        while True:
            ch = cb.read_key()
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
                if tgt == "-" or tgt.endswith("?"):
                    message = f"✗ {row['display']} live pane 없음 (먼저 m 눌러 매핑)"
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
                            from actl.cli import _send_to_selected

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
                if tgt == "-" or tgt.endswith("?"):
                    message = f"✗ {row['display']} live pane 없음"
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
