# JuActl MainPC 로드맵 (MainPC ↔ asus SSH)

MainPC(Windows)에서 asus 리눅스 박스의 tmux 에이전트들을
버튼식으로 조종하는 절차. 복사-붙여넣기로 그대로 실행 가능.

## 0. 환경 전제

| 항목 | 값 |
|---|---|
| MainPC → asus | `ssh asus` (이미 사용 중) |
| asus tmux 소켓 | `/tmp/tmux-1000/default` |
| JuActl 리포 | `https://github.com/ju0o/JuActl.git` |
| asus 로컬 경로 | `/home/skkse12/Desktop/Projects/Core/actl-v0.1.1-managed` |

## 1. MainPC에서 다운로드 + 실행 (최초 1회)

```powershell
# 1) Windows Terminal에서 asus 접속
ssh asus

# 2) asus에서 JuActl 받기 (이미 있으면 git pull만)
cd ~/Desktop/Projects/Core
git clone https://github.com/ju0o/JuActl.git juactl-mainpc 2>/dev/null || (cd juactl-mainpc && git pull)
cd juactl-mainpc 2>/dev/null || cd actl-v0.1.1-managed

# 3) 설치 (PATH 등록 + 기본 config 생성)
./scripts/install.sh

# 4) 설치 확인
actl help
```

기대 출력:

```text
actl — Agent Control CLI

  actl                REPL (Agent > prompt, /help for commands)
  actl tui            Agent board: number=select+preview, ...
  ...
```

## 2. TUI 보드 실행 (매일 쓰는 진입점)

```bash
ssh asus
actl tui
```

## 3. 보드 조작법 (버튼식)

| 키 | 동작 |
|---|---|
| `1-8` | 에이전트 선택 + live pane 미리보기 (최근 12줄) |
| `c` | 마지막 응답 복사 (SSH=OSC52, 로컬=wl-copy/xclip/xsel) |
| `p` | 마지막 응답 화면 출력 (클립보드 막히면 수동 복사) |
| `m` | 시각 remap — 빈 live pane 목록 → 번호/`%ID` 선택 → 즉시 매핑+저장 |
| `s` | 메시지 전송 — 여러 줄 입력 → `::send` 줄로 종료 → 해당 pane 전송 |
| `h` 또는 `?` | 도움말 + 첫실행 튜토리얼 오버레이 |
| `r` | 새로고침 (stale 제거 + 자동 매핑) |
| `q` | 종료 |

## 4. 첫실행 튜토리얼 (보드 안에서 `h`)

1. tmux pane에서 에이전트 실행 (`grok`, `opencode`, `claude`, `codex` …).
2. `actl tui` 실행.
3. `1-8` 눌러 에이전트 선택 — live pane 미리보기 확인.
   잘못된 pane이면 `m` → 목록에서 올바른 pane 선택.
4. `c` 복사 (안 되면 `p` 출력 후 수동 복사).
5. `s` 메시지 전송 (`::send` 로 종료).
6. 에이전트 켜고 끈 뒤엔 `r` 새로고침. `q` 종료.

## 5. 자동 매핑 규칙 (기억할 것)

- pane 1개 → 자동 매핑.
- 같은 에이전트 pane 여러 개 → **가장 최근 시작 프로세스** 자동 선택.
  기존 live 매핑은 유지. 동점/판독불가만 inline 질문.
- OpenCode `session_id` 자동 바인딩 (별도 `actl bind` 불필요).
- pane이 죽거나 바뀌면 stale 제거 후 위 규칙으로 재매핑.

## 6. 클립보드 주의 (SSH)

- SSH 경로는 OSC52 시퀀스로 MainPC 클립보드 전달.
- Windows Terminal: OSC52 허용 → `c` 그대로 사용.
- 차단된 SSH 클라이언트 → `p` 출력 후 드래그 수동 복사.
- 완전 로컬(asus 직결) → `wl-copy` → `xclip` → `xsel` 순.

## 7. CLI 바로가기 (보드 없이)

```bash
actl copy grok --print     # Grok 마지막 응답 출력
actl copy opencode         # OpenCode 복사
actl map grok              # Grok pane 고르기 (번호 또는 %69)
actl discover              # 전체 pane + 감지 상태 읽기전용 표시
actl status                # 매핑 + liveness 표
actl help                  # 전체 명령 요약
```

## 8. 문제 해결

| 증상 | 조치 |
|---|---|
| `Multiple live … panes` 예전 메시지 | 구버전 실행 중 → `git pull` + 재설치 |
| 매핑이 stale | 보드 `r` 또는 `actl discover --apply` |
| OpenCode `/copy` bind 요구 | 구버전 → `git pull`; 신버전은 자동 바인딩 |
| 클립보드 안 붙음 | `p` 로 출력 후 수동 복사; 터미널 OSC52 허용 확인 |
| 보드 깨짐 | `q` 후 터미널 크기 늘리고 `actl tui` 재실행 |

## 9. 업데이트 절차

```bash
ssh asus
cd ~/Desktop/Projects/Core/actl-v0.1.1-managed  # 또는 juactl-mainpc
git pull
./scripts/install.sh
actl help
```
