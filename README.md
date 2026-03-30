# Token Dashboard — Hook Scripts

EO Studio AI Token Dashboard 데이터 수집 스크립트.

각 팀원의 PC에서 Claude Code / Codex / Gemini CLI 사용량을 자동으로 수집하여 대시보드에 반영합니다.

## 설치

**Mac / Linux:**
```bash
curl -sL https://raw.githubusercontent.com/EO-Studio-Dev/token-dashboard-hooks/main/install-hook.sh | bash
```

**Windows (PowerShell):**
```powershell
powershell -Command "irm https://raw.githubusercontent.com/EO-Studio-Dev/token-dashboard-hooks/main/install-hook.ps1 | iex"
```

## 포함 스크립트

| 파일 | 역할 |
|------|------|
| `install-hook.sh` / `install-hook.ps1` | 원라이너 설치 스크립트 |
| `hook_health.py` | 30분 주기 데이터 수집 오케스트레이터 |
| `otel_push.py` | 토큰 사용량 backfill push |
| `generate_activity.py` | transcript → Activity Feed 데이터 생성 |
| `generate_backfill.py` | backfill JSON 생성 |
| `codex_push.py` | Codex CLI 데이터 push |

## 대시보드

https://token-dashboard-iota.vercel.app
