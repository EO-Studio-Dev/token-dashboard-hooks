"""
Hook Health Check — 30분마다 실행 (macOS: launchd, Windows: Task Scheduler, Linux: cron)
transcript 스캔으로 토큰 사용량을 수집하고, Codex/Activity 데이터를 push한다.
Stop hook은 세션 블로킹 문제로 제거됨 — launchd 스캔이 유일한 수집 경로.

macOS에서는 cron → launchd 자동 마이그레이션 포함.
"""
SCRIPT_VERSION = "2"

import json
import hashlib
import os
import platform
import subprocess
import sys
import urllib.request
import urllib.error
from typing import List, Optional

SETTINGS_PATH = os.path.expanduser("~/.claude/settings.json")
HOOKS_DIR = os.path.expanduser("~/.claude/hooks")
HOOK_FILE = os.path.join(HOOKS_DIR, "otel_push.py")
RECENT_BACKFILL_STATE_DIR = os.path.join(HOOKS_DIR, ".recent_backfill_sent")
BASE_URL = "https://raw.githubusercontent.com/EO-Studio-Dev/token-dashboard-hooks/main"

IS_WINDOWS = platform.system() == "Windows"
IS_MACOS = platform.system() == "Darwin"

LAUNCHD_LABEL = "net.eoeoeo.hook-health"
LAUNCHD_PLIST_PATH = os.path.expanduser(f"~/Library/LaunchAgents/{LAUNCHD_LABEL}.plist")


def cleanup_legacy_hooks():
    """settings.json에서 레거시 hook 정리:
    1. otel_push Stop/UserPromptSubmit hook 제거 (launchd 스캔으로 대체)
    2. curl 포함된 self-heal hook → 로컬 전용으로 교체 (보안 경고 방지)"""
    if not os.path.exists(SETTINGS_PATH):
        return []
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, IOError, OSError):
        return []

    removed = []
    changed = False
    hooks = data.get("hooks", {})

    # 1. otel_push Stop hook 제거
    for event_type in ["Stop"]:
        entries = hooks.get(event_type, [])
        cleaned = []
        for entry in entries:
            entry_hooks = entry.get("hooks", [])
            filtered = [h for h in entry_hooks if "otel_push" not in h.get("command", "")]
            if filtered:
                entry["hooks"] = filtered
                cleaned.append(entry)
            elif entry_hooks:
                removed.append(f"{event_type} hook")
                changed = True
        if cleaned != entries:
            hooks[event_type] = cleaned

    # 2. UserPromptSubmit: curl 포함 hook → 로컬 전용으로 교체
    LOCAL_SELF_HEAL_CMD = "bash -lc '(python3 ~/.claude/hooks/hook_health.py --self-heal >/dev/null 2>&1 &) >/dev/null 2>&1'"
    ups_entries = hooks.get("UserPromptSubmit", [])
    new_ups = []
    has_self_heal = False
    for entry in ups_entries:
        entry_hooks = entry.get("hooks", [])
        new_entry_hooks = []
        for h in entry_hooks:
            cmd = h.get("command", "")
            if "otel_push" in cmd:
                removed.append("UserPromptSubmit otel_push")
                changed = True
                continue
            if "hook_health.py --self-heal" in cmd:
                if "curl" in cmd:
                    # curl 포함된 레거시 → 로컬 전용으로 교체
                    h = {"type": "command", "command": LOCAL_SELF_HEAL_CMD}
                    removed.append("self-heal curl 제거")
                    changed = True
                has_self_heal = True
            new_entry_hooks.append(h)
        if new_entry_hooks:
            entry["hooks"] = new_entry_hooks
            new_ups.append(entry)
    if not has_self_heal:
        new_ups.append({"hooks": [{"type": "command", "command": LOCAL_SELF_HEAL_CMD}]})
        changed = True
    hooks["UserPromptSubmit"] = new_ups

    if changed:
        try:
            with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except (IOError, OSError):
            pass
    return removed


def check_otel_script() -> bool:
    """otel_push.py 파일이 존재하는지 확인"""
    return os.path.exists(HOOK_FILE)


def download_otel_script():
    """otel_push.py를 GitHub에서 다운로드"""
    os.makedirs(HOOKS_DIR, exist_ok=True)
    url = f"{BASE_URL}/otel_push.py"
    try:
        urllib.request.urlretrieve(url, HOOK_FILE)
        os.chmod(HOOK_FILE, 0o755)
    except Exception:
        pass


# --- 범용 스크립트 자동 업데이트 ---

MANAGED_SCRIPTS = ["otel_push.py", "codex_push.py", "gemini_push.py",
                   "generate_activity.py", "hook_health.py", "generate_backfill.py"]
VERSION_CHECK_MARKER = os.path.join(HOOKS_DIR, ".version_check_last")
VERSION_CHECK_INTERVAL = 86400  # 24시간에 한 번만 원격 체크


