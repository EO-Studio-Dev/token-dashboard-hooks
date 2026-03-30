#!/bin/bash
# Token Dashboard - AI 도구 사용량 자동 수집 설치
# Claude Code / Codex / Gemini CLI 토큰 사용량을 자동으로 대시보드에 수집합니다.
# 기존 transcript가 있으면 과거 데이터도 자동으로 대시보드에 전송합니다.
#
# 필요: python3, curl, git (GitHub 계정 불필요)
#
# 사용법:
#   curl -sL https://raw.githubusercontent.com/EO-Studio-Dev/token-dashboard-hooks/main/install-hook.sh | bash

set -e

# Windows 환경 감지 → Python UTF-8 모드 강제
if [[ "$OSTYPE" == "msys" || "$OSTYPE" == "cygwin" || "$OS" == "Windows_NT" ]]; then
  export PYTHONUTF8=1
  export PYTHONIOENCODING=utf-8
fi

HOOKS_DIR="$HOME/.claude/hooks"
SETTINGS="$HOME/.claude/settings.json"
HOOK_FILE="$HOOKS_DIR/otel_push.py"
HOOK_HEALTH_FILE="$HOOKS_DIR/hook_health.py"
BASE_URL="https://raw.githubusercontent.com/EO-Studio-Dev/token-dashboard-hooks/main"
DASHBOARD_API="https://token-dashboard-iota.vercel.app/api/backfill"
OTEL_COLLECTOR="https://otel-collector-production-2dac.up.railway.app"
GEMINI_SETTINGS="$HOME/.gemini/settings.json"

echo ""
echo "  ╔═══════════════════════════════════════════════════════════╗"
echo "  ║          EO Studio Token Dashboard Installer              ║"
echo "  ╠═══════════════════════════════════════════════════════════╣"
echo "  ║                                                           ║"
echo "  ║  EO Studio 내부 전용 도구입니다.                         ║"
echo "  ║  AI 도구(Claude/Codex/Gemini) 사용량만 수집하며,        ║"
echo "  ║  코드·파일·개인정보는 일절 수집하지 않습니다.            ║"
echo "  ║  모든 데이터는 EO Studio 내부 서버로만 전송됩니다.      ║"
echo "  ║                                                           ║"
echo "  ║  [!] 설치 중 아래 보안 경고가 뜰 수 있습니다:           ║"
echo "  ║      • \"네트워크 연결을 허용하시겠습니까?\" → 허용       ║"
echo "  ║      • \"컴퓨터를 관리하려 합니다\" → 허용 (cron 등록)   ║"
echo "  ║      • Claude Code hook 승인 요청 → 허용                ║"
echo "  ║      모두 정상이며, EO Studio 대시보드 연동에 필요합니다 ║"
echo "  ║                                                           ║"
echo "  ║  대시보드: https://token-dashboard-iota.vercel.app        ║"
echo "  ║  문의: 서현 (ash@eoeoeo.net)                              ║"
echo "  ║                                                           ║"
echo "  ╚═══════════════════════════════════════════════════════════╝"
echo ""

# 0. 필수 도구 확인
for cmd in python3 curl git; do
  if ! command -v "$cmd" &>/dev/null; then
    echo "[!] $cmd 이 설치되어 있지 않습니다."
    exit 1
  fi
done

GIT_EMAIL=$(git config user.email 2>/dev/null || echo "")
if [ -z "$GIT_EMAIL" ] || ! echo "$GIT_EMAIL" | grep -q "@eoeoeo.net"; then
  if [ -n "$GIT_EMAIL" ]; then
    echo "현재 이메일: $GIT_EMAIL (eoeoeo.net이 아닙니다)"
  fi
  echo ""
  echo "본인의 @eoeoeo.net 이메일을 입력해주세요 (예: june)"
  printf "이메일 아이디: "
  read -r EMAIL_ID </dev/tty
  if [ -z "$EMAIL_ID" ]; then
    echo "[!] 이메일을 입력하지 않았습니다."
    exit 1
  fi
  # 풀 이메일 입력 시 중복 도메인 방지
  if echo "$EMAIL_ID" | grep -q "@"; then
    GIT_EMAIL="$EMAIL_ID"
  else
    GIT_EMAIL="${EMAIL_ID}@eoeoeo.net"
  fi
  git config --global user.email "$GIT_EMAIL"
  echo "-> git email 설정 완료: $GIT_EMAIL"
fi

