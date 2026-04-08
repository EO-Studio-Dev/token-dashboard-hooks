"""
Gemini Push - ~/.gemini/tmp/*/chats/session-*.json 파싱 → Dashboard API로 전송

Gemini CLI 세션 로그를 일별로 집계하여 대시보드에 반영합니다.
install-hook.sh에서 자동 실행되며, 수동으로도 실행 가능합니다.

사용법:
  python3 gemini_push.py                         # 파싱 + API 전송
  python3 gemini_push.py --dry-run                # 파싱만 (전송 안 함)
  python3 gemini_push.py --email ash@eoeoeo.net   # 이메일 지정
"""
from __future__ import annotations
SCRIPT_VERSION = "2"

import json
import glob
import os
import sys
import subprocess
import urllib.request
import urllib.error
from collections import defaultdict

GEMINI_TMP = os.path.expanduser("~/.gemini/tmp")
BACKFILL_API = "https://token-dashboard-iota.vercel.app/api/backfill"
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


def parse_sessions(base_dir: str) -> list:
    """~/.gemini/tmp/*/chats/session-*.json 파싱 → 날짜+모델별 집계"""
    daily = defaultdict(lambda: {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,  # Gemini thoughts tokens
        "session_count": 0,
        "model": "",
    })

    session_files = glob.glob(os.path.join(base_dir, "*/chats/session-*.json"))
    if not session_files:
        return []

    session_dates = set()  # track unique sessions per date for session_count

    for filepath in session_files:
        try:
            with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                data = json.load(f)
        except (json.JSONDecodeError, IOError, OSError):
            continue

        session_start = data.get("startTime", "")

        # per-message timestamp 기반 날짜 할당 (자정 넘김 대응)
        for msg in data.get("messages", []):
            if msg.get("type") != "gemini":
                continue
            tokens = msg.get("tokens", {})
            if not tokens:
                continue

            msg_input = tokens.get("input", 0)
            msg_output = tokens.get("output", 0)
            msg_cached = tokens.get("cached", 0)
            msg_thoughts = tokens.get("thoughts", 0)

            if msg_input + msg_output == 0:
                continue

            # 메시지 자체 timestamp 우선, 없으면 세션 startTime fallback
            msg_ts = msg.get("timestamp", "") or session_start
            date = _to_kst_date(msg_ts)
            if not date:
                continue

            model = msg.get("model", "") or "gemini-unknown"
            key = (date, model)
            day = daily[key]
            # Gemini input은 cached 포함 — Claude backfill 형식에 맞춰 분리
            day["input_tokens"] += msg_input - msg_cached
            day["output_tokens"] += msg_output
            day["cache_read_tokens"] += msg_cached
            day["cache_creation_tokens"] += msg_thoughts
            if model:
                day["model"] = model

            # 세션 카운트: 같은 파일+날짜는 1세션으로
            session_key = (filepath, date)
            if session_key not in session_dates:
                session_dates.add(session_key)
                day["session_count"] += 1

    result = []
    for (date, model), values in sorted(daily.items()):
        result.append({
            "date": date,
            "model": model,
            "input_tokens": values["input_tokens"],
            "output_tokens": values["output_tokens"],
            "cache_read_tokens": values["cache_read_tokens"],
            "cache_creation_tokens": values["cache_creation_tokens"],
            "session_count": values["session_count"],
        })
    return result


def _to_kst_date(iso_str: str) -> str:
    """ISO 8601 UTC → KST 날짜 (YYYY-MM-DD)"""
    try:
        from datetime import datetime, timedelta, timezone
        # 2026-03-26T15:16:35.441Z
        ts = iso_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(ts)
        kst = dt.astimezone(timezone(timedelta(hours=9)))
        return kst.strftime("%Y-%m-%d")
    except Exception:
        # fallback: 날짜 부분만 추출
        return iso_str[:10] if len(iso_str) >= 10 else ""


def detect_email() -> str:
    """otel_email 또는 git config에서 이메일 추출"""
    otel_path = os.path.expanduser("~/.claude/hooks/.otel_email")
    try:
        if os.path.exists(otel_path):
            with open(otel_path, "r", encoding="utf-8") as f:
                email = f.read().strip()
            if email:
                return email
    except Exception:
        pass
    try:
        result = subprocess.run(
            ["git", "config", "user.email"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass
    return ""


def push_to_api(email: str, data: list) -> bool:
    """Dashboard backfill API에 POST"""
    # backfill API 형식: {email, data: [{date, model, input_tokens, ...}]}
    # actor 필드 추가 (data-source.ts가 actor.email_address로 유저 식별)
    for entry in data:
        entry["actor"] = {"type": "user", "id": email, "email_address": email}

    payload = json.dumps({"email": email, "data": data}).encode("utf-8")
    req = urllib.request.Request(
        BACKFILL_API,
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
    print(f"세션 디렉토리: {GEMINI_TMP}")

    if not os.path.isdir(GEMINI_TMP):
        print("  ~/.gemini/tmp/ 디렉토리가 없습니다. Gemini CLI를 한번 이상 실행해주세요.")
        sys.exit(0)

    data = parse_sessions(GEMINI_TMP)
    if not data:
        print("  파싱 가능한 세션 데이터가 없습니다.")
        sys.exit(0)

    total_sessions = sum(d["session_count"] for d in data)
    total_tokens = sum(d["input_tokens"] + d["output_tokens"] for d in data)
    total_cached = sum(d["cache_read_tokens"] for d in data)
    print(f"  {len(data)}일, {total_sessions}세션, {total_tokens:,} tokens (cached: {total_cached:,})")

    if dry_run:
        print(json.dumps({"email": email, "data": data}, indent=2, ensure_ascii=False))
        return

    push_to_api(email, data)


if __name__ == "__main__":
    main()
