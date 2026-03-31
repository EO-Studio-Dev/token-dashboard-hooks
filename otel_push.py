"""
OTel Push - Stop Hook
Claude Code 세션 종료 시 transcript JSONL을 파싱하여
토큰 사용량을 OTel Collector로 push한다.

내장 텔레메트리의 모델명 오보고 버그를 우회하여 정확한 데이터를 전송.
"""
SCRIPT_VERSION = "2"

import json
import hashlib
import os
import re
import sys
import urllib.request
import urllib.error
from collections import defaultdict

OTEL_ENDPOINT = "https://otel-collector-production-2dac.up.railway.app"
# Gemini CLI v0.33+ 표준 경로 복원 (v0.32 버그 우회 해제)
OTLP_METRICS_PATH = "/v1/metrics"

# 팀 메타데이터
SERVICE_NAME = "claude-code"
TEAM_NAME = "eostudio"


def parse_transcript(transcript_path: str) -> list[dict]:
    """transcript JSONL에서 assistant 메시지의 model/usage 추출 (message ID 중복 제거)

    Claude Code transcript는 하나의 API 호출에 대해 여러 JSONL 행을 기록
    (thinking block, tool_use block, text block 등). 같은 message.id는
    동일한 usage를 공유하므로 마지막 항목만 사용하여 2-4x 과다집계 방지.
    """
    # message ID별로 마지막 항목만 유지 (last-wins 전략)
    seen: dict[str, dict] = {}
    no_id_entries: list[dict] = []
    try:
        with open(transcript_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if record.get("type") != "assistant":
                    continue

                msg = record.get("message", {})
                model = msg.get("model")
                usage = msg.get("usage")
                if not model or not usage:
                    continue

                msg_id = msg.get("id")
                entry = {"model": model, "usage": usage}
                if msg_id:
                    seen[msg_id] = entry  # 같은 ID는 마지막 값으로 덮어쓰기
                else:
                    no_id_entries.append(entry)
    except (IOError, OSError):
        pass
    return list(seen.values()) + no_id_entries


def count_bash_commands(transcript_path: str):
    """transcript에서 git commit / gh pr create 실행 횟수 카운트 (fallback용)"""
    commits = 0
    prs = 0
    try:
        with open(transcript_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("type") != "assistant":
                    continue
                for block in record.get("message", {}).get("content", []):
                    if block.get("type") == "tool_use" and block.get("name") == "Bash":
                        cmd = block.get("input", {}).get("command", "")
                        if "git commit" in cmd:
                            commits += 1
                        if "gh pr create" in cmd:
                            prs += 1
    except (IOError, OSError):
        pass
    return commits, prs


def count_git_activity(transcript_path: str, user_email: str):
    """git log 기반 세션 중 실제 커밋 카운트.
    transcript 파일 생성 시점 이후의 커밋을 git history에서 직접 조회.
    Claude Code 밖에서 만든 커밋도 포함되므로 전 팀원 커버 가능.
    """
    import subprocess
    import datetime

    # 세션 시작 시간: transcript 파일 생성 시간
    try:
        stat = os.stat(transcript_path)
        start_time = getattr(stat, "st_birthtime", stat.st_ctime)
        start_iso = datetime.datetime.fromtimestamp(start_time).isoformat()
    except Exception:
        return 0

    # git log로 세션 중 커밋 카운트
    try:
        result = subprocess.run(
            ["git", "log", "--oneline", f"--after={start_iso}", f"--author={user_email}"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return len(result.stdout.strip().split("\n"))
    except Exception:
        pass
    return 0


def aggregate_tokens(entries: list[dict]) -> dict:
    """모델별, 토큰 타입별 합산"""
    # key: (model, token_type) -> sum
    totals = defaultdict(int)

    for e in entries:
        model = e["model"]
        usage = e["usage"]

        totals[(model, "input")] += usage.get("input_tokens", 0)
        totals[(model, "output")] += usage.get("output_tokens", 0)
        totals[(model, "cache_read")] += usage.get("cache_read_input_tokens", 0)
        totals[(model, "cache_creation")] += usage.get("cache_creation_input_tokens", 0)

    return dict(totals)


def estimate_cost(totals: dict) -> dict:
    """모델별 비용 추정 (USD). 가격은 2026-03 기준 근사치."""
    # per 1M tokens
    pricing = {
        "claude-opus-4-6": {"input": 15.0, "output": 75.0, "cache_read": 1.5, "cache_creation": 18.75},
        "claude-sonnet-4-6": {"input": 3.0, "output": 15.0, "cache_read": 0.3, "cache_creation": 3.75},
        "claude-haiku-4-5": {"input": 0.8, "output": 4.0, "cache_read": 0.08, "cache_creation": 1.0},
    }
    # fallback: sonnet pricing
    default_price = pricing["claude-sonnet-4-6"]

    costs = defaultdict(float)
    for (model, token_type), count in totals.items():
        price_map = pricing.get(model, default_price)
        rate = price_map.get(token_type, price_map.get("input", 3.0))
        costs[model] += (count / 1_000_000) * rate

    return dict(costs)


def build_otlp_payload(totals: dict, costs: dict, user_email: str, session_id: str, commits: int = 0, prs: int = 0) -> dict:
    """OTLP JSON 형식의 메트릭 payload 생성"""
    import time

    now_ns = int(time.time() * 1e9)

    # 토큰 메트릭 데이터 포인트
    token_data_points = []
    for (model, token_type), count in totals.items():
        if count == 0:
            continue
        token_data_points.append({
            "attributes": [
                {"key": "model", "value": {"stringValue": model}},
                {"key": "token_type", "value": {"stringValue": token_type}},
                {"key": "user_email", "value": {"stringValue": user_email}},
            ],
            "timeUnixNano": str(now_ns),
            "startTimeUnixNano": str(now_ns),
            "asInt": str(count),
        })

    # 비용 메트릭 데이터 포인트
    cost_data_points = []
    for model, cost in costs.items():
        if cost == 0:
            continue
        cost_data_points.append({
            "attributes": [
                {"key": "model", "value": {"stringValue": model}},
                {"key": "user_email", "value": {"stringValue": user_email}},
            ],
            "timeUnixNano": str(now_ns),
            "startTimeUnixNano": str(now_ns),
            "asDouble": round(cost, 6),
        })

    metrics = []
    if token_data_points:
        metrics.append({
            "name": "claude_code_tokens_total",
            "description": "Claude Code token usage per session",
            "sum": {
                "dataPoints": token_data_points,
                "aggregationTemporality": 1,  # DELTA
                "isMonotonic": True,
            },
        })
    if cost_data_points:
        metrics.append({
            "name": "claude_code_cost_total",
            "description": "Claude Code estimated cost per session (USD)",
            "sum": {
                "dataPoints": cost_data_points,
                "aggregationTemporality": 1,
                "isMonotonic": True,
            },
        })

    # 세션 카운트: otel_push.py 1회 실행 = 1 세션
    metrics.append({
        "name": "claude_code_session_count_total",
        "description": "Claude Code session count",
        "sum": {
            "dataPoints": [{
                "attributes": [
                    {"key": "user_email", "value": {"stringValue": user_email}},
                ],
                "timeUnixNano": str(now_ns),
                "startTimeUnixNano": str(now_ns),
                "asInt": "1",
            }],
            "aggregationTemporality": 1,
            "isMonotonic": True,
        },
    })

    if commits > 0:
        metrics.append({
            "name": "claude_code_commit_count_total",
            "description": "Git commits made during Claude Code sessions",
            "sum": {
                "dataPoints": [{
                    "attributes": [
                        {"key": "user_email", "value": {"stringValue": user_email}},
                    ],
                    "timeUnixNano": str(now_ns),
                    "startTimeUnixNano": str(now_ns),
                    "asInt": str(commits),
                }],
                "aggregationTemporality": 1,
                "isMonotonic": True,
            },
        })

    if prs > 0:
        metrics.append({
            "name": "claude_code_pull_request_count_total",
            "description": "Pull requests created during Claude Code sessions",
            "sum": {
                "dataPoints": [{
                    "attributes": [
                        {"key": "user_email", "value": {"stringValue": user_email}},
                    ],
                    "timeUnixNano": str(now_ns),
                    "startTimeUnixNano": str(now_ns),
                    "asInt": str(prs),
                }],
                "aggregationTemporality": 1,
                "isMonotonic": True,
            },
        })

    return {
        "resourceMetrics": [
            {
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": SERVICE_NAME}},
                        {"key": "team.name", "value": {"stringValue": TEAM_NAME}},
                    ]
                },
                "scopeMetrics": [
                    {
                        "scope": {"name": "claude.code.stop-hook", "version": "1.0.0"},
                        "metrics": metrics,
                    }
                ],
            }
        ]
    }


def push_metrics(payload: dict) -> bool:
    """OTLP HTTP JSON으로 메트릭 전송"""
    url = OTEL_ENDPOINT + OTLP_METRICS_PATH
    data = json.dumps(payload).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError):
        return False


EMAIL_ALIAS = {
    "jobskim@icloud.com": "ty@eoeoeo.net",
}


def sanitize_email(email: str) -> str:
    """중복 도메인 제거 + alias 변환 (git email이 다른 팀원용)"""
    at_count = email.count("@")
    if at_count > 1:
        parts = email.split("@")
        email = f"{parts[0]}@{parts[-1]}"
    normalized = email.lower()
    return EMAIL_ALIAS.get(normalized, normalized)


def detect_user_email() -> str:
    """이메일 감지: .otel_email 파일 → git config → unknown"""
    # 1. install-hook이 저장한 .otel_email 파일
    otel_email_path = os.path.join(os.path.expanduser("~/.claude/hooks"), ".otel_email")
    try:
        if os.path.exists(otel_email_path):
            with open(otel_email_path, "r", encoding="utf-8") as f:
                email = f.read().strip()
            if email:
                return sanitize_email(email)
    except Exception:
        pass
    # 2. git config
    try:
        import subprocess
        result = subprocess.run(
            ["git", "config", "user.email"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return sanitize_email(result.stdout.strip())
    except Exception:
        pass
    return "unknown"


SENT_STATE_DIR = os.path.expanduser("~/.claude/hooks/.otel_sent")


def _state_path(transcript_path: str) -> str:
    """transcript 경로를 해시하여 상태 파일 경로 생성"""
    h = hashlib.md5(transcript_path.encode()).hexdigest()[:12]
    return os.path.join(SENT_STATE_DIR, f"{h}.json")


def load_sent_state(transcript_path: str) -> dict:
    """이전 push에서 보낸 토큰 합계 로드. 없으면 빈 dict."""
    path = _state_path(transcript_path)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (IOError, OSError, json.JSONDecodeError):
        return {}


def save_sent_state(transcript_path: str, totals: dict, commits: int = 0, prs: int = 0):
    """현재 토큰 합계를 상태 파일에 저장 (다음 push에서 delta 계산용)"""
    os.makedirs(SENT_STATE_DIR, exist_ok=True)
    path = _state_path(transcript_path)
    # key를 "model|token_type" 문자열로 변환 (JSON 호환)
    serializable = {"_commits": commits, "_prs": prs}
    for (model, token_type), count in totals.items():
        serializable[f"{model}|{token_type}"] = count
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(serializable, f)
    except (IOError, OSError):
        pass


def compute_delta(totals: dict, prev_state: dict) -> dict:
    """현재 합계 - 이전에 보낸 합계 = 실제 delta"""
    delta = {}
    for (model, token_type), count in totals.items():
        key = f"{model}|{token_type}"
        prev = prev_state.get(key, 0)
        diff = count - prev
        if diff > 0:
            delta[(model, token_type)] = diff
    return delta


ACTIVITY_API_URL = "https://token-dashboard-iota.vercel.app/api/activity"

DAILY_BACKFILL_MARKER = os.path.expanduser("~/.claude/hooks/.backfill_daily")
BACKFILL_SCRIPT_URL = "https://raw.githubusercontent.com/EO-Studio-Dev/token-dashboard-hooks/main/generate_backfill.py"
BACKFILL_API_URL = "https://token-dashboard-iota.vercel.app/api/backfill"


def send_session_backfill(delta: dict, user_email: str):
    """매 세션 종료 시 현재 세션의 delta를 backfill API에 전송.
    backfill cutoff를 항상 오늘로 유지하여 Prometheus 과다집계 영향 차단."""
    import datetime

    if not delta or not user_email:
        return

    today = datetime.date.today().isoformat()

    # delta: {(model, token_type): count} → 날짜+모델별 집계
    by_model = defaultdict(lambda: {"input_tokens": 0, "output_tokens": 0,
                                     "cache_read_tokens": 0, "cache_creation_tokens": 0})
    for (model, token_type), count in delta.items():
        if token_type == "input":
            by_model[model]["input_tokens"] += count
        elif token_type == "output":
            by_model[model]["output_tokens"] += count
        elif token_type == "cache_read":
            by_model[model]["cache_read_tokens"] += count
        elif token_type == "cache_creation":
            by_model[model]["cache_creation_tokens"] += count

    records = []
    for model, tokens in by_model.items():
        records.append({"date": today, "model": model, **tokens})

    if not records:
        return

    try:
        payload = json.dumps({"email": user_email, "data": records, "mode": "add"}).encode("utf-8")
        req = urllib.request.Request(
            BACKFILL_API_URL,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=15)
    except Exception:
        pass  # 실패해도 메인 로직에 영향 없음


def maybe_daily_rebackfill(user_email: str):
    """하루 1회 전체 transcript re-backfill. 누락 보정용 안전망.
    날짜 마커로 오늘 이미 실행했으면 스킵."""
    import datetime

    today = datetime.date.today().isoformat()

    # 오늘 이미 실행했는지 확인
    try:
        if os.path.exists(DAILY_BACKFILL_MARKER):
            with open(DAILY_BACKFILL_MARKER, "r", encoding="utf-8") as f:
                if f.read().strip() == today:
                    return
    except Exception:
        pass

    import subprocess
    import tempfile

    try:
        # 최신 generate_backfill.py 다운로드
        script_path = os.path.join(tempfile.gettempdir(), "generate_backfill_daily.py")
        urllib.request.urlretrieve(BACKFILL_SCRIPT_URL, script_path)

        # 실행하여 backfill JSON 생성 (Windows: python3 없을 수 있으므로 sys.executable 사용)
        out_path = os.path.join(tempfile.gettempdir(), "backfill_daily.json")
        subprocess.run(
            [sys.executable, script_path, "--out", out_path],
            capture_output=True, text=True, timeout=60,
        )

        if not os.path.exists(out_path):
            return

        with open(out_path, "r", encoding="utf-8") as f:
            backfill_data = json.load(f)

        if not backfill_data.get("data"):
            return

        # API에 POST
        backfill_data["email"] = user_email
        payload = json.dumps(backfill_data).encode("utf-8")
        req = urllib.request.Request(
            BACKFILL_API_URL,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.status == 200:
                # 날짜 마커 갱신 — 오늘은 다시 실행 안 함
                os.makedirs(os.path.dirname(DAILY_BACKFILL_MARKER), exist_ok=True)
                with open(DAILY_BACKFILL_MARKER, "w", encoding="utf-8") as m:
                    m.write(today)
    except Exception:
        pass  # 실패해도 메인 로직에 영향 없음
    finally:
        for p in [script_path, out_path]:
            try:
                os.remove(p)
            except Exception:
                pass


SKIP_TAGS = (
    "<system-reminder>",
    "<local-command-caveat>",
    "<local-command-stdout>",
    "<command-name>",
    "<command-message>",
    "Base directory for this skill:",
    "Stop hooks aren't triggered",
)


def _extract_user_message_text(content) -> str | None:
    """user message content에서 텍스트 추출."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text", "")
                if isinstance(text, str):
                    texts.append(text)
            elif isinstance(block, str):
                texts.append(block)
        return " ".join(texts) if texts else None
    return None


def _should_skip_message(text: str) -> bool:
    """system-reminder 등 태그 콘텐츠가 포함된 메시지인지 확인"""
    stripped = text.strip()
    for tag in SKIP_TAGS:
        if stripped.startswith(tag):
            return True
    return False


def _extract_project_from_cwd(cwd: str) -> str:
    """cwd에서 마지막 의미있는 디렉토리명 추출"""
    if not cwd:
        return "unknown"
    normalized = cwd.replace("\\", "/").rstrip("/")
    parts = normalized.split("/")
    for part in reversed(parts):
        if part:
            return part
    return "unknown"


def _infer_work_type(files_changed: list, tools: dict) -> str:
    """파일 경로 + 도구 패턴으로 작업 유형 분류"""
    files_lower = [f.lower().replace("\\", "/") for f in files_changed]
    total = sum(tools.values()) or 1
    has_comp = any("/component" in f or ".tsx" in f or ".jsx" in f for f in files_lower)
    has_api = any("/api/" in f or "route.ts" in f for f in files_lower)
    has_script = any("/script" in f or f.endswith(".py") or f.endswith(".sh") for f in files_lower)
    has_doc = any(f.endswith(".md") for f in files_lower)
    has_test = any("test" in f or "spec" in f for f in files_lower)
    edit_r = (tools.get("Edit", 0) + tools.get("Write", 0)) / total
    read_r = (tools.get("Read", 0) + tools.get("Grep", 0) + tools.get("Glob", 0)) / total
    bash_r = tools.get("Bash", 0) / total
    has_web = bool(set(tools) & {"WebSearch", "WebFetch"}) or any(k.startswith("mcp__") for k in tools)
    if has_test: return "testing"
    if has_comp: return "ui"
    if has_api: return "api"
    if has_script: return "scripting"
    if has_doc and edit_r > 0.3: return "docs"
    if has_web or read_r > 0.6: return "research"
    if bash_r > 0.5: return "devops"
    return "coding"


def _generate_haiku_summary(project: str, work_type: str, commit_messages: list,
                             files_changed: list, tools: dict) -> str:
    """Claude Haiku로 1줄 자연어 요약. 실패 시 휴리스틱 요약."""
    known_project_slugs = [
        "token-dashboard",
        "finance-dashboard",
        "leave-dashboard",
        "eo-request-bot",
        "gowid-slack-bot",
        "eo-video-pipeline",
        "ash_bot",
        "ai-native-camp",
        "onboarding",
        "townhall",
    ]
    project_labels = {
        "eoash": "Seohyun Workspace",
        "token-dashboard": "Token Dashboard",
        "finance-dashboard": "Finance Dashboard",
        "leave-dashboard": "Leave Dashboard",
        "eo-request-bot": "EO Request Bot",
        "gowid-slack-bot": "Gowid Slack Bot",
        "eo-video-pipeline": "EO Video Pipeline",
        "ash_bot": "Ash Bot",
        "ai-native-camp": "AI Native Camp",
        "onboarding": "Onboarding Bot",
        "townhall": "Townhall Slides",
        "scripts": "Scripts",
        "ash": "Seohyun Workspace",
        "새 폴더": "Seohyun Workspace",
        "yoojinkang": "Jade Workspace",
        "saul_eo": "Seongheum Workspace",
        "chiri": "Chiri Workspace",
        "june": "June Workspace",
        "ty": "TaeYong Workspace",
        "phoenix": "Phoenix Workspace",
        "gwy": "Gunwook Workspace",
        "unknown": "Unknown",
    }

    def truncate_summary(text: str, max_len: int = 60) -> str:
        clean = re.sub(r"\s+", " ", text).strip()
        if len(clean) <= max_len:
            return clean
        return clean[: max_len - 3].rstrip() + "..."

    def strip_session_wrap_prefix(message: str) -> str:
        message = re.sub(r"^session wrap:\s*\d{4}-\d{2}-\d{2}\s*[—-]\s*", "", message, flags=re.I)
        return message.strip()

    def humanize_basename(name: str) -> str:
        return re.sub(r"[-_]+", " ", re.sub(r"\.[^.]+$", "", name)).strip()

    def resolve_project_label(raw: str) -> str:
        raw = (raw or "unknown").strip()
        return project_labels.get(raw) or project_labels.get(raw.lower()) or humanize_basename(raw).title()

    def has_korean(text: str) -> bool:
        return bool(re.search(r"[가-힣]", text))

    def is_low_signal_summary(text: str) -> bool:
        if not text or not text.strip():
            return True
        value = text.strip()
        if has_korean(value):
            return True
        if re.match(r"^session wrap:", value, flags=re.I):
            return True
        if re.search(r"\b(Read|Bash|ToolSearch|WebSearch|WebFetch|Grep|Glob|Task|Agent|Skill|Edit|Write)\s*x?\d+\b", value):
            return True
        if re.search(r"\.(md|json|ts|tsx|js|jsx|py|txt|srt|plist|yaml|yml)\b", value, flags=re.I):
            return True
        if len(value.split(", ")) >= 2:
            return True
        return False

    def is_tool_driven_text(text: str) -> bool:
        if not text or not text.strip():
            return True
        value = text.strip()
        if re.match(r"^session wrap:", value, flags=re.I):
            return True
        if re.search(r"\b(Read|Bash|ToolSearch|WebSearch|WebFetch|Grep|Glob|Task|Agent|Skill|Edit|Write)\s*x?\d+\b", value):
            return True
        if re.search(r"\.(md|json|ts|tsx|js|jsx|py|txt|srt|plist|yaml|yml)\b", value, flags=re.I):
            return True
        if re.match(r"^\d+\s+files edited$", value, flags=re.I):
            return True
        return False

    def infer_project_label() -> str:
        normalized = [f.replace("\\", "/").lower() for f in files_changed]
        for slug in known_project_slugs:
            if any(f"/{slug}/" in path for path in normalized):
                return resolve_project_label(slug)
        return resolve_project_label(project)

    def infer_project_focus() -> str:
        lower = [f.replace("\\", "/").lower() for f in files_changed]

        def has(pattern: str) -> bool:
            return any(re.search(pattern, path) for path in lower)

        if has(r"activity|otel_push|hook_health|transcript|backfill"):
            return "activity feed updates"
        if has(r"prometheus|metrics?"):
            return "metrics and reporting updates"
        if has(r"budget|executive-summary|p&l|finance"):
            return "finance dashboard updates"
        if has(r"airtable|attachment|expense|request|worker"):
            return "request workflow updates"
        if has(r"notion|meeting_minutes"):
            return "Notion workflow updates"
        if has(r"auth|login|session"):
            return "auth flow updates"
        if has(r"leaderboard|rank|members|board"):
            return "dashboard feature updates"
        if has(r"/api/|route\.ts$"):
            return "backend flow updates"
        if has(r"/components?/|page\.tsx$|page\.jsx$|\.tsx$|\.jsx$"):
            return "UI updates"
        if has(r"/skills?/|skill\.md$"):
            return "AI skill updates"
        if has(r"research|analysis|brief"):
            return "research brief updates"
        if has(r"subtitle|transcript|\.srt$|\.txt$"):
            return "subtitle workflow updates"
        if has(r"/scripts?/|\.py$|\.sh$"):
            return "automation updates"
        if has(r"claude\.md$|memory\.md$|\.md$"):
            return "working doc updates"

        if tools.get("WebSearch") or tools.get("WebFetch"):
            return "research work"
        if work_type == "api":
            return "backend updates"
        if work_type == "ui":
            return "UI updates"
        if work_type == "scripting":
            return "automation updates"
        if work_type == "research":
            return "research work"
        if work_type == "docs":
            return "working doc updates"
        if work_type == "testing":
            return "test fixes"
        if work_type == "devops":
            return "workflow improvements"
        return "project updates"

    def build_project_summary(subject: str) -> str:
        return truncate_summary(f"{infer_project_label()}: {subject}", 72)

    def build_heuristic_ai_summary() -> str:
        cleaned_commits = [strip_session_wrap_prefix(msg) for msg in commit_messages if msg]
        for msg in cleaned_commits:
            if not is_tool_driven_text(msg):
                return build_project_summary(msg)
        return build_project_summary(infer_project_focus())

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    fallback = build_heuristic_ai_summary()
    if not api_key:
        return fallback
    labels = {"ui": "UI", "api": "API", "scripting": "Script",
              "research": "Research", "devops": "DevOps", "docs": "Docs",
              "coding": "Coding", "testing": "Testing"}
    ctx = [f"Project: {infer_project_label()}", f"Type: {labels.get(work_type, work_type)}"]
    if commit_messages:
        ctx.append(f"Commits: {'; '.join(commit_messages[:3])}")
    if files_changed:
        ctx.append(f"Files: {', '.join(os.path.basename(f) for f in files_changed[:10])}")
    top = sorted(tools.items(), key=lambda x: -x[1])[:5]
    if top:
        ctx.append(f"Tools: {', '.join(f'{n} {c}x' for n, c in top)}")
    prompt = (
        "Summarize this session in English as '<Project>: <workstream>' in one short line. "
        "Start with the project or product name. Focus on what moved forward, not tools used.\n\n"
        + "\n".join(ctx) + "\n\nSummary ('<Project>: <workstream>'):"
    )
    try:
        payload = json.dumps({
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": 60,
            "messages": [{"role": "user", "content": prompt}],
        }).encode("utf-8")
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
            candidate = truncate_summary(result.get("content", [{}])[0].get("text", "").strip().strip("\"'"))
            return fallback if is_low_signal_summary(candidate) else candidate
    except Exception:
        return fallback


def extract_session_activity(transcript_path: str, user_email: str) -> dict | None:
    """transcript에서 ActivitySession 데이터 추출"""
    from datetime import datetime

    session_id = None
    cwd = None
    first_timestamp = None
    last_timestamp = None
    models = set()
    tools = defaultdict(int)
    commits = 0
    pull_requests = 0
    commit_messages = []
    files_changed = []
    seen = {}
    no_id_entries = []

    try:
        with open(transcript_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue

                ts_str = record.get("timestamp", "")
                if session_id is None and record.get("sessionId"):
                    session_id = record["sessionId"]
                if cwd is None and record.get("cwd"):
                    cwd = record["cwd"]
                if ts_str:
                    if first_timestamp is None:
                        first_timestamp = ts_str
                    last_timestamp = ts_str

                if record.get("type") == "assistant":
                    msg = record.get("message", {})
                    model = msg.get("model")
                    usage = msg.get("usage")
                    if model:
                        models.add(model)
                    for block in msg.get("content", []):
                        if block.get("type") == "tool_use":
                            tool_name = block.get("name", "")
                            if tool_name:
                                tools[tool_name] += 1
                            inp = block.get("input", {})
                            if tool_name in ("Edit", "Write") and inp.get("file_path"):
                                files_changed.append(inp["file_path"])
                            if tool_name == "Bash":
                                cmd = inp.get("command", "")
                                if "git commit" in cmd:
                                    commits += 1
                                    # HEREDOC: <<'EOF'\n실제메시지
                                    cm = re.search(r"<<'?EOF'?\s*\n(.+?)(?:\n|$)", cmd)
                                    if cm and not cm.group(1).strip().startswith("Co-Authored"):
                                        commit_messages.append(cm.group(1).strip()[:80])
                                    else:
                                        # 일반: -m "message" (HEREDOC/치환 제외)
                                        cm = re.search(r'-m\s+["\']([^$].+?)["\']', cmd)
                                        if cm:
                                            commit_messages.append(cm.group(1)[:80])
                                if "gh pr create" in cmd:
                                    pull_requests += 1
                    if model and usage and ts_str:
                        entry = {
                            "input_tokens": usage.get("input_tokens", 0),
                            "output_tokens": usage.get("output_tokens", 0),
                        }
                        msg_id = msg.get("id")
                        if msg_id:
                            seen[msg_id] = entry
                        else:
                            no_id_entries.append(entry)
    except (IOError, OSError):
        return None

    if not first_timestamp:
        return None

    total_input = 0
    total_output = 0
    for entry in list(seen.values()) + no_id_entries:
        total_input += entry["input_tokens"]
        total_output += entry["output_tokens"]

    duration_minutes = 0
    if first_timestamp and last_timestamp:
        try:
            dt_first = datetime.fromisoformat(first_timestamp.replace("Z", "+00:00"))
            dt_last = datetime.fromisoformat(last_timestamp.replace("Z", "+00:00"))
            duration_minutes = round((dt_last - dt_first).total_seconds() / 60, 1)
        except (ValueError, AttributeError):
            pass

    project = _extract_project_from_cwd(cwd) if cwd else "unknown"
    tools_dict = dict(tools)
    unique_files = list(dict.fromkeys(files_changed))

    # 자동 요약: 커밋 메시지 + 변경 파일 + 도구 패턴
    parts = []
    if commit_messages:
        parts.append(commit_messages[0])
    if unique_files:
        if len(unique_files) <= 3:
            parts.append(", ".join(os.path.basename(f) for f in unique_files))
        else:
            parts.append(f"{len(unique_files)} files edited")
    if not parts and tools_dict:
        top = sorted(tools_dict.items(), key=lambda x: -x[1])[:3]
        parts.append(" + ".join(f"{name} x{count}" for name, count in top))
    summary = " · ".join(parts)

    # 작업 유형 추론
    work_type = _infer_work_type(files_changed, tools_dict)

    # Haiku 요약 (ANTHROPIC_API_KEY 있을 때만)
    ai_summary = _generate_haiku_summary(project, work_type, commit_messages, unique_files, tools_dict)

    return {
        "id": session_id or os.path.basename(transcript_path).replace(".jsonl", ""),
        "cwd": cwd or "",
        "project": project,
        "timestamp": first_timestamp,
        "summary": summary,
        "aiSummary": ai_summary,
        "workType": work_type,
        "commitMessages": commit_messages[:5],
        "filesChanged": unique_files[:20],
        "models": sorted(models),
        "tools": dict(tools),
        "totalInputTokens": total_input,
        "totalOutputTokens": total_output,
        "commits": commits,
        "pullRequests": pull_requests,
        "durationMinutes": duration_minutes,
        "email": user_email,
    }


def send_session_activity(transcript_path: str, user_email: str):
    """세션 activity 데이터를 /api/activity에 POST"""
    activity = extract_session_activity(transcript_path, user_email)
    if not activity:
        return
    try:
        payload = json.dumps({"email": user_email, "data": [activity]}).encode("utf-8")
        req = urllib.request.Request(
            ACTIVITY_API_URL,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=15)
    except Exception:
        pass  # 실패해도 메인 로직에 영향 없음


def ensure_hook_registered():
    """settings.json에 Stop hook이 등록되어 있는지 확인하고, 없으면 자동 복구.
    otel_push.py는 매 세션 GitHub에서 자동 다운로드되므로,
    이 함수만으로 전 팀원 자가복구가 가능하다 (재설치 불필요)."""
    import platform
    settings_path = os.path.expanduser("~/.claude/settings.json")
    base_url = "https://raw.githubusercontent.com/EO-Studio-Dev/token-dashboard-hooks/main"
    hooks_dir = os.path.expanduser("~/.claude/hooks")
    hook_file = os.path.join(hooks_dir, "otel_push.py")

    if platform.system() == "Windows":
        hook_file_win = hook_file.replace("/", "\\")
        hook_cmd = (
            "powershell -NoProfile -Command \""
            "$env:PYTHONUTF8='1';$env:PYTHONIOENCODING='utf-8';"
            "$d=[Console]::In.ReadToEnd();"
            f"Invoke-WebRequest -Uri '{base_url}/otel_push.py' -OutFile '{hook_file_win}' -ErrorAction SilentlyContinue;"
            f"$d|python3 '{hook_file_win}'\""
        )
        self_heal_cmd = (
            "powershell -NoProfile -Command \""
            "$hookDir=Join-Path $HOME '.claude\\hooks';"
            "$hookHealth=Join-Path $hookDir 'hook_health.py';"
            "New-Item -ItemType Directory -Force -Path $hookDir | Out-Null;"
            f"Invoke-WebRequest -Uri '{base_url}/hook_health.py' -OutFile $hookHealth -ErrorAction SilentlyContinue;"
            "Start-Process python3 -ArgumentList @($hookHealth,'--self-heal') -WindowStyle Hidden\""
        )
    else:
        hook_cmd = (
            "bash -c 'D=$(cat);curl -sL "
            f"{base_url}/otel_push.py -o ~/.claude/hooks/otel_push.py 2>/dev/null;"
            "echo \"$D\"|python3 ~/.claude/hooks/otel_push.py'"
        )
        self_heal_cmd = (
            "bash -lc 'mkdir -p ~/.claude/hooks; "
            f"curl -sL {base_url}/hook_health.py -o ~/.claude/hooks/hook_health.py 2>/dev/null; "
            "(python3 ~/.claude/hooks/hook_health.py --self-heal >/dev/null 2>&1 &) >/dev/null 2>&1'"
        )

    try:
        data = {}
        if os.path.exists(settings_path):
            with open(settings_path, "r", encoding="utf-8") as f:
                data = json.load(f)

        if "hooks" not in data:
            data["hooks"] = {}

        found_stop = False
        found_self_heal = False
        needs_write = False

        for entry in data["hooks"].get("Stop", []):
            for hook in entry.get("hooks", []):
                cmd = hook.get("command", "")
                if "otel_push" in cmd:
                    found_stop = True
                    if platform.system() == "Windows" and cmd.startswith("bash "):
                        hook["command"] = hook_cmd
                        needs_write = True

        for entry in data["hooks"].get("UserPromptSubmit", []):
            for hook in entry.get("hooks", []):
                cmd = hook.get("command", "")
                if "hook_health.py --self-heal" in cmd:
                    found_self_heal = True
                    if platform.system() == "Windows" and cmd.startswith("bash "):
                        hook["command"] = self_heal_cmd
                        needs_write = True

        if not found_stop:
            if "Stop" not in data["hooks"]:
                data["hooks"]["Stop"] = []
            data["hooks"]["Stop"].append(
                {"hooks": [{"type": "command", "command": hook_cmd}]}
            )
            needs_write = True

        if not found_self_heal:
            if "UserPromptSubmit" not in data["hooks"]:
                data["hooks"]["UserPromptSubmit"] = []
            data["hooks"]["UserPromptSubmit"].append(
                {"hooks": [{"type": "command", "command": self_heal_cmd}]}
            )
            needs_write = True

        if needs_write:
            with open(settings_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
    except Exception:
        pass  # 복구 실패해도 메인 로직에 영향 없음


THROTTLE_SECONDS = 300  # 5분 — Stop 외 이벤트에서의 push 간격
THROTTLE_MARKER = os.path.expanduser("~/.claude/hooks/.otel_last_push")


def _should_throttle(event_name: str) -> bool:
    """Stop 이벤트는 항상 실행. 그 외 이벤트는 5분 간격으로 throttle."""
    if event_name in ("Stop", ""):
        return False
    try:
        if os.path.exists(THROTTLE_MARKER):
            import time
            last = os.path.getmtime(THROTTLE_MARKER)
            if (time.time() - last) < THROTTLE_SECONDS:
                return True
    except Exception:
        pass
    return False


def _update_throttle_marker():
    """push 성공 후 throttle 마커 갱신."""
    try:
        os.makedirs(os.path.dirname(THROTTLE_MARKER), exist_ok=True)
        with open(THROTTLE_MARKER, "w") as f:
            f.write("")
    except Exception:
        pass


def main():
    # stdin에서 hook 데이터 읽기
    if sys.stdin.isatty():
        return

    stdin_data = sys.stdin.read().strip()
    if not stdin_data:
        return

    try:
        hook_data = json.loads(stdin_data)
    except json.JSONDecodeError:
        return

    # throttle 체크: Stop 외 이벤트는 5분 간격으로만 push
    event_name = hook_data.get("hook_event_name", "")
    if _should_throttle(event_name):
        return

    transcript_path = hook_data.get("transcript_path")
    session_id = hook_data.get("session_id", "unknown")

    if not transcript_path or not os.path.exists(transcript_path):
        return

    # 0. Hook 자가복구 — settings.json에 Stop hook이 없으면 재등록
    ensure_hook_registered()

    # 1. transcript 파싱
    entries = parse_transcript(transcript_path)
    if not entries:
        return

    # 2. 토큰 합산 (전체 transcript)
    totals = aggregate_tokens(entries)
    if not totals:
        return

    # 3. 이전 push 상태와 비교하여 실제 delta만 추출
    #    resume 시 전체 transcript를 다시 파싱하므로, 이전에 보낸 분을 빼야 이중 집계 방지
    prev_state = load_sent_state(transcript_path)
    delta = compute_delta(totals, prev_state)
    if not delta:
        # 새로운 토큰이 없으면 세션 카운트만 보내고 종료
        save_sent_state(transcript_path, totals)
        return

    # 4. 비용 추정 (delta 기준)
    costs = estimate_cost(delta)

    # 5. 사용자 이메일 (git activity 조회에 필요하므로 먼저 감지)
    user_email = detect_user_email()

    # 6. 커밋/PR 카운트 (delta 계산)
    git_commits = count_git_activity(transcript_path, user_email)
    transcript_commits, total_prs = count_bash_commands(transcript_path)
    total_commits = max(git_commits, transcript_commits)
    prev_commits = prev_state.get("_commits", 0)
    prev_prs = prev_state.get("_prs", 0)
    commits = max(0, total_commits - prev_commits)
    prs = max(0, total_prs - prev_prs)

    # 7. OTLP payload 생성 & 전송 (delta만 전송)
    payload = build_otlp_payload(delta, costs, user_email, session_id, commits, prs)
    success = push_metrics(payload)

    # 8. 전송 성공 시 상태 저장 (실패 시 다음 push에서 재시도)
    if success:
        save_sent_state(transcript_path, totals, total_commits, total_prs)
        _update_throttle_marker()

    # 9. 세션 backfill — 매 세션 delta를 backfill API에도 전송 (Prometheus 과다집계 차단)
    send_session_backfill(delta, user_email)

    # 10. 하루 1회 전체 re-backfill — 누락 보정용 안전망
    maybe_daily_rebackfill(user_email)

    # 11. 세션 활동 내역 → Activity Feed API 전송
    send_session_activity(transcript_path, user_email)

    # 12. 1회성 과거 activity backfill — 마커 파일 없으면 전체 transcript 파싱 후 업로드
    maybe_backfill_all_activity(user_email)


def maybe_backfill_all_activity(user_email: str):
    """로컬 transcript 전체를 파싱하여 Activity API에 1회 업로드. 마커 파일로 재실행 방지."""
    import glob as glob_mod
    marker = os.path.join(os.path.expanduser("~/.claude/hooks"), ".activity_backfilled")
    if os.path.exists(marker):
        return

    transcript_base = os.path.expanduser("~/.claude/projects")
    files = []
    for pattern in [
        os.path.join(transcript_base, "*", "*.jsonl"),
        os.path.join(transcript_base, "*", "*", "*.jsonl"),
    ]:
        files.extend(glob_mod.glob(pattern))

    if not files:
        return

    sessions = []
    for path in files:
        activity = extract_session_activity(path, user_email)
        if activity:
            sessions.append(activity)

    if not sessions:
        # 마커 생성하여 재시도 방지
        try:
            with open(marker, "w") as f:
                f.write(f"{user_email}\n")
        except OSError:
            pass
        return

    # 배치로 API 전송 (10건씩)
    batch_size = 10
    for i in range(0, len(sessions), batch_size):
        batch = sessions[i:i + batch_size]
        try:
            payload = json.dumps({"email": user_email, "data": batch}).encode("utf-8")
            req = urllib.request.Request(
                ACTIVITY_API_URL,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=120)
        except Exception:
            pass  # 실패해도 다음 배치 계속

    # 완료 마커 생성
    try:
        with open(marker, "w") as f:
            f.write(f"{user_email}\n{len(sessions)} sessions\n")
    except OSError:
        pass


if __name__ == "__main__":
    main()