def _should_check_versions() -> bool:
    """하루에 한 번만 원격 버전 체크 (네트워크 부담 최소화)"""
    try:
        if os.path.exists(VERSION_CHECK_MARKER):
            import time
            mtime = os.path.getmtime(VERSION_CHECK_MARKER)
            if time.time() - mtime < VERSION_CHECK_INTERVAL:
                return False
    except Exception:
        pass
    return True


def _touch_version_check_marker():
    try:
        with open(VERSION_CHECK_MARKER, "w") as f:
            f.write("")
    except Exception:
        pass


def _read_local_version(script_name: str) -> str:
    """로컬 스크립트의 SCRIPT_VERSION 읽기"""
    path = os.path.join(HOOKS_DIR, script_name)
    if not os.path.exists(path):
        return ""
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("SCRIPT_VERSION"):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
                # docstring, import, 빈줄은 skip
                if not line.strip() or line.startswith("#") or line.startswith("from ") \
                        or line.startswith("import ") or line.startswith('"""') or line.startswith("'''"):
                    continue
                if line.startswith("SCRIPT_VERSION"):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return ""


def _read_remote_version(script_name: str) -> str:
    """GitHub에서 스크립트 첫 부분만 읽어 SCRIPT_VERSION 확인"""
    url = f"{BASE_URL}/{script_name}"
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=5) as resp:
            lines_read = 0
            for line in resp:
                lines_read += 1
                if lines_read > 30:  # 상단 30줄 이내에 없으면 포기
                    break
                decoded = line.decode("utf-8", errors="replace")
                if decoded.startswith("SCRIPT_VERSION"):
                    return decoded.split("=", 1)[1].strip().strip('"').strip("'")
    except Exception:
        pass
    return ""


def ensure_all_scripts_updated() -> list:
    """모든 managed 스크립트의 버전을 체크하고, 구버전이면 자동 업데이트.
    하루에 한 번만 원격 체크하여 네트워크 부담 최소화."""
    if not _should_check_versions():
        return []
    _touch_version_check_marker()

    updated = []
    for script_name in MANAGED_SCRIPTS:
        local_path = os.path.join(HOOKS_DIR, script_name)
        local_ver = _read_local_version(script_name)

        if not local_ver and not os.path.exists(local_path):
            # 파일 자체가 없으면 다운로드
            try:
                os.makedirs(HOOKS_DIR, exist_ok=True)
                urllib.request.urlretrieve(f"{BASE_URL}/{script_name}", local_path)
                os.chmod(local_path, 0o755)
                updated.append(f"{script_name} 신규 설치")
            except Exception:
                pass
            continue

        if not local_ver:
            # 파일은 있지만 SCRIPT_VERSION 없음 = 구버전 확정 → 무조건 업데이트
            try:
                urllib.request.urlretrieve(f"{BASE_URL}/{script_name}", local_path)
                os.chmod(local_path, 0o755)
                updated.append(f"{script_name} 구버전→최신 업데이트")
            except Exception:
                pass
            continue

        # 버전 비교
        try:
            remote_ver = _read_remote_version(script_name)
            if remote_ver and remote_ver > local_ver:
                urllib.request.urlretrieve(f"{BASE_URL}/{script_name}", local_path)
                os.chmod(local_path, 0o755)
                updated.append(f"{script_name} v{local_ver}→v{remote_ver}")
        except Exception:
            pass  # 업데이트 실패해도 기존 파일로 동작

    return updated


def get_email() -> str:
    """저장된 이메일 읽기 (~/.claude/hooks/.otel_email)"""
    email_file = os.path.join(HOOKS_DIR, ".otel_email")
    if os.path.exists(email_file):
        try:
            with open(email_file, "r") as f:
                return f.read().strip()
        except (IOError, OSError):
            pass
    return ""


