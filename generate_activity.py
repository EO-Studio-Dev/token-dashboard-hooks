from __future__ import annotations

SCRIPT_VERSION = "3"

"""
Generate Activity JSON - transcript JSONL을 파싱하여 세션 단위 ActivitySession[] 형태의 JSON 생성.

Activity Feed 용도. 각 transcript 파일에서 세션 메타데이터(project, summary, tools, tokens 등)를 추출.

사용법:
  python3 generate_activity.py                    # stdout JSON
  python3 generate_activity.py --out result.json  # 파일로 저장
"""

import json
import os
import sys
import glob
import re
import subprocess
import time
import urllib.request
import urllib.error
from collections import defaultdict
from datetime import datetime

TRANSCRIPT_BASE = os.path.expanduser("~/.claude/projects")

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# system-reminder 등 스킵 대상 태그
SKIP_TAGS = (
    "<system-reminder>",
    "<local-command-caveat>",
    "<local-command-stdout>",
    "<command-name>",
    "<command-message>",
    "Base directory for this skill:",
    "Stop hooks aren't triggered",
)

EMAIL_ALIAS = {
    "jobskim@icloud.com": "ty@eoeoeo.net",
}


def console_print(text: str = "") -> None:
    """Windows cp949 콘솔/Task Scheduler에서도 출력 때문에 죽지 않게 한다."""
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    safe = text.encode(encoding, errors="replace").decode(encoding, errors="replace")
    print(safe)


def sanitize_email(email: str) -> str:
    """중복 도메인 제거 + alias 변환"""
    at_count = email.count("@")
    if at_count > 1:
        parts = email.split("@")
        email = f"{parts[0]}@{parts[-1]}"
    normalized = email.lower()
    return EMAIL_ALIAS.get(normalized, normalized)


