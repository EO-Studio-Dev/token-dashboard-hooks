#!/usr/bin/env python3
"""
EO Studio Token Dashboard — 설치 스크립트
Claude Code / Codex / Gemini CLI 사용량 자동 수집 설정.

사용법:
  Mac/Linux:  curl -sfL https://raw.githubusercontent.com/EO-Studio-Dev/token-dashboard-hooks/main/install-hook.py | python3
  Windows:    curl -sfL https://raw.githubusercontent.com/EO-Studio-Dev/token-dashboard-hooks/main/install-hook.py | python

이메일 미리 지정 (pipe 사용 시 권장):
  curl -sfL ... | EMAIL=june python3
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

BASE_URL = "https://raw.githubusercontent.com/EO-Studio-Dev/token-dashboard-hooks/main"
DASHBOARD_API = "https://token-dashboard-iota.vercel.app/api/backfill"
HOOKS_DIR = os.path.join(os.path.expanduser("~"), ".claude", "hooks")
SETTINGS_PATH = os.path.join(os.path.expanduser("~"), ".claude", "settings.json")

IS_MAC = platform.system() == "Darwin"
IS_WINDOWS = platform.system() == "Windows"

# Windows UTF-8: stdout/stderr 인코딩 강제 (irm | python 파이프에서 한글 깨짐 방지)
if IS_WINDOWS:
    os.environ["PYTHONUTF8"] = "1"
    os.environ["PYTHONIOENCODING"] = "utf-8"
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def print_banner():
    print()
    print("  ╔═══════════════════════════════════════════════════════════╗")
    print("  ║          EO Studio Token Dashboard Installer              ║")
    print("  ╠═══════════════════════════════════════════════════════════╣")
    print("  ║                                                           ║")
    print("  ║  EO Studio 내부 전용 도구입니다.                         ║")
    print("  ║  AI 도구(Claude/Codex/Gemini) 사용량만 수집하며,        ║")
    print("  ║  코드·파일·개인정보는 일절 수집하지 않습니다.            ║")
    print("  ║  모든 데이터는 EO Studio 내부 서버로만 전송됩니다.      ║")
    print("  ║                                                           ║")
    print("  ║  대시보드: https://token-dashboard-iota.vercel.app        ║")
    print("  ║  문의: 서현 (ash@eoeoeo.net)                              ║")
    print("  ║                                                           ║")
    print("  ╚═══════════════════════════════════════════════════════════╝")
    print()


def _read_tty(prompt: str) -> str:
    """stdin이 pipe여도 TTY에서 직접 입력받기. (Fix #1: curl | python3 대응)"""
    try:
        if IS_WINDOWS:
            import msvcrt
            sys.stderr.write(prompt)
            sys.stderr.flush()
            chars = []
            while True:
                c = msvcrt.getwch()
                if c in ("\r", "\n"):
                    sys.stderr.write("\n")
                    break
                chars.append(c)
                sys.stderr.write(c)
            return "".join(chars).strip()
        else:
            tty = open("/dev/tty", "r")
            sys.stderr.write(prompt)
            sys.stderr.flush()
            result = tty.readline().strip()
            tty.close()
            return result
    except (OSError, EOFError):
        return ""


def check_prerequisites() -> str:
    """git 확인 + 이메일 감지/입력. Returns email."""
    if not shutil.which("git"):
        print("[!] git이 설치되어 있지 않습니다.")
        sys.exit(1)

    # Fix #1: EMAIL 환경변수 우선 (pipe 사용 시)
    env_email = os.environ.get("EMAIL", "").strip()
    if env_email:
        email = env_email if "@" in env_email else f"{env_email}@eoeoeo.net"
        print(f"사용자 (환경변수): {email}")
        os.makedirs(HOOKS_DIR, exist_ok=True)
        with open(os.path.join(HOOKS_DIR, ".otel_email"), "w") as f:
            f.write(email)
        print()
        return email

    # git config에서 이메일 감지
    email = ""
    try:
        r = subprocess.run(["git", "config", "user.email"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5)
        if r.returncode == 0:
            email = r.stdout.strip()
    except Exception:
        pass

    if not email:
        # git email이 아예 없는 경우만 입력 요구
        print("@eoeoeo.net 이메일을 입력해주세요 (예: june)")
        print("개인 이메일(iCloud/Gmail 등)로 git을 사용 중이면 그대로 Enter:")

        email_id = _read_tty("이메일 아이디 (Enter=건너뜀): ")
        if email_id:
            email = email_id if "@" in email_id else f"{email_id}@eoeoeo.net"
            print(f"-> 이메일 설정: {email}")
        else:
            print("[!] 이메일을 감지할 수 없습니다.")
            print("    EMAIL 환경변수로 지정해주세요:")
            print(f"    curl -sfL {BASE_URL}/install-hook.py | EMAIL=june python3")
            sys.exit(1)
    elif "@eoeoeo.net" not in email:
        # 개인 이메일 사용자 — 그대로 사용 (서버에서 alias 매핑)
        print(f"  (개인 이메일 감지: {email} → 서버에서 자동 매핑됩니다)")

    print(f"사용자: {email}")

    # .otel_email 저장
    os.makedirs(HOOKS_DIR, exist_ok=True)
    with open(os.path.join(HOOKS_DIR, ".otel_email"), "w") as f:
        f.write(email)
    print()
    return email


def download(url: str, dest: str) -> bool:
    """URL 다운로드. 실패 시 False 반환."""
    try:
        urllib.request.urlretrieve(url, dest)
        if not IS_WINDOWS:
            os.chmod(dest, 0o755)
        # 다운로드된 파일이 HTML 에러 페이지가 아닌지 확인
        if os.path.getsize(dest) < 100:
            print(f"      [!] 다운로드 파일이 너무 작습니다: {dest}")
            return False
        return True
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as e:
        print(f"      [!] 다운로드 실패: {url} → {e}")
        return False


def step1_download_otel_push():
    """otel_push.py 다운로드 — Fix #4: critical 파일 실패 시 exit"""
    print("[1/7] otel_push.py 다운로드 중...")
    dest = os.path.join(HOOKS_DIR, "otel_push.py")
    if download(f"{BASE_URL}/otel_push.py", dest):
        print(f"      -> {dest}")
    else:
        print("      [!] otel_push.py 다운로드 실패. 네트워크를 확인하세요.")
        sys.exit(1)


def _backup_settings():
    """Fix #5: settings.json 수정 전 백업"""
    if os.path.exists(SETTINGS_PATH):
        backup = SETTINGS_PATH + ".bak"
        try:
            shutil.copy2(SETTINGS_PATH, backup)
        except OSError:
            pass


def step2_cleanup_legacy_hooks():
    """기존 Stop/UserPromptSubmit hook 정리"""
    print("[2/7] 레거시 hook 정리 중...")
    if not os.path.exists(SETTINGS_PATH):
        print("      -> settings.json 없음. 건너뜁니다.")
        return

    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        print("      -> settings.json 읽기 실패. 건너뜁니다.")
        return

    hooks = data.get("hooks", {})
    removed = []
    for event_type in ["Stop", "UserPromptSubmit"]:
        entries = hooks.get(event_type, [])
        cleaned = []
        for entry in entries:
            entry_hooks = entry.get("hooks", [])
            filtered = [h for h in entry_hooks if "otel_push" not in h.get("command", "")]
            if filtered:
                entry["hooks"] = filtered
                cleaned.append(entry)
            elif entry_hooks:
                removed.append(event_type)
        hooks[event_type] = cleaned

    if removed:
        _backup_settings()
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        print(f"      -> {', '.join(removed)} hook 제거 완료 (launchd 스캔으로 대체)")
    else:
        print("      -> 정리할 hook 없음.")


def step3_backfill_transcripts(email: str):
    """과거 transcript backfill"""
    print("[3/7] 과거 transcript backfill 중...")
    claude_dir = os.path.join(os.path.expanduser("~"), ".claude", "projects")
    if not os.path.isdir(claude_dir):
        print("      ~/.claude/projects 디렉토리가 없습니다. 건너뜁니다.")
        return

    count = 0
    for root, dirs, files in os.walk(claude_dir):
        if "subagents" in root:
            continue
        count += sum(1 for f in files if f.endswith(".jsonl"))

    if count == 0:
        print("      과거 transcript가 없습니다. 건너뜁니다.")
        return

    print(f"      transcript 파일 {count}개 발견.")

    # Fix #6: mktemp → mkstemp
    with tempfile.NamedTemporaryFile(suffix=".py", delete=False, mode="w") as sf:
        script_path = sf.name
    fd, backfill_json = tempfile.mkstemp(suffix=".json")
    os.close(fd)

    try:
        if not download(f"{BASE_URL}/generate_backfill.py", script_path):
            return
        subprocess.run([sys.executable, script_path, "--out", backfill_json],
                       timeout=120, check=False, encoding="utf-8", errors="replace")

        if not os.path.exists(backfill_json) or os.path.getsize(backfill_json) == 0:
            print("      파싱 가능한 데이터가 없습니다. 건너뜁니다.")
            return

        with open(backfill_json, "r", encoding="utf-8") as f:
            result = json.load(f)

        data_count = len(result.get("data", []))
        if data_count == 0:
            print("      파싱 가능한 데이터가 없습니다. 건너뜁니다.")
            return

        print(f"      {data_count}개 레코드 생성. 대시보드로 전송 중...")
        result["email"] = email
        payload = json.dumps(result).encode("utf-8")
        req = urllib.request.Request(DASHBOARD_API, data=payload,
                                     headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                if resp.status == 200:
                    print("      -> 전송 완료!")
                else:
                    print(f"      -> 전송 실패 (HTTP {resp.status})")
        except (urllib.error.URLError, urllib.error.HTTPError) as e:
            print(f"      -> 전송 실패: {e}")
            print("      -> hook 설치는 완료됩니다. 과거 데이터는 나중에 재시도 가능합니다.")
    finally:
        for p in [script_path, backfill_json]:
            try:
                os.unlink(p)
            except OSError:
                pass


def step4_collect_codex_gemini(email: str):
    """Codex + Gemini CLI 1회 수집"""
    print("[4/7] Codex + Gemini CLI 데이터 수집 중...")

    codex_dir = os.path.join(os.path.expanduser("~"), ".codex", "sessions")
    if os.path.isdir(codex_dir):
        with tempfile.NamedTemporaryFile(suffix=".py", delete=False, mode="w") as sf:
            script_path = sf.name
        try:
            if download(f"{BASE_URL}/codex_push.py", script_path):
                r = subprocess.run([sys.executable, script_path, "--email", email],
                                   capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
                for line in (r.stdout or "").splitlines():
                    print(f"      {line}")
        finally:
            try:
                os.unlink(script_path)
            except OSError:
                pass
    else:
        print("      ~/.codex/sessions/ 없음. Codex를 사용하면 자동 수집됩니다.")

    gemini_dir = os.path.join(os.path.expanduser("~"), ".gemini", "tmp")
    if os.path.isdir(gemini_dir):
        with tempfile.NamedTemporaryFile(suffix=".py", delete=False, mode="w") as sf:
            script_path = sf.name
        try:
            if download(f"{BASE_URL}/gemini_push.py", script_path):
                r = subprocess.run([sys.executable, script_path, "--email", email],
                                   capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
                for line in (r.stdout or "").splitlines():
                    print(f"      {line}")
        finally:
            try:
                os.unlink(script_path)
            except OSError:
                pass
    else:
        print("      ~/.gemini/tmp/ 없음. Gemini를 사용하면 자동 수집됩니다.")


def step5_install_scripts_and_scheduler(email: str):
    """스크립트 설치 + 스케줄러 등록"""
    print("[5/7] 스크립트 설치 + 자동 수집 등록 중...")

    scripts = ["codex_push.py", "gemini_push.py", "hook_health.py",
               "generate_activity.py", "generate_backfill.py"]

    print("      스크립트 다운로드 중...")
    critical_ok = True
    for s in scripts:
        if not download(f"{BASE_URL}/{s}", os.path.join(HOOKS_DIR, s)):
            if s in ("hook_health.py", "codex_push.py", "gemini_push.py"):
                critical_ok = False

    if not critical_ok:
        print("      [!] 핵심 스크립트 다운로드 실패. 네트워크를 확인하세요.")
        sys.exit(1)

    hook_health = os.path.join(HOOKS_DIR, "hook_health.py")
    codex_push = os.path.join(HOOKS_DIR, "codex_push.py")
    gemini_push = os.path.join(HOOKS_DIR, "gemini_push.py")

    py = sys.executable
    # 경로에 공백이 있을 수 있으므로 인용부호 사용
    scheduler_cmd = f'"{py}" "{hook_health}"; "{py}" "{codex_push}" --email {email}; "{py}" "{gemini_push}" --email {email}'

    if IS_MAC:
        _register_launchd(scheduler_cmd)
    elif IS_WINDOWS:
        _register_task_scheduler(py, hook_health, codex_push, gemini_push, email)
    else:
        _register_cron(scheduler_cmd)


def _register_launchd(scheduler_cmd: str):
    """macOS launchd 등록"""
    label = "net.eoeoeo.hook-health"
    plist_dir = os.path.join(os.path.expanduser("~"), "Library", "LaunchAgents")
    os.makedirs(plist_dir, exist_ok=True)
    plist_path = os.path.join(plist_dir, f"{label}.plist")

    plist_xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>-c</string>
        <string>{scheduler_cmd}</string>
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
</plist>"""

    with open(plist_path, "w") as f:
        f.write(plist_xml)

    r = subprocess.run(["launchctl", "unload", plist_path], capture_output=True, timeout=10)
    r = subprocess.run(["launchctl", "load", plist_path], capture_output=True, timeout=10)
    if r.returncode != 0:
        print(f"      [!] launchd 등록 실패: {r.stderr.decode().strip()}")
    else:
        print("      -> launchd 등록 완료: 30분마다 자동 수집")

    # 기존 cron 제거 (마이그레이션)
    try:
        r = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=5)
        if r.returncode == 0 and "eo-codex-push" in r.stdout:
            lines = [l for l in r.stdout.splitlines() if "eo-codex-push" not in l]
            # Fix #7: Popen에 timeout 제거
            p = subprocess.Popen(["crontab", "-"], stdin=subprocess.PIPE)
            p.communicate(input="\n".join(lines).encode(), timeout=5)
            print("      -> 기존 cron 제거 완료 (launchd로 전환)")
    except Exception:
        pass


def _register_task_scheduler(py: str, hook_health: str, codex_push: str, gemini_push: str, email: str):
    """Windows Task Scheduler 등록"""
    task_name = "EO-TokenDashboard"

    subprocess.run(["schtasks", "/Delete", "/TN", task_name, "/F"],
                   capture_output=True, encoding="utf-8", errors="replace", timeout=10)

    bat_path = os.path.join(HOOKS_DIR, "run-hooks.bat")
    with open(bat_path, "w") as f:
        f.write(f'@echo off\n')
        f.write(f'"{py}" "{hook_health}"\n')
        f.write(f'"{py}" "{codex_push}" --email {email}\n')
        f.write(f'"{py}" "{gemini_push}" --email {email}\n')

    # Fix #3: Windows 경로 공백 대응 — /TR에 인용부호
    r = subprocess.run([
        "schtasks", "/Create", "/TN", task_name,
        "/TR", f'"{bat_path}"',
        "/SC", "MINUTE", "/MO", "30",
        "/F"
    ], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10)

    if r.returncode == 0:
        print("      -> Task Scheduler 등록 완료: 30분마다 자동 수집")
    else:
        print(f"      -> Task Scheduler 등록 실패: {(r.stderr or '').strip()}")
        print("      -> 수동으로 등록하거나 관리자 권한으로 다시 시도해주세요.")


def _register_cron(scheduler_cmd: str):
    """Linux cron 등록"""
    cron_line = f"*/30 * * * * {scheduler_cmd} # eo-codex-push"
    try:
        r = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=5)
        existing = r.stdout if r.returncode == 0 else ""
    except Exception:
        existing = ""

    lines = [l for l in existing.splitlines() if "eo-codex-push" not in l]
    lines.append(cron_line)

    # Fix #7: Popen에 timeout 제거 → communicate에서 timeout
    p = subprocess.Popen(["crontab", "-"], stdin=subprocess.PIPE)
    p.communicate(input="\n".join(lines).encode(), timeout=5)
    print("      -> cron 등록 완료: 30분마다 자동 수집")


def step6_self_heal_hook():
    """UserPromptSubmit self-heal hook 등록"""
    print("[6/7] self-heal hook 등록 중...")

    py = sys.executable
    if IS_WINDOWS:
        hook_cmd = f'"{py}" "%USERPROFILE%/.claude/hooks/hook_health.py" --self-heal'
    else:
        hook_cmd = f"bash -lc '(\"{py}\" ~/.claude/hooks/hook_health.py --self-heal >/dev/null 2>&1 &) >/dev/null 2>&1'"

    # Fix #5: settings.json 파싱 실패 시 기존 파일 보존
    data = {}
    if os.path.exists(SETTINGS_PATH):
        try:
            with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            # 파싱 실패 시 기존 파일 건드리지 않고 종료
            print("      -> settings.json 파싱 실패. 건너뜁니다.")
            return

    if "hooks" not in data:
        data["hooks"] = {}

    entries = data["hooks"].get("UserPromptSubmit", [])
    found = any(
        "hook_health.py --self-heal" in h.get("command", "")
        for entry in entries
        for h in entry.get("hooks", [])
    )

    if not found:
        _backup_settings()
        entries.append({"hooks": [{"type": "command", "command": hook_cmd}]})
        data["hooks"]["UserPromptSubmit"] = entries
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    print("      -> self-heal hook 등록 완료")


def step7_verify(email: str):
    """검증"""
    print("[7/7] 검증 중...")

    all_ok = True
    for s in ["otel_push.py", "hook_health.py", "codex_push.py", "gemini_push.py"]:
        path = os.path.join(HOOKS_DIR, s)
        if os.path.exists(path) and os.path.getsize(path) > 100:
            print(f"        {s} OK")
        else:
            print(f"        {s} MISSING")
            all_ok = False

    if all_ok:
        print("      -> 검증 완료!")
    else:
        print("      -> [!] 일부 파일이 없습니다. 네트워크 확인 후 재실행하세요.")


def print_summary(email: str):
    print()
    print("=== 설치 완료 ===")
    print(f"사용자: {email}")
    print("대시보드: https://token-dashboard-iota.vercel.app")
    print()
    if IS_MAC:
        sched = "launchd"
    elif IS_WINDOWS:
        sched = "Task Scheduler"
    else:
        sched = "cron"
    print(f"Claude Code: 30분마다 transcript 스캔 ({sched})")
    print(f"Codex CLI:   30분마다 자동 수집 ({sched})")
    print(f"Gemini CLI:  30분마다 자동 수집 ({sched})")


def main():
    print_banner()
    email = check_prerequisites()
    os.makedirs(HOOKS_DIR, exist_ok=True)

    step1_download_otel_push()
    step2_cleanup_legacy_hooks()
    step3_backfill_transcripts(email)
    step4_collect_codex_gemini(email)
    step5_install_scripts_and_scheduler(email)
    step6_self_heal_hook()
    step7_verify(email)
    print_summary(email)


if __name__ == "__main__":
    main()
