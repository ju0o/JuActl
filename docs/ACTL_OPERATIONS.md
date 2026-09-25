# actl 운영 참고

README의 빠른 시작에서 이어지는 운영 세부사항입니다.

## 외부 웹 보드 인증

`actl serve`의 기본 호스트는 `127.0.0.1`이므로 로컬 브라우저에서 토큰 없이
사용합니다. 다른 기기에서 보려면 `--host`로 loopback 밖에 바인딩해야 합니다.

```bash
actl serve --host 0.0.0.0
```

이때 `--token TOKEN`을 주거나 `ACTL_WEB_TOKEN` 환경 변수를 설정할 수 있습니다.
둘 다 없으면 서버가 일회성 토큰을 만들어 `ACTL_WEB_TOKEN=...`으로 출력하고,
토큰이 포함된 접속 주소도 출력합니다.

브라우저는 첫 접속 때 주소의 `?token=...`을 사용합니다. 보드는 이를 세션에
보관한 뒤 API 요청마다 다음 헤더를 보냅니다.

```text
Authorization: Bearer <토큰>
```

토큰이 없는 외부 요청은 거부됩니다. 토큰을 주소나 로그에 공개하지 말고,
공유가 끝나면 서버를 종료하거나 새 토큰으로 다시 실행하세요.

## 감사 로그

`actl audit`는 프롬프트와 응답 본문을 기록하지 않습니다. 전송·복사 같은 작업의
시각, 대상, 성공 여부, 출처, 길이와 결과 해시 등 메타데이터만 JSONL로 남깁니다.

- 기본 경로: `~/.local/state/actl/audit.jsonl`
- 변경: `ACTL_AUDIT_PATH=/path/to/audit.jsonl`
- 디렉터리 권한: `0700`
- 로그 파일 권한: `0600`
- 기본 최대 크기: 5 MiB; 초과 시 `.1`로 회전

```bash
actl audit 50
actl history codex 50
```

감사 기록을 쓸 수 없어도 안전한 전송·복사 작업 자체는 중단하지 않습니다.

## 릴리즈 전 검사

Linux에서는 다음이 컴파일, 회귀 테스트, doctor JSON 계약, 공백 검사를 실행하고
마지막에 `QA_COMPLETE`를 출력합니다.

```bash
bash scripts/qa.sh
```

Windows에서는 다음이 소스 컴파일, 패키지 빌드, SHA-256 목록, doctor JSON,
패키지 smoke를 실행하고 마지막에 `PACKAGE_QA_COMPLETE`를 출력합니다.

```powershell
powershell -ExecutionPolicy Bypass -File scripts/qa.ps1
```

실제 외부 tmux나 Windows 원격 운영 증거의 범위는
[docs/TESTER.md](TESTER.md)를 참고하세요.
