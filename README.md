# JuActl

## 한 줄 소개

`actl`은 여러 AI 창을 한 번에 살펴보고, 지시를 보내고, 답을 복사하는 리모컨입니다.
여러 AI 비서를 번갈아 사용하는 비개발자를 위한 도구입니다.

## 지금 되는 것

- **보드:** 실행 중인 AI 창, 연결 상태, 작업 상태, 결과 준비 여부를 한 화면에서 봅니다.
- **보내기:** 선택한 AI 창에 여러 줄 지시를 보냅니다. CLI, TUI, 웹 보드에서 쓸 수 있습니다.
- **결과 복사:** 마지막 답을 클립보드로 복사하거나 `--print`로 화면에 그대로 출력합니다.
- **창 연결:** `discover`가 tmux 창에서 AI를 찾아 보여 주고, 확실한 매핑만 `--apply`로 저장합니다.
- **동시 전송 보호:** 창마다 전송 잠금이 있어 두 전송자가 같은 창에 글을 섞어 보내지 못합니다.
- **여러 실행 방식:** Claude Team, Claude Pro, OpenCode, Codex, Cursor, CommandCode, Cline, Grok을 지원합니다.

## 빠른 시작

AI 창을 먼저 tmux에서 실행하고, 저장소 루트에서 아래 순서로 실행합니다.

```bash
python3 -m pip install -e .
actl --init
actl doctor
actl discover --apply
actl tui
```

TUI에서 숫자를 눌러 창을 선택하고, `s`로 지시를 보내고, `c`로 마지막 답을 복사합니다.
답을 터미널에 출력하려면 다음처럼 실행합니다.

```bash
actl copy codex --print
```

명령줄에서 바로 보내려면 표준 입력을 사용합니다.

```bash
echo "요청 내용" | actl send codex
```

웹 보드를 쓰려면 다음을 실행한 뒤 표시된 주소를 브라우저에서 엽니다.

```bash
actl serve
```

원격 tmux를 SSH로 조작하는 경우에는 명령에 `--ssh <SSH_TARGET>`을 붙입니다.
예: `actl tui --ssh <SSH_TARGET>`.

주요 명령은 `actl help`에서 확인할 수 있습니다. 상세한 원격 운영 절차는
[MAINPC_SETUP.md](MAINPC_SETUP.md), 테스트와 라이브 검증 경계는
[docs/TESTER.md](docs/TESTER.md)에 정리되어 있습니다.

## 다른 프로그램과의 관계

```text
actl (리모컨)
  → Agent Relay (셋톱박스: PM → Worker → QA)
    → JuControler (허브: JuPlan · JuCeipt · Tester)
```

`actl`은 AI 창을 조작하고 결과를 가져오는 도구입니다. Agent Relay는 승인된
작업을 PM·Worker·QA 흐름으로 실행하며, JuControler는 여러 프로젝트와 도구를
한 곳에서 감독합니다.

## 아직 안 되는 것

- AI 프로그램을 설치하거나 로그인하거나, tmux 창을 대신 만들어 주지는 않습니다.
- 연결된 live tmux 창이 없으면 실제 지시 전송과 결과 수집을 할 수 없습니다.
- SSH 터미널이 OSC52 클립보드를 막으면 자동 붙여넣기 대신 `--print` 수동 복사가 필요합니다.
- Windows 설치와 실제 원격 운영은 이 저장소의 오프라인 테스트만으로 완료되었다고 볼 수 없습니다.
- AI의 답변 품질이나 작업 완료를 JuActl이 보증하지는 않습니다.

## English summary

JuActl's `actl` is a remote control for people who use several AI assistants.
It shows agent panes, sends prompts, and copies the latest result.
Each pane has a send lock so concurrent senders cannot mix input.
It supports CLI, TUI, web board, local tmux, and SSH-routed tmux workflows.
Agent Relay and JuControler sit above it for execution and project supervision.
