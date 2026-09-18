# JuActl — MainPC 에이전트 무전기

asus tmux에서 돌고 있는 AI 에이전트 8종을 MainPC에서 버튼으로 조종.
매핑·복사·전송 전부 자동. 터미널 명령어 외울 필요 없음.

지원: Claude Team, Claude Pro, OpenCode, Codex, Cursor, CommandCode, Cline, Grok.

## MainPC에서 실행 (프로그램)

```powershell
git clone https://github.com/ju0o/JuActl.git juactl
```
```powershell
cd juactl
```
```powershell
.\scripts\install.ps1
```
```powershell
.\scripts\build-exe.ps1
```

`dist\JuActlBoard.exe` 더블클릭. 바탕화면 바로가기도 자동 생성.
빌드 스크립트는 현재 Python 환경의 PyInstaller만 사용하며 전역 패키지를 무조건 업그레이드하지 않는다.
빌드 후 `dist\SHA256SUMS.txt`에 exe의 SHA-256이 기록된다. GitHub Actions는 Linux 테스트와 Windows exe 빌드를 별도로 검증한다.

릴리즈 전 QA:

```bash
./scripts/qa.sh
```

Windows에서는 `powershell -ExecutionPolicy Bypass -File scripts\qa.ps1`를 실행한다.
빌드 전에는 바로가기가 pythonw 폴백으로 동작.

## MainPC에서 실행 (웹 보드)

asus에서 서버 기동 (loopback 기본, SSH tunnel 권장):

```bash
actl serve 8765 --host 0.0.0.0 --token "붙여넣을-강한-토큰"
```

loopback 밖으로 열면 Bearer 토큰이 필수다. 토큰을 생략하면 일회성 토큰을
생성해 터미널에 한 번 출력한다. 보안을 우선하면 기본값 `127.0.0.1`과 SSH
port-forward를 사용한다.

MainPC 브라우저:

```
http://100.82.108.31:8765/
```

5초 자동 폴링, 카드 클릭=미리보기, pane 행 클릭=즉시 매핑,
복사는 브라우저 클립보드 직행.

## MainPC에서 실행 (CLI 원격)

```powershell
actl tui --ssh asus
actl copy grok --print --ssh asus
actl discover --ssh asus
actl doctor
```

## TUI 보드 키 (asus 로컬 / ssh 터미널)

5초 자동 새로고침, 활동 상태는 CPU와 pane tail을 함께 확인하며 불충분하면 `미확인`,
숫자=선택+미리보기, `c`=복사(실패시 자동출력), `p`=출력,
`m`=재매핑, `s`=전송, `v`=pane보드, `V`=복사검증,
`h`=도움말, `r`=새로고침, `q`=종료.

## 자동 매핑 규칙

- pane 1개 → 자동 매핑.
- 같은 에이전트 pane 여러 개 → 가장 최근 시작 프로세스 자동 선택.
  기존 live 매핑은 유지. 동점/판독불가만 직접 질문.
- OpenCode `session_id` 자동 바인딩 (별도 `actl bind` 불필요).
- pane이 죽거나 바뀌면 stale 제거 후 위 규칙으로 재매핑.

## CLI 명령

- `actl` — REPL (`/help` 전체 명령)
- `actl copy AGENT [--print]` — 마지막 응답 복사/출력
- `actl extract AGENT [PANE]` — 기계 파이프 (stdout 텍스트만)
- `actl send AGENT` — stdin 프롬프트 전송 (원격 위임용)
- `actl map AGENT` — 번호 또는 `%ID` 직접 입력 (예: `%69`)
- `actl push FILE [--print]` — 현 SSH 세션 경유 base64 전송
- `actl discover [--apply]` / `actl status [--json]` / `actl doctor [--json]` / `actl audit [N]` / `actl history [AGENT] [N]`
- `actl gui [--ssh T]` / `actl tui` / `actl serve [port]`
- 별칭: `claude-team`/`ct`, `claude-pro`/`cp`, `opencode`/`oc`,
  `codex`/`cx`, `cursor`/`cu`, `commandcode`/`cmd`, `cline`/`cl`, `grok`/`gr`.

## 클립보드

- MainPC 네이티브: PowerShell `Set-Clipboard` 직행.
- SSH: OSC52 (Windows Terminal 허용, 차단 시 `p` 출력 후 수동 복사).
- 로컬 리눅스: `wl-copy` → `xclip` → `xsel`.

## 감사 로그

복사·전송·매핑·해제 동작은 응답 본문 없이 시각, 대상, 결과 길이, 상관관계와
짧은 결과 hash만 로컬 JSONL에 기록한다. 기본 경로는
`~/.local/state/actl/audit.jsonl`이며 `ACTL_AUDIT_PATH`로 바꿀 수 있다.
감사 로그는 prompt/response 본문과 절대 storage 경로를 기록하지 않으며, Unix에서는 디렉터리 `0700`, 파일 `0600`으로 생성된다.

```bash
actl audit 50
actl history commandcode 20
```

## 문제 해결

| 증상 | 조치 |
|---|---|
| 매핑 stale | 보드 `r` 또는 `actl discover --apply` |
| OpenCode bind 요구 | 구버전 → `git pull` (신버전 자동 바인딩) |
| 클립보드 안 붙음 | `p` 출력 후 수동 복사, OSC52 허용 확인 |
| Windows `termios` 에러 | 구버전 → `git pull` (lazy import 패치됨) |
| GUI cmd 팝업 | 구버전 → `git pull` (`CREATE_NO_WINDOW` 패치됨) |

자세한 MainPC↔asus 절차는 [MAINPC_SETUP.md](MAINPC_SETUP.md).