def detect_user_email() -> str:
    # .otel_email 우선 (토큰 push와 동일 경로 — push_recent_transcripts와 일치)
    otel_email_path = os.path.expanduser("~/.claude/hooks/.otel_email")
    try:
        if os.path.exists(otel_email_path):
            with open(otel_email_path, "r", encoding="utf-8") as f:
                email = f.read().strip()
            if email:
                return sanitize_email(email)
    except Exception:
        pass
    # fallback: git config
    try:
        result = subprocess.run(
            ["git", "config", "user.email"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return sanitize_email(result.stdout.strip())
    except Exception:
        pass
    return "unknown"


def find_transcripts():
    """~/.claude/projects 아래 모든 .jsonl 파일 찾기 (subagents 폴더 제외)"""
    files = []
    for pattern in [
        os.path.join(TRANSCRIPT_BASE, "*", "*.jsonl"),
        os.path.join(TRANSCRIPT_BASE, "*", "*", "*.jsonl"),
    ]:
        files.extend(glob.glob(pattern))
    return [f for f in files if "subagents" not in os.path.basename(os.path.dirname(f))]


def extract_project_from_cwd(cwd: str) -> str:
    """cwd에서 마지막 의미있는 디렉토리명 추출 (윈도우 백슬래시 대응)"""
    if not cwd:
        return "unknown"
    # 윈도우 백슬래시 → 슬래시 통일
    normalized = cwd.replace("\\", "/").rstrip("/")
    parts = normalized.split("/")
    # 마지막 비어있지 않은 부분
    for part in reversed(parts):
        if part:
            return part
    return "unknown"


def extract_user_message_text(content) -> str | None:
    """user message content에서 텍스트 추출. 문자열이면 그대로, 배열이면 text type 블록 추출."""
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


def should_skip_message(text: str) -> bool:
    """system-reminder 등 태그 콘텐츠가 포함된 메시지인지 확인"""
    stripped = text.strip()
    for tag in SKIP_TAGS:
        if stripped.startswith(tag):
            return True
    return False


def _extract_commit_message(cmd: str) -> str | None:
    """git commit 명령에서 커밋 메시지 추출"""
    if "git commit" not in cmd:
        return None
    # HEREDOC 패턴: -m "$(cat <<'EOF'\n실제 메시지\n..." → 첫 실제 줄 추출
    m = re.search(r"<<'?EOF'?\s*\n(.+?)(?:\n|$)", cmd)
    if m:
        msg = m.group(1).strip()
        if msg and not msg.startswith("Co-Authored"):
            return msg[:80]
    # 일반 패턴: -m "message" 또는 -m 'message' (HEREDOC/치환 제외)
    m = re.search(r'-m\s+["\']([^$].+?)["\']', cmd)
    if m:
        return m.group(1)[:80]
    return None


def _infer_work_type(files_changed: list[str], tools: dict) -> str:
    """파일 경로 + 도구 패턴으로 작업 유형 자동 분류"""
    files_lower = [f.lower().replace("\\", "/") for f in files_changed]
    tool_set = set(tools.keys())
    total_tool_uses = sum(tools.values())

    # 파일 경로 패턴
    has_component = any("/component" in f or ".tsx" in f or ".jsx" in f for f in files_lower)
    has_api = any("/api/" in f or "route.ts" in f for f in files_lower)
    has_script = any("/script" in f or f.endswith(".py") or f.endswith(".sh") for f in files_lower)
    has_doc = any(f.endswith(".md") or "readme" in f or "claude" in f for f in files_lower)
    has_config = any(f.endswith(".json") or f.endswith(".yaml") or f.endswith(".yml") or f.endswith(".toml") for f in files_lower)
    has_style = any(".css" in f or "tailwind" in f for f in files_lower)
    has_test = any("test" in f or "spec" in f for f in files_lower)

    # 도구 패턴
    edit_ratio = (tools.get("Edit", 0) + tools.get("Write", 0)) / max(total_tool_uses, 1)
    read_ratio = (tools.get("Read", 0) + tools.get("Grep", 0) + tools.get("Glob", 0)) / max(total_tool_uses, 1)
    bash_ratio = tools.get("Bash", 0) / max(total_tool_uses, 1)
    has_web = bool(tool_set & {"WebSearch", "WebFetch"})
    has_mcp = any(k.startswith("mcp__") for k in tools)

    # 분류 (우선순위 순)
    if has_test:
        return "testing"
    if has_component and has_style:
        return "ui"
    if has_component:
        return "ui"
    if has_api:
        return "api"
    if has_script:
        return "scripting"
    if has_doc and edit_ratio > 0.3:
        return "docs"
    if has_config and bash_ratio > 0.3:
        return "devops"
    if has_web or has_mcp:
        return "research"
    if read_ratio > 0.6:
        return "research"
    if edit_ratio > 0.3:
        return "coding"
    if bash_ratio > 0.5:
        return "devops"
    return "coding"


WORK_TYPE_LABELS = {
    "ui": "UI",
    "api": "API",
    "scripting": "Script",
    "research": "Research",
    "devops": "DevOps",
    "docs": "Docs",
    "coding": "Coding",
    "testing": "Testing",
}

KNOWN_PROJECT_SLUGS = [
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

PROJECT_LABELS = {
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


def _truncate_summary(text: str, max_len: int = 60) -> str:
    clean = re.sub(r"\s+", " ", text).strip()
    if len(clean) <= max_len:
        return clean
    return clean[: max_len - 3].rstrip() + "..."


def _strip_session_wrap_prefix(message: str) -> str:
    message = re.sub(r"^session wrap:\s*\d{4}-\d{2}-\d{2}\s*[—-]\s*", "", message, flags=re.I)
    return message.strip()


def _humanize_basename(name: str) -> str:
    return re.sub(r"[-_]+", " ", re.sub(r"\.[^.]+$", "", name)).strip()


def _resolve_project_label(raw: str) -> str:
    raw = (raw or "unknown").strip()
    return PROJECT_LABELS.get(raw) or PROJECT_LABELS.get(raw.lower()) or _humanize_basename(raw).title()


def _has_korean(text: str) -> bool:
    return bool(re.search(r"[가-힣]", text))


def _is_low_signal_summary(text: str) -> bool:
    if not text or not text.strip():
        return True
    value = text.strip()
    if _has_korean(value):
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


def _is_tool_driven_text(text: str) -> bool:
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


def _infer_project_label(project: str, files_changed: list[str]) -> str:
    normalized = [f.replace("\\", "/").lower() for f in files_changed]
    for slug in KNOWN_PROJECT_SLUGS:
        if any(f"/{slug}/" in path for path in normalized):
            return _resolve_project_label(slug)
    return _resolve_project_label(project)


def _infer_project_focus(files_changed: list[str], work_type: str, tools: dict) -> str:
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


def _build_project_summary(project_label: str, subject: str) -> str:
    return _truncate_summary(f"{project_label}: {subject}", 72)


def _build_heuristic_ai_summary(project: str, work_type: str, commit_messages: list[str],
                                files_changed: list[str], tools: dict) -> str:
    project_label = _infer_project_label(project, files_changed)
    cleaned_commits = [_strip_session_wrap_prefix(msg) for msg in commit_messages if msg]
    for msg in cleaned_commits:
        if not _is_tool_driven_text(msg):
            return _build_project_summary(project_label, msg)
    return _build_project_summary(project_label, _infer_project_focus(files_changed, work_type, tools))


def _generate_haiku_summary(project: str, work_type: str, commit_messages: list[str],
                             files_changed: list[str], tools: dict) -> str:
    """Claude Haiku로 1줄 자연어 요약 생성. 실패 시 빈 문자열."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    fallback = _build_heuristic_ai_summary(project, work_type, commit_messages, files_changed, tools)
    if not api_key:
        return fallback

    # 요약용 컨텍스트 (최소한의 정보만)
    project_label = _infer_project_label(project, files_changed)
    context_parts = [f"Project: {project_label}", f"Type: {WORK_TYPE_LABELS.get(work_type, work_type)}"]
    if commit_messages:
        context_parts.append(f"Commits: {'; '.join(commit_messages[:3])}")
    if files_changed:
        unique = list(dict.fromkeys(files_changed))[:10]
        context_parts.append(f"Files: {', '.join(os.path.basename(f) for f in unique)}")
    top_tools = sorted(tools.items(), key=lambda x: -x[1])[:5]
    if top_tools:
        context_parts.append(f"Tools: {', '.join(f'{n} {c}x' for n, c in top_tools)}")

    prompt = f"""Summarize this session in English as '<Project>: <workstream>' in one short line.
Start with the project or product name. Focus on what moved forward, not tools used.

{chr(10).join(context_parts)}

Summary ('<Project>: <workstream>'):"""

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
            text = result.get("content", [{}])[0].get("text", "").strip()
            candidate = _truncate_summary(text.strip('"\'').strip())
            return fallback if _is_low_signal_summary(candidate) else candidate
    except Exception:
        return fallback


def _build_auto_summary(commit_messages: list[str], files_changed: list[str], tools: dict) -> str:
    """커밋 메시지 + 변경 파일 + 도구 패턴으로 자동 요약 생성"""
    parts = []

    # 1. 커밋 메시지 (첫 번째만)
    if commit_messages:
        parts.append(commit_messages[0])

    # 2. 변경 파일 요약
    if files_changed:
        unique = list(dict.fromkeys(files_changed))  # 순서 유지 중복 제거
        if len(unique) <= 3:
            parts.append(", ".join(os.path.basename(f) for f in unique))
        else:
            parts.append(f"{len(unique)} files edited")

    # 3. 도구 패턴 (커밋/파일 정보 없을 때만)
    if not parts and tools:
        top = sorted(tools.items(), key=lambda x: -x[1])[:3]
        parts.append(" + ".join(f"{name} x{count}" for name, count in top))

    return " · ".join(parts) if parts else ""


def parse_single_transcript(path: str) -> dict | None:
    """단일 transcript 파일에서 ActivitySession 데이터 추출"""
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

    # message ID dedup for token counting
    seen = {}  # msg_id -> {input_tokens, output_tokens}
    no_id_entries = []

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue

                ts_str = record.get("timestamp", "")

                # 첫 record에서 sessionId, cwd 추출
                if session_id is None and record.get("sessionId"):
                    session_id = record["sessionId"]
                if cwd is None and record.get("cwd"):
                    cwd = record["cwd"]

                # timestamp 추적
                if ts_str:
                    if first_timestamp is None:
                        first_timestamp = ts_str
                    last_timestamp = ts_str

                # assistant message 처리
                if record.get("type") == "assistant":
                    msg = record.get("message", {})
                    model = msg.get("model")
                    usage = msg.get("usage")

                    if model:
                        models.add(model)

                    # tool_use 블록에서 도구명 카운트 + 파일/커밋 추출
                    for block in msg.get("content", []):
                        if block.get("type") == "tool_use":
                            tool_name = block.get("name", "")
                            if tool_name:
                                tools[tool_name] += 1
                            inp = block.get("input", {})
                            # Edit/Write → 변경 파일 수집
                            if tool_name in ("Edit", "Write") and inp.get("file_path"):
                                files_changed.append(inp["file_path"])
                            # Bash → git commit 메시지 / PR 카운트
                            if tool_name == "Bash":
                                cmd = inp.get("command", "")
                                if "git commit" in cmd:
                                    commits += 1
                                    cm = _extract_commit_message(cmd)
                                    if cm:
                                        commit_messages.append(cm)
                                if "gh pr create" in cmd:
                                    pull_requests += 1

                    # 토큰 집계 (message ID dedup)
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

    # 토큰 합산
    total_input = 0
    total_output = 0
    for entry in list(seen.values()) + no_id_entries:
        total_input += entry["input_tokens"]
        total_output += entry["output_tokens"]

    # duration 계산
    duration_minutes = 0
    if first_timestamp and last_timestamp:
        try:
            dt_first = datetime.fromisoformat(first_timestamp.replace("Z", "+00:00"))
            dt_last = datetime.fromisoformat(last_timestamp.replace("Z", "+00:00"))
            delta_seconds = (dt_last - dt_first).total_seconds()
            duration_minutes = round(delta_seconds / 60, 1)
        except (ValueError, AttributeError):
            pass

    project = extract_project_from_cwd(cwd) if cwd else "unknown"
    tools_dict = dict(tools)
    unique_files = list(dict.fromkeys(files_changed))
    summary = _build_auto_summary(commit_messages, files_changed, tools_dict)
    work_type = _infer_work_type(files_changed, tools_dict)
    ai_summary = _generate_haiku_summary(project, work_type, commit_messages, unique_files, tools_dict)

    return {
        "id": session_id or os.path.basename(path).replace(".jsonl", ""),
        "cwd": cwd or "",
        "project": project,
        "timestamp": first_timestamp,
        "summary": summary,
        "aiSummary": ai_summary,
        "workType": work_type,
        "commitMessages": commit_messages[:5],
        "filesChanged": unique_files[:20],
        "models": sorted(models),
        "tools": tools_dict,
        "totalInputTokens": total_input,
        "totalOutputTokens": total_output,
        "commits": commits,
        "pullRequests": pull_requests,
        "durationMinutes": duration_minutes,
    }


ACTIVITY_API_URL = "https://token-dashboard-iota.vercel.app/api/activity"

# ── hook_health.py piggyback 업데이트 ──
# 구버전 hook_health.py는 MANAGED_SCRIPTS에 자기 자신이 없어 자동 업데이트 불가.
# generate_activity.py는 구버전에도 MANAGED_SCRIPTS에 포함 → 여기서 간접 업데이트.
_HOOKS_DIR = os.path.expanduser("~/.claude/hooks")
_HH_LOCAL = os.path.join(_HOOKS_DIR, "hook_health.py")
_HH_REMOTE = "https://raw.githubusercontent.com/EO-Studio-Dev/token-dashboard-hooks/main/hook_health.py"


def _piggyback_update_hook_health():
    """hook_health.py 로컬 버전이 낮으면 자동 다운로드. 하루 1회만 체크."""
    marker = os.path.join(_HOOKS_DIR, ".hh_piggyback_last")
    try:
        if os.path.exists(marker):
            age = time.time() - os.path.getmtime(marker)
            if age < 86400:
                return
    except Exception:
        pass

    try:
        # 로컬 버전 읽기
        local_ver = 0
        if os.path.exists(_HH_LOCAL):
            with open(_HH_LOCAL, "r", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("SCRIPT_VERSION"):
                        local_ver = int(line.split("=")[1].strip().strip('"').strip("'"))
                        break

        # 원격 첫 15줄에서 버전 읽기
        req = urllib.request.Request(_HH_REMOTE, headers={"Range": "bytes=0-1024"})
        resp = urllib.request.urlopen(req, timeout=10)
        head = resp.read().decode("utf-8", errors="replace")
        remote_ver = 0
        for line in head.splitlines()[:15]:
            if line.startswith("SCRIPT_VERSION"):
                remote_ver = int(line.split("=")[1].strip().strip('"').strip("'"))
                break

        if remote_ver > local_ver:
            urllib.request.urlretrieve(_HH_REMOTE, _HH_LOCAL)
            os.chmod(_HH_LOCAL, 0o755)
            console_print(f"[piggyback] hook_health.py v{local_ver}→v{remote_ver} 업데이트")

        # 마커 갱신
        with open(marker, "w") as f:
            f.write("")
    except Exception:
        pass  # 실패해도 기존 hook_health.py로 정상 동작
ACTIVITY_PUSH_SECRET = os.environ.get("ACTIVITY_PUSH_SECRET", "")
BATCH_SIZE = 15  # API 1회 호출당 세션 수 (서버 AI 요약 타임아웃 방지)
MAX_BATCH_RETRIES = 3


def push_to_api(email: str, sessions: list[dict]) -> tuple[int, int]:
    """Activity API에 세션 데이터를 배치로 POST. (성공, 실패) 건수 반환."""
    success = 0
    failed = 0
    headers = {"Content-Type": "application/json"}
    if ACTIVITY_PUSH_SECRET:
        headers["x-activity-secret"] = ACTIVITY_PUSH_SECRET
    for i in range(0, len(sessions), BATCH_SIZE):
        batch = sessions[i:i + BATCH_SIZE]
        payload = json.dumps({"email": email, "data": batch}).encode("utf-8")
        req = urllib.request.Request(
            ACTIVITY_API_URL,
            data=payload,
            headers=headers,
            method="POST",
        )
        batch_ok = False
        last_error = ""
        for attempt in range(1, MAX_BATCH_RETRIES + 1):
            try:
                resp = urllib.request.urlopen(req, timeout=120)
                result = json.loads(resp.read())
                if result.get("ok"):
                    success += len(batch)
                    console_print(f"  ✓ {success}/{len(sessions)} 업로드 완료")
                    batch_ok = True
                    break
                last_error = f"unexpected response: {result}"
            except urllib.error.HTTPError as e:
                try:
                    body = e.read().decode("utf-8", errors="replace")[:200]
                except Exception:
                    body = ""
                last_error = f"HTTP {e.code} {body}".strip()
            except Exception as e:
                last_error = str(e)

            if attempt < MAX_BATCH_RETRIES:
                sleep_sec = 2 * attempt
                console_print(f"  ! 배치 재시도 {attempt}/{MAX_BATCH_RETRIES - 1}: {last_error}")
                time.sleep(sleep_sec)

        if not batch_ok:
            failed += len(batch)
            console_print(f"  ✗ 배치 실패: {last_error}")
        # 배치 간 간격 (Vercel rate limit + 서버 부하 방지)
        if i + BATCH_SIZE < len(sessions):
            time.sleep(2)
    return success, failed


def main():
    is_push = "--push" in sys.argv

    # hook_health.py 구버전 자동 업데이트 (--push 모드 = launchd 30분 스케줄)
    if is_push:
        _piggyback_update_hook_health()

    email = detect_user_email()
    files = find_transcripts()

    if not files:
        if is_push:
            console_print(f"⚠ {email}: transcript 파일 없음")
            sys.exit(1)
        print(json.dumps({"data": []}))
        return

    sessions = []
    for path in files:
        session = parse_single_transcript(path)
        if session:
            session["email"] = email
            sessions.append(session)

    # timestamp 기준 정렬 (최신 먼저)
    sessions.sort(key=lambda s: s.get("timestamp", ""), reverse=True)

    # --push: API로 직접 업로드
    if is_push:
        if not sessions:
            console_print(f"⚠ {email}: 파싱된 세션 0건 (파일 {len(files)}개)")
            sys.exit(1)
        console_print(f"📤 {email}: {len(sessions)}개 세션 → Activity API 업로드 중...")
        ok, fail = push_to_api(email, sessions)
        console_print(f"✅ 완료: {ok}건 성공, {fail}건 실패")
        if fail > 0 or ok == 0:
            sys.exit(1)
        return

    out_path = None
    for i, arg in enumerate(sys.argv):
        if arg == "--out" and i + 1 < len(sys.argv):
            out_path = sys.argv[i + 1]

    result = json.dumps({"data": sessions}, ensure_ascii=False)

    if out_path:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(result)
        console_print(f"      {email}: {len(sessions)}개 세션")
    else:
        print(result)


if __name__ == "__main__":
    main()