def build_launchd_plist(email: str) -> str:
    """macOS launchd plist XML 생성"""
    codex_push = os.path.join(HOOKS_DIR, "codex_push.py")
    gemini_push = os.path.join(HOOKS_DIR, "gemini_push.py")
    hook_health = os.path.join(HOOKS_DIR, "hook_health.py")
    # 30분마다 실행: 헬스체크 → transcript 스캔 → Codex 수집 → Gemini 수집
    # 로컬 파일만 실행 — 네트워크 접근 없음 (보안 경고 방지)
    # 스크립트 업데이트는 self-heal의 ensure_*() 함수가 버전 체크 후 수행
    script = (
        f'python3 {hook_health}; '
        f'python3 {codex_push} --email {email}; '
        f'python3 {gemini_push} --email {email}'
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{LAUNCHD_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>-c</string>
        <string>{script}</string>
    </array>
    <key>StartInterval</key>
    <integer>1800</integer>
    <key>RunAtLoad</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{HOOKS_DIR}/launchd.log</string>
    <key>StandardErrorPath</key>
    <string>{HOOKS_DIR}/launchd.log</string>
</dict>
</plist>
"""


def is_launchd_active() -> bool:
    """launchd에 이미 등록되어 있는지 확인"""
    if not IS_MACOS:
        return False
    try:
        result = subprocess.run(
            ["launchctl", "list", LAUNCHD_LABEL],
            capture_output=True, text=True
        )
        return result.returncode == 0
    except (FileNotFoundError, OSError):
        return False


def install_launchd(email: str) -> bool:
    """launchd plist 생성 및 등록"""
    try:
        plist_dir = os.path.dirname(LAUNCHD_PLIST_PATH)
        os.makedirs(plist_dir, exist_ok=True)
        plist_content = build_launchd_plist(email)
        with open(LAUNCHD_PLIST_PATH, "w") as f:
            f.write(plist_content)
        # 기존 등록 해제 (있으면)
        subprocess.run(
            ["launchctl", "unload", LAUNCHD_PLIST_PATH],
            capture_output=True
        )
        # 새로 등록
        result = subprocess.run(
            ["launchctl", "load", LAUNCHD_PLIST_PATH],
            capture_output=True, text=True
        )
        return result.returncode == 0
    except (IOError, OSError):
        return False


TARGET_INTERVAL = 1800  # 30분


def sync_plist_interval() -> list:
    """기존 plist의 StartInterval + ProgramArguments를 최신값으로 자동 동기화.
    GitHub push만으로 기존 팀원의 launchd 설정이 변경되지 않는 문제 해결.
    - StartInterval이 다르면 재작성
    - curl이 plist에 남아있으면 재작성 (로컬 전용으로 전환 — 보안 경고 방지)"""
    repaired = []
    if not IS_MACOS or not os.path.exists(LAUNCHD_PLIST_PATH):
        return repaired
    try:
        with open(LAUNCHD_PLIST_PATH, "r") as f:
            content = f.read()
        import re
        match = re.search(r"<integer>(\d+)</integer>", content)
        if not match:
            return repaired
        current = int(match.group(1))
        has_curl = "curl " in content
        needs_update = current != TARGET_INTERVAL or has_curl
        if not needs_update:
            return repaired
        # 주기 불일치 또는 curl 잔재 — plist 재작성 (로컬 전용)
        email = get_email()
        if not email:
            return repaired
        if install_launchd(email):
            if has_curl:
                repaired.append("launchd plist curl 제거 (로컬 전용 전환)")
            if current != TARGET_INTERVAL:
                repaired.append(f"launchd 주기 {current}→{TARGET_INTERVAL}초 자동 갱신")
    except (IOError, OSError, ValueError):
        pass
    return repaired


def remove_cron():
    """기존 eo-codex-push cron 항목 제거"""
    try:
        result = subprocess.run(
            ["crontab", "-l"], capture_output=True, text=True
        )
        if result.returncode != 0:
            return
        lines = result.stdout.splitlines()
        new_lines = [l for l in lines if "eo-codex-push" not in l]
        if len(new_lines) == len(lines):
            return  # 제거할 항목 없음
        new_cron = "\n".join(new_lines)
        if new_cron.strip():
            new_cron += "\n"
        proc = subprocess.Popen(
            ["crontab", "-"], stdin=subprocess.PIPE, text=True
        )
        proc.communicate(input=new_cron)
    except (FileNotFoundError, OSError):
        pass


def migrate_cron_to_launchd() -> list:
    """macOS: cron → launchd 자동 마이그레이션"""
    repaired = []
    if not IS_MACOS:
        return repaired

    if is_launchd_active():
        return repaired  # 이미 launchd로 동작 중

    email = get_email()
    if not email:
        return repaired  # 이메일 없으면 스킵

    if install_launchd(email):
        repaired.append("launchd 등록 완료")
        remove_cron()
        repaired.append("cron → launchd 마이그레이션")

    return repaired


def parse_transcript_with_dates(transcript_path: str) -> dict:
    """transcript JSONL을 파싱하여 (date, model) 기준으로 토큰 집계.
    각 레코드의 timestamp 필드에서 KST 날짜를 추출하여 정확한 날짜에 배분.
    Returns: {(date, model, token_type): count}
    """
    from datetime import datetime, timezone, timedelta
    KST = timezone(timedelta(hours=9))

    seen: dict[str, dict] = {}  # msg_id → {model, usage, timestamp}
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

                ts_str = record.get("timestamp", "")
                try:
                    dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    date_str = dt.astimezone(KST).strftime("%Y-%m-%d")
                except (ValueError, AttributeError):
                    date_str = ""  # KST 변환 실패 시 스킵 (UTC fallback 금지)

                entry = {"model": model, "usage": usage, "date": date_str}
                msg_id = msg.get("id")
                if msg_id:
                    seen[msg_id] = entry
                else:
                    no_id_entries.append(entry)
    except (IOError, OSError):
        pass

    # (date, model, token_type) 기준 집계
    from collections import defaultdict
    totals = defaultdict(int)
    for e in list(seen.values()) + no_id_entries:
        d = e["date"]
        m = e["model"]
        u = e["usage"]
        if not d:
            continue
        totals[(d, m, "input")] += u.get("input_tokens", 0)
        totals[(d, m, "output")] += u.get("output_tokens", 0)
        totals[(d, m, "cache_read")] += u.get("cache_read_input_tokens", 0)
        totals[(d, m, "cache_creation")] += u.get("cache_creation_input_tokens", 0)

    return dict(totals)


def _recent_backfill_state_path(transcript_path: str) -> str:
    """최근 transcript 재스캔용 날짜 단위 상태 파일 경로."""
    digest = hashlib.md5(transcript_path.encode("utf-8")).hexdigest()[:12]
    return os.path.join(RECENT_BACKFILL_STATE_DIR, f"{digest}.json")


def load_recent_backfill_state(transcript_path: str) -> dict:
    """이전 재스캔에서 보낸 (date, model, token_type) 합계 로드."""
    path = _recent_backfill_state_path(transcript_path)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (IOError, OSError, json.JSONDecodeError):
        return {}


def save_recent_backfill_state(transcript_path: str, totals: dict) -> None:
    """현재 (date, model, token_type) 합계를 상태 파일에 저장."""
    os.makedirs(RECENT_BACKFILL_STATE_DIR, exist_ok=True)
    path = _recent_backfill_state_path(transcript_path)
    serializable = {}
    for (date_str, model, token_type), count in totals.items():
        serializable[f"{date_str}|{model}|{token_type}"] = count
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(serializable, f)
    except (IOError, OSError):
        pass


def compute_recent_backfill_delta(totals: dict, prev_state: dict) -> dict:
    """현재 날짜 단위 합계 - 이전 재스캔 합계 = 실제 delta."""
    delta = {}
    for (date_str, model, token_type), count in totals.items():
        key = f"{date_str}|{model}|{token_type}"
        prev = prev_state.get(key, 0)
        diff = count - prev
        if diff > 0:
            delta[(date_str, model, token_type)] = diff
    return delta


def push_recent_transcripts():
    """최근 transcript를 스캔하여 미전송분을 backfill API로 push.
    hook이 전혀 작동하지 않는 에디터(Zed 등)에서도 데이터를 수집하기 위한 안전망.
    2시간마다 launchd에서 호출되므로, 최대 2시간 지연으로 모든 세션을 커버.
    각 레코드의 timestamp에서 실제 KST 날짜를 추출하여 정확한 날짜에 배분."""
    import glob
    import time
    import urllib.request
    from collections import defaultdict

    BACKFILL_API = "https://token-dashboard-iota.vercel.app/api/backfill"
    TRANSCRIPT_BASE = os.path.expanduser("~/.claude/projects")

    if not os.path.isdir(TRANSCRIPT_BASE):
        return

    email = get_email()
    if not email:
        return

    # 최근 24시간 이내 수정된 transcript만 스캔
    cutoff = time.time() - 86400
    transcripts = glob.glob(os.path.join(TRANSCRIPT_BASE, "**", "*.jsonl"), recursive=True)
    recent = [t for t in transcripts if "subagents" not in t and os.path.getmtime(t) > cutoff]

    if not recent:
        return

    pushed = 0
    for transcript_path in recent:
        try:
            # 날짜별 집계 파싱
            dated_totals = parse_transcript_with_dates(transcript_path)
            if not dated_totals:
                continue

            # 기존 전송 상태와 비교하여 delta 계산
            # 날짜별 상태를 따로 저장하여 최신 날짜 토큰이 이전 날짜로 재분배되는 문제 방지
            prev_state = load_recent_backfill_state(transcript_path)
            dated_delta = compute_recent_backfill_delta(dated_totals, prev_state)
            if not dated_delta:
                continue

            by_date_model = defaultdict(lambda: {"input_tokens": 0, "output_tokens": 0,
                                                  "cache_read_tokens": 0, "cache_creation_tokens": 0})
            type_map = {"input": "input_tokens", "output": "output_tokens",
                        "cache_read": "cache_read_tokens", "cache_creation": "cache_creation_tokens"}

            for (date_str, model, token_type), delta_count in dated_delta.items():
                field = type_map.get(token_type)
                if not field:
                    continue
                by_date_model[(date_str, model)][field] += delta_count

            records = [{"date": d, "model": m, **tokens}
                       for (d, m), tokens in by_date_model.items()
                       if any(v > 0 for v in tokens.values())]
            if not records:
                continue

            payload = json.dumps({"email": email, "data": records, "mode": "add"}).encode("utf-8")
            req = urllib.request.Request(
                BACKFILL_API, data=payload,
                headers={"Content-Type": "application/json"}, method="POST",
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                if resp.status == 200:
                    save_recent_backfill_state(transcript_path, dated_totals)
                    pushed += 1
        except Exception:
            continue

    if pushed > 0:
        print(f"[hook_health] transcript 스캔: {pushed}개 세션 데이터 push")


WEEKLY_RESCAN_MARKER = os.path.join(HOOKS_DIR, ".weekly_rescan")
WEEKLY_RESCAN_INTERVAL = 3 * 86400  # 3일


def maybe_weekly_full_rescan():
    """주 1회 전체 transcript를 re-scan하여 backfill 누락 복구.
    push_recent_transcripts()는 최근 24시간만 스캔하므로,
    과거 누락 데이터(Codex만 있고 Claude 없는 날 등)를 보정할 수 없음.
    이 함수가 전체 transcript를 generate_backfill.py로 재스캔 후 backfill API에 전송."""
    import time

    # 주간 마커 체크
    try:
        if os.path.exists(WEEKLY_RESCAN_MARKER):
            mtime = os.path.getmtime(WEEKLY_RESCAN_MARKER)
            if time.time() - mtime < WEEKLY_RESCAN_INTERVAL:
                return
    except Exception:
        pass

    email = get_email()
    if not email:
        return

    # generate_backfill.py 위치 (로컬 번들 또는 GitHub)
    backfill_script = os.path.join(HOOKS_DIR, "generate_backfill.py")
    backfill_script_url = f"{BASE_URL}/generate_backfill.py"

    # 로컬에 없으면 다운로드
    if not os.path.exists(backfill_script):
        try:
            os.makedirs(HOOKS_DIR, exist_ok=True)
            urllib.request.urlretrieve(backfill_script_url, backfill_script)
            os.chmod(backfill_script, 0o755)
        except Exception:
            return

    try:
        result = subprocess.run(
            [sys.executable, backfill_script],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            print(f"[rescan] generate_backfill.py 실패: {result.stderr[-200:]}")
            return

        import urllib.request as _ur
        new_data = json.loads(result.stdout)
        payload = json.dumps({
            "email": email,
            "data": new_data.get("data", []),
            "mode": "add",
        }).encode("utf-8")

        req = _ur.Request(
            "https://token-dashboard-iota.vercel.app/api/backfill",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with _ur.urlopen(req, timeout=30) as resp:
            if resp.status == 200:
                print(f"[rescan] 주간 full re-scan 완료: {len(new_data.get('data', []))}건 전송")

        # 성공 시 마커 갱신
        os.makedirs(os.path.dirname(WEEKLY_RESCAN_MARKER), exist_ok=True)
        with open(WEEKLY_RESCAN_MARKER, "w") as f:
            f.write("")
    except Exception as e:
        print(f"[rescan] 예외: {e}")


DAILY_ACTIVITY_MARKER = os.path.expanduser("~/.claude/hooks/.activity_daily")
ACTIVITY_SCRIPT_URL = "https://raw.githubusercontent.com/EO-Studio-Dev/token-dashboard-hooks/main/generate_activity.py"
ACTIVITY_SCRIPT_LOCAL = os.path.join(HOOKS_DIR, "generate_activity.py")
GEMINI_PUSH_URL = "https://raw.githubusercontent.com/EO-Studio-Dev/token-dashboard-hooks/main/gemini_push.py"
GEMINI_PUSH_LOCAL = os.path.join(HOOKS_DIR, "gemini_push.py")
ACTIVITY_API_URL = "https://token-dashboard-iota.vercel.app/api/activity"


ACTIVITY_DIAG_URL = "https://token-dashboard-iota.vercel.app/api/activity-diag"
ACTIVITY_HEALTH_URL = "https://token-dashboard-iota.vercel.app/api/activity-health"


def _get_local_activity_version() -> str:
    """로컬 generate_activity.py의 SCRIPT_VERSION 읽기"""
    if not os.path.exists(ACTIVITY_SCRIPT_LOCAL):
        return ""
    try:
        with open(ACTIVITY_SCRIPT_LOCAL, "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("SCRIPT_VERSION"):
                    # SCRIPT_VERSION = "1" → "1"
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
                if not line.strip() or line.startswith("#") or line.startswith("from "):
                    continue
                break
    except Exception:
        pass
    return ""


def _get_remote_activity_version() -> str:
    """GitHub에서 generate_activity.py 첫 10줄만 읽어 버전 확인"""
    try:
        req = urllib.request.Request(ACTIVITY_SCRIPT_URL)
        with urllib.request.urlopen(req, timeout=5) as resp:
            for line in resp:
                decoded = line.decode("utf-8", errors="replace")
                if decoded.startswith("SCRIPT_VERSION"):
                    return decoded.split("=", 1)[1].strip().strip('"').strip("'")
                if not decoded.strip() or decoded.startswith("#") or decoded.startswith("from "):
                    continue
                break
    except Exception:
        pass
    return ""


def ensure_activity_script() -> Optional[str]:
    """generate_activity.py 로컬 번들 보장. 없거나 버전 낮으면 다운로드.
    다운로드 실패 시에도 기존 로컬 파일이 있으면 그대로 사용.
    Returns: 복구 메시지 또는 None"""
    local_ver = _get_local_activity_version()

    if local_ver:
        # 로컬 파일 있음 — 버전 체크 (실패해도 기존 파일 사용)
        try:
            remote_ver = _get_remote_activity_version()
            if remote_ver and remote_ver > local_ver:
                urllib.request.urlretrieve(ACTIVITY_SCRIPT_URL, ACTIVITY_SCRIPT_LOCAL)
                os.chmod(ACTIVITY_SCRIPT_LOCAL, 0o755)
                return f"generate_activity.py 업데이트 v{local_ver}→v{remote_ver}"
        except Exception:
            pass  # 업데이트 실패 — 기존 버전으로 계속 동작
        return None

    # 로컬 파일 없음 — 다운로드
    try:
        os.makedirs(HOOKS_DIR, exist_ok=True)
        urllib.request.urlretrieve(ACTIVITY_SCRIPT_URL, ACTIVITY_SCRIPT_LOCAL)
        os.chmod(ACTIVITY_SCRIPT_LOCAL, 0o755)
        return "generate_activity.py 신규 설치"
    except Exception as e:
        print(f"[hook_health] generate_activity.py 다운로드 실패: {e}")
        return None


def ensure_gemini_push() -> Optional[str]:
    """gemini_push.py 로컬 번들 보장. 없으면 다운로드."""
    if os.path.exists(GEMINI_PUSH_LOCAL):
        return None
    try:
        os.makedirs(HOOKS_DIR, exist_ok=True)
        urllib.request.urlretrieve(GEMINI_PUSH_URL, GEMINI_PUSH_LOCAL)
        os.chmod(GEMINI_PUSH_LOCAL, 0o755)
        return "gemini_push.py 신규 설치"
    except Exception:
        return None


def _report_activity_diag(email, status, detail=""):
    """activity push 결과를 서버에 보고 (진단용) + 로컬 로그 fallback"""
    import platform
    import datetime as _dt
    try:
        payload = json.dumps({
            "email": email,
            "status": status,
            "detail": detail[:500],
            "platform": platform.system(),
            "python": sys.version.split()[0],
        }).encode("utf-8")
        req = urllib.request.Request(
            ACTIVITY_DIAG_URL, data=payload,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass


def _get_launchd_status() -> str:
    if not IS_MACOS:
        return "n/a"
    try:
        result = subprocess.run(
            ["launchctl", "list", LAUNCHD_LABEL],
            capture_output=True, text=True, timeout=5,
        )
        return "loaded" if result.returncode == 0 else "missing"
    except Exception:
        return "error"


def _read_tail(path: str, limit: int = 5) -> list[str]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()[-limit:]
        return [line.strip()[:200] for line in lines if line.strip()]
    except Exception:
        return []


def collect_health_snapshot(email: str, source: str, repaired: Optional[List[str]] = None) -> dict:
    transcript_base = os.path.expanduser("~/.claude/projects")
    transcript_count = 0
    recent_transcript_count = 0
    try:
        import glob
        import time
        files = glob.glob(os.path.join(transcript_base, "**", "*.jsonl"), recursive=True)
        files = [f for f in files if "subagents" not in f]
        transcript_count = len(files)
        cutoff = time.time() - 86400
        recent_transcript_count = sum(1 for f in files if os.path.getmtime(f) > cutoff)
    except Exception:
        pass

    marker_value = ""
    try:
        if os.path.exists(DAILY_ACTIVITY_MARKER):
            marker_value = open(DAILY_ACTIVITY_MARKER, "r", encoding="utf-8").read().strip()[:120]
    except Exception:
        pass

    payload = {
        "email_file_exists": os.path.exists(os.path.join(HOOKS_DIR, ".otel_email")),
        "otel_push_exists": os.path.exists(os.path.join(HOOKS_DIR, "otel_push.py")),
        "hook_health_exists": os.path.exists(os.path.join(HOOKS_DIR, "hook_health.py")),
        "codex_push_exists": os.path.exists(os.path.join(HOOKS_DIR, "codex_push.py")),
        "settings_exists": os.path.exists(SETTINGS_PATH),
        "launchd_plist_exists": os.path.exists(LAUNCHD_PLIST_PATH) if IS_MACOS else False,
        "launchd_status": _get_launchd_status(),
        "activity_marker": marker_value,
        "transcript_count": transcript_count,
        "recent_transcript_count_24h": recent_transcript_count,
        "activity_diag_tail": _read_tail(os.path.join(HOOKS_DIR, "activity_diag.log")),
        "launchd_log_tail": _read_tail(os.path.join(HOOKS_DIR, "launchd.log")),
        "repaired": repaired or [],
        "source": source,
    }
    return payload


def _report_activity_health(
    email: str,
    source: str,
    status: str,
    repaired: Optional[List[str]] = None,
    detail: str = "",
):
    if not email:
        return
    try:
        payload = json.dumps({
            "email": email,
            "source": source,
            "status": status,
            "platform": platform.system(),
            "python": sys.version.split()[0],
            "payload": collect_health_snapshot(email, source, repaired),
        }).encode("utf-8")
        req = urllib.request.Request(
            ACTIVITY_HEALTH_URL, data=payload,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass
    # 로컬 로그에도 항상 기록 (POST 실패 대비)
    try:
        log_path = os.path.join(HOOKS_DIR, "activity_diag.log")
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"{_dt.datetime.now().isoformat()} | {email} | {status} | {detail[:200]}\n")
    except Exception:
        pass


MAX_DAILY_RETRIES = 48  # 30분 × 48 = 24시간 기준 지표. 실패 시에도 다음 주기 재시도는 계속 허용.


def _read_marker(today: str) -> tuple:
    """마커 파일 읽기 → (is_success, attempts).

    형식:
      - 신규 성공: '2026-03-26:success'
      - 신규 재시도 상태: '2026-03-26:retry:12'
      - 레거시: '2026-03-26:12' 또는 '2026-03-26'

    중요:
      과거 구현은 성공과 재시도 소진을 모두 'date:48'로 저장해서,
      실패만 누적된 사용자도 당일 재시도가 완전히 멈췄다.
      이제는 success만 skip 대상이고, 실패는 계속 재시도한다.
    """
    try:
        if os.path.exists(DAILY_ACTIVITY_MARKER):
            content = open(DAILY_ACTIVITY_MARKER, "r", encoding="utf-8").read().strip()
            parts = content.split(":")
            if len(parts) == 2:
                marker_date, value = parts
                if marker_date != today:
                    return (False, 0)
                if value == "success":
                    return (True, MAX_DAILY_RETRIES)
                if value.isdigit():
                    return (False, int(value))
                return (False, 0)
            if len(parts) >= 3:
                marker_date, status, value = parts[0], parts[1], parts[2]
                if marker_date != today:
                    return (False, 0)
                if status == "success":
                    return (True, MAX_DAILY_RETRIES)
                if status == "retry" and value.isdigit():
                    return (False, int(value))
                return (False, 0)
            # 기존 형식 ("2026-03-26") — 재시도 허용 (attempts=1로 취급)
            return (False, 1) if content == today else (False, 0)
    except Exception:
        pass
    return (False, 0)


def _write_marker(today: str, attempts: int, success: bool = False):
    os.makedirs(os.path.dirname(DAILY_ACTIVITY_MARKER), exist_ok=True)
    with open(DAILY_ACTIVITY_MARKER, "w", encoding="utf-8") as m:
        if success:
            m.write(f"{today}:success")
        else:
            m.write(f"{today}:retry:{attempts}")


def maybe_daily_reactivity():
    """하루 최대 3회 전체 transcript에서 activity 데이터 재생성 및 전송.
    로컬 번들(~/.claude/hooks/generate_activity.py)을 우선 사용.
    없으면 GitHub에서 다운로드 후 로컬에 저장 (다음 실행부터 로컬 사용)."""
    import datetime

    today = datetime.date.today().isoformat()
    email = get_email()  # 마커 체크보다 먼저 — diag 전송용

    is_success, attempts = _read_marker(today)
    if is_success:
        if email:
            _report_activity_diag(email, "skip", "done_today_success")
        return

    if not email:
        _report_activity_diag("unknown", "skip", "no_email")
        return

    # 로컬 번들 보장 (없으면 다운로드, 버전 낮으면 갱신)
    ensure_activity_script()
    ensure_gemini_push()

    script_path = ACTIVITY_SCRIPT_LOCAL
    if not os.path.exists(script_path):
        # 로컬도 없고 다운로드도 실패한 경우 — 직접 다운로드 1회 더 시도 (에러 캡처)
        dl_err = ""
        try:
            os.makedirs(HOOKS_DIR, exist_ok=True)
            urllib.request.urlretrieve(ACTIVITY_SCRIPT_URL, script_path)
            os.chmod(script_path, 0o755)
        except Exception as e:
            dl_err = str(e)[:200]
            _report_activity_diag(email, "error", f"generate_activity.py not available — download failed: {dl_err}")
            return

    try:
        # --push: 15건씩 배치로 API 전송
        result = subprocess.run(
            [sys.executable, script_path, "--push"],
            capture_output=True, text=True, timeout=600,
        )

        print(f"[activity] {email}: returncode={result.returncode}")
        if result.stdout:
            for line in result.stdout.strip().split("\n")[-3:]:
                print(f"[activity]   {line}")
        if result.stderr:
            for line in result.stderr.strip().split("\n")[-3:]:
                print(f"[activity] ERR: {line}")

        if result.returncode == 0:
            stdout = result.stdout or ""
            if ": 0건 성공," in stdout:
                next_attempt = attempts + 1
                _write_marker(today, next_attempt)
                print(f"[activity] 0건 성공 — 재시도 {next_attempt}/{MAX_DAILY_RETRIES}+")
                _report_activity_diag(email, "empty", f"attempt_{next_attempt} {stdout[-200:]}")
            else:
                _write_marker(today, attempts, success=True)
                print(f"[activity] 완료 — 마커 기록 (성공)")
                _report_activity_diag(email, "ok", stdout[-200:])
        else:
            next_attempt = attempts + 1
            _write_marker(today, next_attempt)
            detail = (result.stderr or result.stdout or "")[-300:]
            print(f"[activity] 실패 — 재시도 {next_attempt}/{MAX_DAILY_RETRIES}+")
            _report_activity_diag(email, "fail", f"attempt_{next_attempt} rc={result.returncode} {detail}")
    except Exception as e:
        next_attempt = attempts + 1
        _write_marker(today, next_attempt)
        print(f"[activity] 예외: {e} — 재시도 {next_attempt}/{MAX_DAILY_RETRIES}+")
        _report_activity_diag(email, "error", f"attempt_{next_attempt} {e}")


SELF_HEAL_MARKER = os.path.join(HOOKS_DIR, ".self_heal_last")
SELF_HEAL_THROTTLE_SECS = 3600  # 1시간 쓰로틀


def _should_run_self_heal() -> bool:
    """마지막 self-heal 실행 후 1시간 미경과 시 skip"""
    try:
        if os.path.exists(SELF_HEAL_MARKER):
            mtime = os.path.getmtime(SELF_HEAL_MARKER)
            import time
            if time.time() - mtime < SELF_HEAL_THROTTLE_SECS:
                return False
    except Exception:
        pass
    return True


def _touch_self_heal_marker():
    try:
        os.makedirs(os.path.dirname(SELF_HEAL_MARKER), exist_ok=True)
        with open(SELF_HEAL_MARKER, "w") as f:
            f.write("")
    except Exception:
        pass


def self_heal_mode():
    # 1시간 쓰로틀: 매 프롬프트마다 실행되므로 과도한 실행 방지
    if not _should_run_self_heal():
        return
    _touch_self_heal_marker()

    repaired = []
    repaired.extend(migrate_cron_to_launchd())
    repaired.extend(sync_plist_interval())
    # 모든 managed 스크립트 버전 체크 + 자동 업데이트 (하루 1회)
    repaired.extend(ensure_all_scripts_updated())

    # 인프라 복구 외에 즉시 데이터 push — launchd 비활성 유저도 데이터 수집
    try:
        push_recent_transcripts()
    except Exception:
        pass
    try:
        maybe_weekly_full_rescan()
    except Exception:
        pass
    try:
        maybe_daily_reactivity()
    except Exception:
        pass

    email = get_email()
    if email:
        _report_activity_health(email, "self-heal", "ok" if repaired else "idle", repaired)


def main():
    if "--self-heal" in sys.argv:
        self_heal_mode()
        return

    repaired = []

    # 0. macOS: cron → launchd 마이그레이션 + plist 주기 동기화
    repaired.extend(migrate_cron_to_launchd())
    repaired.extend(sync_plist_interval())

    # 1. otel_push.py 파일 확인 (push_recent_transcripts가 import하므로 필요)
    if not check_otel_script():
        download_otel_script()
        repaired.append("otel_push.py 재다운로드")

    # 2. 레거시 Stop/UserPromptSubmit hook 정리 (세션 블로킹 방지)
    removed = cleanup_legacy_hooks()
    repaired.extend(removed)

    # 2.5 로컬 번들 보장 (generate_activity.py + gemini_push.py)
    activity_repair = ensure_activity_script()
    if activity_repair:
        repaired.append(activity_repair)
    gemini_repair = ensure_gemini_push()
    if gemini_repair:
        repaired.append(gemini_repair)

    # 3. 최근 transcript 스캔 & push (hook 미작동 에디터 대응)
    try:
        push_recent_transcripts()
    except Exception:
        pass  # 스캔 실패해도 헬스체크 메인 로직에 영향 없음

    # 3.5 주간 full re-scan (과거 누락 데이터 복구)
    try:
        maybe_weekly_full_rescan()
    except Exception:
        pass

    # 4. 하루 1회 activity 데이터 재생성
    try:
        maybe_daily_reactivity()
    except Exception:
        pass

    email = get_email()
    if email:
        _report_activity_health(email, "scheduler", "ok" if repaired else "idle", repaired)

    if repaired:
        print(f"[hook_health] 자동 복구: {', '.join(repaired)}")
    elif "--verbose" in sys.argv:
        print("[hook_health] 정상")


if __name__ == "__main__":
    main()
