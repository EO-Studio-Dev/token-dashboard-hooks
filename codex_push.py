"""
Codex Push - ~/.codex/sessions/ JSONL 파싱 → Dashboard API로 전송

Codex CLI 세션 로그를 일별로 집계하여 대시보드에 반영합니다.
install-hook.sh에서 자동 실행되며, 수동으로도 실행 가능합니다.

사용법:
  python3 codex_push.py                         # 파싱 + API 전송
  python3 codex_push.py --dry-run                # 파싱만 (전송 안 함)
  python3 codex_push.py --email ash@eoeoeo.net   # 이메일 지정
"""
SCRIPT_VERSION = "2"

import json
import glob
import os
import sys
import subprocess
import urllib.request
import urllib.error
from collections import defaultdict

SESSIONS_DIR = os.path.expanduser("~/.codex/sessions")
CODEX_BACKFILL_API = "https://token-dashboard-iota.vercel.app/api/codex-backfill"
HOOKS_DIR = os.path.expanduser("~/.claude/hooks")
HOOK_HEALTH_LOCAL = os.path.join(HOOKS_DIR, "hook_health.py")
HOOK_HEALTH_URLS = [
    "https://raw.githubusercontent.com/EO-Studio-Dev/token-dashboard-hooks/main/hook_health.py",
    "https://token-dashboard-iota.vercel.app/api/hook-script?name=hook_health.py",
]


def _download_with_fallback(url: str, dest: str):
    try:
        urllib.request.urlretrieve(url, dest)
        return
    except Exception:
        pass

    result = subprocess.run(
        ["curl", "-fsSL", url, "-o", dest],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "download failed").strip()[:300])


