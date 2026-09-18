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

## 1. MainPC 로컬 설치 + 원격 보드 (권장: ssh-into-asus 불필요)

MainPC에 actl 한 번 깔고, 이후 `actl tui --ssh asus` 한 방으로
asus tmux를 조종. asus 쪽에 추가 설치 없음 (tmux만 있으면 됨).

```powershell
# 1) MainPC PowerShell 5.1에서 JuActl 받기 (한 줄씩 실행)
git clone https://github.com/ju0o/JuActl.git juactl
```
```powershell
cd juactl
```
```powershell
powershell -ExecutionPolicy Bypass -File scripts/install.ps1
```
```powershell
# 2) 자가진단 (python/tmux/ssh/클립보드/매핑 한 번에)
actl doctor
```

`doctor`는 클립보드에 테스트 문자열을 쓰지 않는 read-only 자가진단이다.
MainPC에서 asus의 실제 pane 매핑까지 확인하려면 `actl doctor --ssh asus`를 사용한다.
TUI는 5초마다 pane 상태와 결과 준비 여부를 자동 갱신한다. 동작 이력은
`actl audit 50`으로 확인할 수 있다.
```powershell
# 3) asus SSH 확인 (이미 ssh asus가 되면 생략)
ssh asus "echo OK"
```
```powershell
# 4) 원격 보드 실행 — MainPC 터미널에서 asus pane 전부 조종
actl tui --ssh asus
```

원격 단일 명령도 가능:

```powershell
actl copy grok --print --ssh asus
actl discover --ssh asus
actl map grok --ssh asus
```

## 1b. 구방식: ssh-into-asus 후 실행 (대안)

```powershell
ssh asus
cd ~/Desktop/Projects/Core
git clone https://github.com/ju0o/JuActl.git juactl-mainpc 2>/dev/null || (cd juactl-mainpc && git pull)
cd juactl-mainpc 2>/dev/null || cd actl-v0.1.1-managed
./scripts/install.sh
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

```powershell
# MainPC 로컬 설치 기준
cd ~/juactl
git pull
./scripts/install.sh
actl help
```

## 10. asus → MainPC 파일 전송

### 10a. `actl push` (권장: MainPC 설정 변경 제로)

현 SSH 세션 경유 base64 전송. MainPC sshd·키 등록 불필요.

```bash
# asus에서
actl push board-wireframe.html
```

```text
ACTL_PUSH_BEGIN board-wireframe.html 7187 a1b2c3d4e5f6
<base64 76자씩 N줄>
ACTL_PUSH_END a1b2c3d4e5f6
```

MainPC PowerShell 수신기 (BEGIN~END 블록을 `push.txt`로 저장 후):

```powershell
$lines = Get-Content push.txt | Where-Object { $_ -notmatch '^ACTL_PUSH_(BEGIN|END)' -and $_.Trim() -ne '' }
$name = ((Get-Content push.txt | Select-Object -First 1) -split ' ')[1]
[IO.File]::WriteAllBytes($name, [Convert]::FromBase64String(($lines -join '')))
```

`actl push FILE --print`는 원본 바이트 그대로 stdout (수동 복사/리다이렉트용).

### 10b. 진짜 역방향 scp (영구 설정, 1회 작업)

asus → MainPC 직접 `scp`. 전제: MainPC OpenSSH Server 실행 중
(확인됨: `OpenSSH_for_Windows_9.5`, 포트 22 OPEN) + asus 공개키 등록.

```powershell
# 1) MainPC PowerShell (관리자)에서 OpenSSH Server 확인/시작
Get-Service sshd
Start-Service sshd
Set-Service -Name sshd -StartupType Automatic

# 2) asus 공개키를 MainPC authorized_keys에 등록 (아래 키 중 택1)
# asus에서 공개키 출력:
#   cat ~/.ssh/id_ed25519_mainpc.pub
# MainPC에서 등록:
$key = "ssh-ed25519 AAAA… asus-to-mainpc-shutdown"  # asus 출력 그대로
$auth = "$env:USERPROFILE\.ssh\authorized_keys"
New-Item -ItemType Directory -Force (Split-Path $auth) | Out-Null
Add-Content $auth $key
icacls (Split-Path $auth) /inheritance:r /grant:r "$($env:USERNAME):(OI)(CI)F" | Out-Null
icacls $auth /inheritance:r /grant:r "$($env:USERNAME):F" | Out-Null
Restart-Service sshd

# 3) asus에서 테스트
scp -i ~/.ssh/id_ed25519_mainpc board-wireframe.html <MainPC-user>@<MainPC-ip>:~/
```

현재 상태: 3개 asus 키 모두 MainPC에서 거부됨 → 위 2)번 미등록이 원인.
등록 후 `actl push` 없이 `scp` 직접 전송 가능.

### 10c. 왜 "Claude Pro는 되고 다른 Agent는 안 된다"처럼 보였나

pane env 전수 비교 결과 Agent별 차이 없음 (전 pane `SSH_CLIENT`·
`SSH_TTY=/dev/pts/0`·`TMUX` 동일). OSC52 클립보드는 **현 SSH 터미널**로
가므로 pane 종류와 무관 — "될 때"는 터미널이 OSC52를 받았고 "안 될 때"는
막힌 것. pane이 아니라 **터미널/시점** 문제. 막히면 `p` 출력 후 수동 복사.