echo "사용자: $GIT_EMAIL"

# 이메일을 파일로 저장 (git 없는 환경에서도 otel_push가 사용자를 식별하도록)
mkdir -p "$HOOKS_DIR"
echo "$GIT_EMAIL" > "$HOOKS_DIR/.otel_email"
echo ""

# 1. hooks 디렉토리 생성
mkdir -p "$HOOKS_DIR"

# 2. hook 파일 다운로드
echo "[1/8] otel_push.py 다운로드 중..."
curl -sL "$BASE_URL/otel_push.py" -o "$HOOK_FILE"
chmod +x "$HOOK_FILE"
echo "      -> $HOOK_FILE"

# 3. 기존 Stop/UserPromptSubmit hook 정리 (세션 블로킹 방지 — launchd 스캔으로 대체)
echo "[2/8] 레거시 hook 정리 중..."

python3 -c "
import json, os

path = os.path.expanduser('~/.claude/settings.json')
if not os.path.exists(path):
    print('      -> settings.json 없음. 건너뜁니다.')
    exit()

with open(path, 'r') as f:
    data = json.load(f)

hooks = data.get('hooks', {})
removed = []
for event_type in ['Stop', 'UserPromptSubmit']:
    entries = hooks.get(event_type, [])
    cleaned = []
    for entry in entries:
        entry_hooks = entry.get('hooks', [])
        filtered = [h for h in entry_hooks if 'otel_push' not in h.get('command', '')]
        if filtered:
            entry['hooks'] = filtered
            cleaned.append(entry)
        elif entry_hooks:
            removed.append(event_type)
    hooks[event_type] = cleaned

if removed:
    with open(path, 'w') as f:
        json.dump(data, f, indent=2)
    print('      -> ' + ', '.join(removed) + ' hook 제거 완료 (launchd 스캔으로 대체)')
else:
    print('      -> 정리할 hook 없음.')
"

# 4. 과거 transcript backfill → JSON 생성 → Dashboard API로 전송
echo "[4/8] 과거 transcript backfill 중..."

CLAUDE_DIR="$HOME/.claude/projects"
if [ ! -d "$CLAUDE_DIR" ]; then
  echo "      ~/.claude/projects 디렉토리가 없습니다. 건너뜁니다."
