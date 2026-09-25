# JuActl 백로그와 현재 상태

## JuActl은 무엇인가

JuActl은 ASUS의 tmux에서 실행 중인 AI 에이전트를 MainPC에서 조작하는
리모컨이다. 에이전트에 프롬프트를 보내고, 결과를 확인하고, 필요한 결과를
클립보드로 복사하는 흐름을 하나의 보드와 CLI로 제공한다.

## 지금 되는 것

- **보내기 → 결과 → 복사**: 에이전트에 입력을 보내고 완료된 결과를 추출해
  MainPC 클립보드 또는 수동 출력으로 전달한다.
- **보드**: JuActl Board에서 매핑, 활동 상태, 결과 준비 여부와 pane을
  확인한다. 웹 보드와 TUI 보드 모두 한국어 상태 라벨을 사용한다.
- **동시 전송 잠금**: 같은 pane에 대한 동시 전송을 막는 writer guard와
  단일 in-flight 전송 경로가 구현되어 있으며, 전체 운영 시나리오 검증은
  진행 중이다.

## 열린 항목과 현재 상태

근거: [ROOT_CAUSE_REPORT.md](ROOT_CAUSE_REPORT.md)

| 항목 | 현재 상태 | 다음 확인 |
|---|---|---|
| DA / escape 응답 leak | **조사 중** — 제한된 harness에서 재현되지 않았고, 실제 Windows Terminal 대화형 세션 검증은 완료되지 않음 | 실제 Windows Terminal에서 DA 질의/응답을 포함한 독립 재현 수행 |
| SSH 세션 disconnect 원인 | **조사 중** — SSH churn과 tmux topology 변화는 관찰됐지만 직접 인과관계는 입증되지 않음 | 기존 MainPC 클라이언트를 중지하고 30분 독립 A/B 공존 검증 수행 |
| Windows pane discovery 인자 손실 | **원인 확인, 수정 구현됨** — `list-panes -F` 인자가 깨지는 transport 결함 | 새 Windows 빌드/설치본에서 `discover --ssh asus` 재검증 |
| 원격 polling fan-out | **증폭 요인 확인** — 측정된 refresh 1회당 원격 작업 19개 | 독립 QA에서 warm transport, process 수와 refresh 동작 측정 |
| 동시 전송 잠금의 운영 검증 | **진행 중** — 코드 경로와 단위 검증은 있으나 전체 보드 시나리오 증거가 부족함 | 다중 전송 시나리오에서 중복 입력·결과·해제 순서 확인 |

## 다음 단계

1. MainPC의 기존 JuActl 클라이언트를 중지하고 새 빌드로 설치한다.
2. `discover`, 보드 표시, 보내기→결과→복사 흐름을 독립 QA로 확인한다.
3. 30분 SSH/tmux 공존 검증으로 disconnect 원인을 다시 조사한다.
4. DA leak와 동시 전송 잠금은 재현 또는 독립 증거가 생길 때까지 완료로
   표시하지 않는다.
