"""
Generate Backfill JSON - transcript JSONL을 파싱하여 ClaudeCodeDataPoint[] 형태의 JSON 생성.

설치 스크립트에서 자동 호출됨.
출력: stdout에 JSON (install-hook.sh가 파일로 저장)

사용법:
  python3 generate_backfill.py                    # stdout JSON
  python3 generate_backfill.py --out result.json  # 파일로 저장
"""
SCRIPT_VERSION = "2"

import json
import os
import sys
import glob
import subprocess
from collections import defaultdict
from datetime import datetime, timezone, timedelta

KST = timezone(timedelta(hours=9))


def utc_to_kst_date(ts_str: str) -> str:
    """UTC ISO8601 timestamp → KST 날짜 문자열 (YYYY-MM-DD)"""
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        return dt.astimezone(KST).strftime("%Y-%m-%d")
    except (ValueError, AttributeError):
        return ts_str[:10]  # fallback: raw date

TRANSCRIPT_BASE = os.path.expanduser("~/.claude/projects")


def sanitize_email(email: str) -> str:
    """중복 도메인 제거 (예: user@eoeoeo.net@eoeoeo.net → user@eoeoeo.net)"""
    at_count = email.count("@")
    if at_count > 1:
        parts = email.split("@")
        return f"{parts[0]}@{parts[-1]}"
    return email


def detect_user_email() -> str:
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
    files = []
    for pattern in [
        os.path.join(TRANSCRIPT_BASE, "*", "*.jsonl"),
        os.path.join(TRANSCRIPT_BASE, "*", "*", "*.jsonl"),
    ]:
        files.extend(glob.glob(pattern))
    return [f for f in files if "subagents" not in os.path.basename(os.path.dirname(f))]


def parse_transcripts(files):
    results = []
    commits_by_date = defaultdict(int)
    prs_by_date = defaultdict(int)
    sessions_by_date = defaultdict(int)

    for path in files:
        try:
            # 각 transcript 파일 = 1 세션. 첫 날짜 기준으로 세션 카운트
            session_date = None
            current_date = None
            # message ID dedup: 같은 API 호출이 thinking/tool_use/text 등
            # 여러 JSONL 행으로 기록됨. 같은 msg.id는 last-wins로 중복 제거.
            seen = {}  # msg_id → entry
            no_id_entries = []
            with open(path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    ts_str = record.get("timestamp", "")
                    if ts_str:
                        current_date = utc_to_kst_date(ts_str)
                        if session_date is None:
                            session_date = current_date

                    if record.get("type") != "assistant":
                        continue

                    msg = record.get("message", {})
                    model = msg.get("model")
                    usage = msg.get("usage")

                    # Bash 명령 카운트
                    for block in msg.get("content", []):
                        if block.get("type") == "tool_use" and block.get("name") == "Bash":
                            cmd = block.get("input", {}).get("command", "")
                            if "git commit" in cmd and current_date:
                                commits_by_date[current_date] += 1
                            if "gh pr create" in cmd and current_date:
                                prs_by_date[current_date] += 1

                    if not model or not usage or not ts_str:
                        continue

                    entry = {
                        "date": utc_to_kst_date(ts_str),
                        "model": model,
                        "input_tokens": usage.get("input_tokens", 0),
                        "output_tokens": usage.get("output_tokens", 0),
                        "cache_read_tokens": usage.get("cache_read_input_tokens", 0),
                        "cache_creation_tokens": usage.get("cache_creation_input_tokens", 0),
                    }
                    msg_id = msg.get("id")
                    if msg_id:
                        seen[msg_id] = entry  # last-wins dedup
                    else:
                        no_id_entries.append(entry)

            results.extend(seen.values())
            results.extend(no_id_entries)

            if session_date:
                sessions_by_date[session_date] += 1
        except (IOError, OSError):
            pass
    return results, dict(commits_by_date), dict(prs_by_date), dict(sessions_by_date)


def count_git_commits_by_date(email: str, dates: list[str]) -> dict[str, int]:
    """git log 기반 날짜별 실제 커밋 카운트.
    transcript에 기록되지 않은 커밋(터미널/IDE 직접 커밋)도 포함.
    """
    git_commits = {}
    for date in dates:
        try:
            result = subprocess.run(
                ["git", "log", "--oneline", "--all",
                 f"--after={date}T00:00:00", f"--before={date}T23:59:59",
                 f"--author={email}"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                git_commits[date] = len(result.stdout.strip().split("\n"))
        except Exception:
            pass
    return git_commits


def aggregate(entries, email, commits_by_date=None, prs_by_date=None, sessions_by_date=None):
    """date × model 별 합산 → ClaudeCodeDataPoint[] 형태"""
    commits_by_date = commits_by_date or {}
    prs_by_date = prs_by_date or {}
    sessions_by_date = sessions_by_date or {}
    agg = defaultdict(lambda: {
        "input_tokens": 0, "output_tokens": 0,
        "cache_read_tokens": 0, "cache_creation_tokens": 0,
    })

    for e in entries:
        key = (e["date"], e["model"])
        agg[key]["input_tokens"] += e["input_tokens"]
        agg[key]["output_tokens"] += e["output_tokens"]
        agg[key]["cache_read_tokens"] += e["cache_read_tokens"]
        agg[key]["cache_creation_tokens"] += e["cache_creation_tokens"]

    # git log 기반 커밋 카운트 병합 (transcript 기반과 max 사용)
    all_dates = sorted(set(date for date, _ in agg.keys()))
    git_commits_by_date = count_git_commits_by_date(email, all_dates)
    for date in all_dates:
        transcript_val = commits_by_date.get(date, 0)
        git_val = git_commits_by_date.get(date, 0)
        commits_by_date[date] = max(transcript_val, git_val)

    data = []
    seen_dates = set()
    for (date, model), tokens in sorted(agg.items()):
        # 날짜당 첫 번째 모델 항목에만 카운트 배분
        commits = 0
        pull_requests = 0
        session_count = 0
        if date not in seen_dates:
            commits = commits_by_date.get(date, 0)
            pull_requests = prs_by_date.get(date, 0)
            session_count = sessions_by_date.get(date, 0)
            seen_dates.add(date)
        data.append({
            "actor": {
                "type": "user",
                "id": email,
                "email_address": email,
            },
            "model": model,
            "date": date,
            "session_count": session_count,
            "lines_of_code": 0,
            "commits": commits,
            "pull_requests": pull_requests,
            "tool_acceptance_rate": 0,
            **tokens,
        })

    return data


def main():
    email = detect_user_email()
    files = find_transcripts()

    if not files:
        print(json.dumps({"data": []}))
        return

    entries, commits_by_date, prs_by_date, sessions_by_date = parse_transcripts(files)
    if not entries:
        print(json.dumps({"data": []}))
        return

    data = aggregate(entries, email, commits_by_date, prs_by_date, sessions_by_date)

    out_path = None
    for i, arg in enumerate(sys.argv):
        if arg == "--out" and i + 1 < len(sys.argv):
            out_path = sys.argv[i + 1]

    result = json.dumps({"data": data}, ensure_ascii=False)

    if out_path:
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(result)
        dates = sorted(set(d["date"] for d in data))
        print(f"      {email}: {len(data)}개 레코드, {dates[0]} ~ {dates[-1]}")
    else:
        print(result)


if __name__ == "__main__":
    main()