else
  TRANSCRIPT_COUNT=$(find "$CLAUDE_DIR" -name "*.jsonl" 2>/dev/null | grep -v subagents | wc -l | tr -d ' ')

  if [ "$TRANSCRIPT_COUNT" -gt 0 ]; then
    echo "      transcript 파일 ${TRANSCRIPT_COUNT}개 발견."

    # generate_backfill.py 다운로드 & 실행
    BACKFILL_SCRIPT=$(mktemp)
    BACKFILL_JSON=$(mktemp)
    curl -sL "$BASE_URL/generate_backfill.py" -o "$BACKFILL_SCRIPT"
    python3 "$BACKFILL_SCRIPT" --out "$BACKFILL_JSON"

    DATA_COUNT=$(python3 -c "import json; print(len(json.load(open('$BACKFILL_JSON'))['data']))" 2>/dev/null || echo "0")

    if [ "$DATA_COUNT" -gt 0 ]; then
      echo "      ${DATA_COUNT}개 레코드 생성. 대시보드로 전송 중..."

      # email 필드 추가해서 API에 POST
      PAYLOAD=$(python3 -c "
import json
with open('$BACKFILL_JSON') as f:
    data = json.load(f)
data['email'] = '$GIT_EMAIL'
print(json.dumps(data))
")

      RESPONSE=$(curl -s -w "\n%{http_code}" -X POST "$DASHBOARD_API" \
        -H "Content-Type: application/json" \
        -d "$PAYLOAD")

      HTTP_CODE=$(echo "$RESPONSE" | tail -1)
      BODY=$(echo "$RESPONSE" | head -1)

      if [ "$HTTP_CODE" = "200" ]; then
        echo "      -> 전송 완료! Vercel 재배포 후 대시보드에 표시됩니다."
      else
        echo "      -> 전송 실패 (HTTP $HTTP_CODE): $BODY"
        echo "      -> hook 설치는 완료되었습니다. 과거 데이터는 나중에 재시도 가능합니다."
      fi
    else
      echo "      파싱 가능한 데이터가 없습니다. 건너뜁니다."
    fi

    rm -f "$BACKFILL_SCRIPT" "$BACKFILL_JSON"
  else
    echo "      과거 transcript가 없습니다. 건너뜁니다."
  fi
fi

# 5. Codex CLI 세션 데이터 수집 (1회 즉시 실행)
echo "[5/8] Codex CLI 데이터 수집 중..."

CODEX_SESSIONS="$HOME/.codex/sessions"
if [ -d "$CODEX_SESSIONS" ]; then
  CODEX_SCRIPT=$(mktemp)
  curl -sL "$BASE_URL/codex_push.py" -o "$CODEX_SCRIPT"
  python3 "$CODEX_SCRIPT" --email "$GIT_EMAIL" 2>&1 | sed 's/^/      /'
  rm -f "$CODEX_SCRIPT"
else
  echo "      ~/.codex/sessions/ 없음. Codex를 사용하면 자동 수집됩니다."
fi

# 6. 스크립트 로컬 설치 + 자동 수집 등록
echo "[6/8] 스크립트 설치 + 자동 수집 등록 중..."

CODEX_PUSH_LOCAL="$HOOKS_DIR/codex_push.py"
HOOK_HEALTH_LOCAL="$HOOKS_DIR/hook_health.py"
GENERATE_ACTIVITY_LOCAL="$HOOKS_DIR/generate_activity.py"
GENERATE_BACKFILL_LOCAL="$HOOKS_DIR/generate_backfill.py"

echo "      스크립트 다운로드 중..."
curl -sL "$BASE_URL/codex_push.py" -o "$CODEX_PUSH_LOCAL"
curl -sL "$BASE_URL/hook_health.py" -o "$HOOK_HEALTH_LOCAL"
curl -sL "$BASE_URL/generate_activity.py" -o "$GENERATE_ACTIVITY_LOCAL"
curl -sL "$BASE_URL/generate_backfill.py" -o "$GENERATE_BACKFILL_LOCAL"
chmod +x "$CODEX_PUSH_LOCAL" "$HOOK_HEALTH_LOCAL" "$GENERATE_ACTIVITY_LOCAL" "$GENERATE_BACKFILL_LOCAL"

# 로컬 파일만 실행 — 네트워크 접근 없음 (보안 경고 방지)
SCHEDULER_CMD="python3 $HOOK_HEALTH_LOCAL; python3 $CODEX_PUSH_LOCAL --email $GIT_EMAIL"

if [[ "$(uname)" == "Darwin" ]]; then
  # macOS: launchd 사용 (cron은 Full Disk Access 없으면 silent fail)
  LAUNCHD_LABEL="net.eoeoeo.hook-health"
  PLIST_PATH="$HOME/Library/LaunchAgents/${LAUNCHD_LABEL}.plist"
  mkdir -p "$HOME/Library/LaunchAgents"

  cat > "$PLIST_PATH" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${LAUNCHD_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>-c</string>
        <string>${SCHEDULER_CMD}</string>
    </array>
    <key>StartInterval</key>
    <integer>1800</integer>
    <key>RunAtLoad</key>
    <true/>
    <key>StandardOutPath</key>
    <string>${HOOKS_DIR}/launchd.log</string>
    <key>StandardErrorPath</key>
    <string>${HOOKS_DIR}/launchd.log</string>
</dict>
</plist>
PLIST

  # 기존 등록 해제 후 재등록
  launchctl unload "$PLIST_PATH" 2>/dev/null
  launchctl load "$PLIST_PATH"

  # 기존 cron 제거 (마이그레이션)
  CRON_TMP=$(mktemp)
  crontab -l > "$CRON_TMP" 2>/dev/null
  if grep -q "eo-codex-push" "$CRON_TMP"; then
    grep -v "eo-codex-push" "$CRON_TMP" > "${CRON_TMP}.new" 2>/dev/null
    crontab "${CRON_TMP}.new"
    echo "      -> 기존 cron 제거 완료 (launchd로 전환)"
  fi
  rm -f "$CRON_TMP" "${CRON_TMP}.new"
  echo "      -> launchd 등록 완료: 30분마다 transcript 스캔 + 자동 수집"
else
  # Linux: cron 사용
  CRON_LINE="*/30 * * * * $SCHEDULER_CMD # eo-codex-push"
  CRON_TMP=$(mktemp)
  crontab -l > "$CRON_TMP" 2>/dev/null
  grep -v "eo-codex-push" "$CRON_TMP" > "${CRON_TMP}.new" 2>/dev/null
  echo "$CRON_LINE" >> "${CRON_TMP}.new"
  crontab "${CRON_TMP}.new"
  rm -f "$CRON_TMP" "${CRON_TMP}.new"
  echo "      -> cron 등록 완료: 30분마다 transcript 스캔 + 자동 수집"
fi

echo "[6.5/8] self-heal hook 등록 중..."
python3 - <<PY
import json, os
settings_path = os.path.expanduser("$SETTINGS")
# 로컬 파일만 실행 — curl 제거로 보안 경고 방지
hook_cmd = "bash -lc '(python3 ~/.claude/hooks/hook_health.py --self-heal >/dev/null 2>&1 &) >/dev/null 2>&1'"
data = {}
if os.path.exists(settings_path):
    with open(settings_path, "r", encoding="utf-8") as f:
        data = json.load(f)
if "hooks" not in data:
    data["hooks"] = {}
entries = data["hooks"].get("UserPromptSubmit", [])
found = False
for entry in entries:
    for hook in entry.get("hooks", []):
        if "hook_health.py --self-heal" in hook.get("command", ""):
            found = True
if not found:
    entries.append({"hooks": [{"type": "command", "command": hook_cmd}]})
    data["hooks"]["UserPromptSubmit"] = entries
    with open(settings_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
print("      -> UserPromptSubmit self-heal hook 등록 완료")
PY

# 7. Gemini CLI 텔레메트리 설정 (네이티브 OTel → Collector 직접 전송)
echo "[7/8] Gemini CLI 텔레메트리 설정 중..."

if command -v gemini &>/dev/null; then
  mkdir -p "$HOME/.gemini"

  python3 -c "
import json, os

path = os.path.expanduser('$GEMINI_SETTINGS')
otel_endpoint = '$OTEL_COLLECTOR'

# settings.json 읽기 (없으면 빈 객체)
data = {}
if os.path.exists(path):
    try:
        with open(path, 'r') as f:
            data = json.load(f)
    except (json.JSONDecodeError, IOError):
        data = {}

# telemetry 섹션 설정
existing = data.get('telemetry', {})
new_telemetry = {
    'enabled': True,
    'target': 'local',
    'otlpEndpoint': otel_endpoint,
    'otlpProtocol': 'http',
}

if existing.get('otlpEndpoint') == otel_endpoint:
    print('      -> 이미 설정되어 있습니다.')
else:
    data['telemetry'] = {**existing, **new_telemetry}
    with open(path, 'w') as f:
        json.dump(data, f, indent=2)
    old_ep = existing.get('otlpEndpoint', '')
    if old_ep:
        print('      -> endpoint 업데이트: ' + old_ep + ' → ' + otel_endpoint)
    else:
        print('      -> 텔레메트리 설정 완료: ' + otel_endpoint)
"
else
  echo "      Gemini CLI 미설치. 설치 후 install-hook.sh를 다시 실행하면 자동 설정됩니다."
fi

# 8. Gemini CLI GEMINI.md에 사용자 이메일 기록 (메트릭 식별용)
echo "[8/8] Gemini CLI 사용자 설정 중..."

if command -v gemini &>/dev/null; then
  python3 -c "
import json, os

path = os.path.expanduser('$GEMINI_SETTINGS')
email = '$GIT_EMAIL'

data = {}
if os.path.exists(path):
    try:
        with open(path, 'r') as f:
            data = json.load(f)
    except (json.JSONDecodeError, IOError):
        data = {}

if data.get('userEmail') == email:
    print('      -> 이미 설정되어 있습니다.')
else:
    data['userEmail'] = email
    with open(path, 'w') as f:
        json.dump(data, f, indent=2)
    print(f'      -> 사용자 이메일 설정: {email}')
"
else
  echo "      건너뜀 (Gemini CLI 미설치)"
fi

echo ""
echo "=== 설치 완료 ==="
echo "사용자: $GIT_EMAIL"
echo "대시보드: https://token-dashboard-iota.vercel.app"
echo ""
if [[ "$(uname)" == "Darwin" ]]; then
  echo "Claude Code: 30분마다 transcript 스캔 (launchd)"
  echo "Codex CLI:   30분마다 자동 수집 (launchd)"
else
  echo "Claude Code: 30분마다 transcript 스캔 (cron)"
  echo "Codex CLI:   30분마다 자동 수집 (cron)"
fi
echo "Gemini CLI:  세션 중 실시간 전송 (네이티브 OTel)"