def bootstrap_hook_health():
    """launchd에서 함께 도는 helper가 hook_health.py를 최신으로 끌어올린다."""
    try:
        local_ver = 0
        if os.path.exists(HOOK_HEALTH_LOCAL):
            with open(HOOK_HEALTH_LOCAL, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    if line.startswith("SCRIPT_VERSION"):
                        local_ver = int(line.split("=", 1)[1].strip().strip('"').strip("'"))
                        break

        remote_ver = 0
        remote_url = ""
        for url in HOOK_HEALTH_URLS:
            try:
                req = urllib.request.Request(url)
                with urllib.request.urlopen(req, timeout=5) as resp:
                    for line in resp:
                        decoded = line.decode("utf-8", errors="replace")
                        if decoded.startswith("SCRIPT_VERSION"):
                            remote_ver = int(decoded.split("=", 1)[1].strip().strip('"').strip("'"))
                            remote_url = url
                            break
                        if not decoded.strip() or decoded.startswith("#") or decoded.startswith("from "):
                            continue
                        break
                if remote_ver:
                    break
            except Exception:
                continue

        if remote_ver > local_ver and remote_url:
            os.makedirs(HOOKS_DIR, exist_ok=True)
            _download_with_fallback(remote_url, HOOK_HEALTH_LOCAL)
            os.chmod(HOOK_HEALTH_LOCAL, 0o755)
    except Exception:
        pass


def parse_sessions(sessions_dir: str) -> list[dict]:
    """~/.codex/sessions/ 하위 모든 JSONL 파싱 → 날짜+모델별 집계"""
    daily = defaultdict(lambda: {
        "input_tokens": 0,
        "output_tokens": 0,
        "cached_input_tokens": 0,
        "reasoning_output_tokens": 0,
        "sessions": 0,
        "model": "",
    })

    jsonl_files = glob.glob(os.path.join(sessions_dir, "**", "*.jsonl"), recursive=True)
    if not jsonl_files:
        return []

    for filepath in jsonl_files:
        last_token = None
        model = ""

        try:
            with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    # 모델명 추출
                    if record.get("type") == "turn_context":
                        model = record.get("payload", {}).get("model", "")

                    # 마지막 token_count 이벤트 추적 (누적값)
                    if (record.get("type") == "event_msg"
                            and record.get("payload", {}).get("type") == "token_count"):
                        info = record["payload"].get("info")
                        if info and "total_token_usage" in info:
                            last_token = info["total_token_usage"]
        except (IOError, OSError):
            continue

        if not last_token:
            continue

        # 파일명에서 날짜 추출: rollout-2026-03-08T01-49-37-*.jsonl
        basename = os.path.basename(filepath)
        date = basename.replace("rollout-", "").split("T")[0]
        if len(date) != 10:  # YYYY-MM-DD
            continue

        key = (date, model or "codex-unknown")
        day = daily[key]
        raw_input = last_token.get("input_tokens", 0)
        cached = last_token.get("cached_input_tokens", 0)
        # Codex reports input_tokens inclusive of cached — subtract to get pure input
        day["input_tokens"] += raw_input - cached
        day["output_tokens"] += last_token.get("output_tokens", 0)
        day["cached_input_tokens"] += cached
        day["reasoning_output_tokens"] += last_token.get("reasoning_output_tokens", 0)
        day["sessions"] += 1
        if model:
            day["model"] = model

    return [{"date": date, "model": model, **values} for (date, model), values in sorted(daily.items())]


def count_daily_git_activity(dates: list[str], user_email: str) -> dict[str, dict]:
    """날짜별 git commit/PR 수를 git log에서 카운트.
    Codex가 만든 커밋과 사용자 수동 커밋을 구분할 수 없지만,
    해당 날짜에 Codex 세션이 있었으므로 Codex 기여로 간주."""
    activity = {}
    for date in dates:
        commits = 0
        prs = 0
        try:
            result = subprocess.run(
                ["git", "log", "--oneline",
                 f"--after={date}T00:00:00", f"--before={date}T23:59:59",
                 f"--author={user_email}"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                commits = len(result.stdout.strip().split("\n"))
        except Exception:
            pass
        try:
            result = subprocess.run(
                ["git", "log", "--oneline", "--grep=gh pr create",
                 f"--after={date}T00:00:00", f"--before={date}T23:59:59",
                 f"--author={user_email}"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                prs = len(result.stdout.strip().split("\n"))
        except Exception:
            pass
        activity[date] = {"commits": commits, "pull_requests": prs}
    return activity


def detect_email() -> str:
    """git config에서 이메일 추출"""
    try:
        result = subprocess.run(
            ["git", "config", "user.email"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            email = result.stdout.strip()
            # 중복 도메인 제거
            if email.count("@") > 1:
                parts = email.split("@")
                email = f"{parts[0]}@{parts[-1]}"
            return email
    except Exception:
        pass
    return ""


def push_to_api(email: str, data: list[dict]) -> bool:
    """Dashboard API에 POST"""
    payload = json.dumps({"email": email, "data": data}).encode("utf-8")
    req = urllib.request.Request(
        CODEX_BACKFILL_API,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode())
            if resp.status == 200:
                print(f"  -> 전송 완료: {body.get('file', '')} ({body.get('records', 0)}개 레코드)")
                return True
            else:
                print(f"  -> 전송 실패: {body}")
                return False
    except (urllib.error.URLError, urllib.error.HTTPError) as e:
        print(f"  -> 전송 실패: {e}")
        return False


def main():
    dry_run = "--dry-run" in sys.argv
    bootstrap_hook_health()

    # 이메일 결정
    email = ""
    for i, arg in enumerate(sys.argv):
        if arg == "--email" and i + 1 < len(sys.argv):
            email = sys.argv[i + 1]
    if not email:
        email = detect_email()
    if not email:
        print("[!] 이메일을 감지할 수 없습니다. --email 옵션을 사용하세요.")
        sys.exit(1)

    print(f"사용자: {email}")
    print(f"세션 디렉토리: {SESSIONS_DIR}")

    if not os.path.isdir(SESSIONS_DIR):
        print("  ~/.codex/sessions/ 디렉토리가 없습니다. Codex CLI를 한번 이상 실행해주세요.")
        sys.exit(0)

    # 파싱
    data = parse_sessions(SESSIONS_DIR)
    if not data:
        print("  파싱 가능한 세션 데이터가 없습니다.")
        sys.exit(0)

    # git 활동 수집 (날짜별 커밋/PR)
    dates = [d["date"] for d in data]
    git_activity = count_daily_git_activity(dates, email)
    for d in data:
        act = git_activity.get(d["date"], {})
        d["commits"] = act.get("commits", 0)
        d["pull_requests"] = act.get("pull_requests", 0)

    total_sessions = sum(d["sessions"] for d in data)
    total_tokens = sum(d["input_tokens"] + d["output_tokens"] for d in data)
    total_commits = sum(d["commits"] for d in data)
    total_prs = sum(d["pull_requests"] for d in data)
    print(f"  {len(data)}일, {total_sessions}세션, {total_tokens:,} tokens, {total_commits} commits, {total_prs} PRs")

    if dry_run:
        print(json.dumps({"data": data}, indent=2))
        return

    # API 전송
    push_to_api(email, data)


if __name__ == "__main__":
    main()
