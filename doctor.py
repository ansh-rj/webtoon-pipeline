#!/usr/bin/env python3
"""Diagnostics for the webtoon video pipeline. Every other script should be able to
call `python doctor.py` (or import run_checks) as a preflight gate.
Usage: python doctor.py [--dry-run] [--json]
Exits nonzero if any check hard-FAILs (WARN does not fail the exit code).
"""
import argparse
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from state_manager import atomic_write_json, load_json, mark_unit  # noqa: E402

LOGS_DIR = ROOT / "logs"
JOBS_DIR = ROOT / "jobs"
ERROR_LOG = LOGS_DIR / "errors.log"
HEARTBEAT_LOG = LOGS_DIR / "heartbeat.log"
STATE_PATH = JOBS_DIR / "doctor_state.json"
VENV_DIR = ROOT / "venv"
CONFIG_PATH = ROOT / "pipeline_config.json"
MACHINE_PROFILE_PATH = ROOT / "machine_profile.json"
ENV_PATH = ROOT / ".env"
FOLDERS = ["chapters", "projects", "assets/music", "assets/end_cards", "logs", "jobs"]

IMPORT_NAME = {
    "playwright": "playwright",
    "opencv-python": "cv2",
    "pillow": "PIL",
    "easyocr": "easyocr",
    "pytesseract": "pytesseract",
    "edge-tts": "edge_tts",
    "ffmpeg-python": "ffmpeg",
    "faster-whisper": "faster_whisper",
    "anthropic": "anthropic",
    "google-genai": "google.genai",
    "requests": "requests",
    "psutil": "psutil",
}

FFMPEG_INSTALL_HINT = {
    "Windows": "winget install ffmpeg",
    "Darwin": "brew install ffmpeg",
    "Linux": "sudo apt install ffmpeg",
}
TESSERACT_INSTALL_HINT = {
    "Windows": "winget install --id UB-Mannheim.TesseractOCR",
    "Darwin": "brew install tesseract",
    "Linux": "sudo apt install tesseract-ocr",
}

DISK_WARN_GB = 10
DISK_FAIL_GB = 2

CHECKS = [
    "python_version",
    "venv",
    "dependencies",
    "ffmpeg",
    "tesseract",
    "playwright_browser",
    "folders",
    "pipeline_config",
    "machine_profile",
    "env_file",
    "disk_space",
    "network",
    "anthropic_key",
    "gemini_key",
    "elevenlabs_key",
]

CHECK_ESTIMATES = {
    "python_version": "instant",
    "venv": "instant",
    "dependencies": "2-5s (subprocess import probe)",
    "ffmpeg": "instant",
    "tesseract": "instant",
    "playwright_browser": "2-5s (launches headless chromium)",
    "folders": "instant",
    "pipeline_config": "instant",
    "machine_profile": "instant",
    "env_file": "instant",
    "disk_space": "instant",
    "network": "1-3s (socket connect)",
    "anthropic_key": "1s network call, skipped if no key",
    "gemini_key": "1s network call, skipped if no key",
    "elevenlabs_key": "1s network call, skipped if no key",
}

CURRENT_CHECK = {"name": None}


def log_heartbeat(msg):
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    with open(HEARTBEAT_LOG, "a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")


def venv_python():
    if platform.system() == "Windows":
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


def load_env():
    env = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def result(status, message, fix=None):
    return {"status": status, "message": message, "fix": fix}


def preflight():
    """Doctor's own tiny preflight: can it even run and write its state?"""
    problems = []
    try:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        JOBS_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        problems.append(
            f"Cannot create logs/ or jobs/ under {ROOT}: {exc}. "
            f"Check folder permissions and re-run: python doctor.py"
        )
    return problems


def plan_dry_run():
    print("=== doctor.py --dry-run ===")
    print(f"Project root: {ROOT}\n")
    print("Planned checks (read-only, no changes made):")
    for check in CHECKS:
        print(f"  - {check:20s} ~{CHECK_ESTIMATES[check]}")
    print("\nNo changes have been made. Re-run without --dry-run to execute.")


def check_python_version(ctx):
    v = sys.version_info
    if v < (3, 10):
        return result(
            "FAIL", f"Python {v.major}.{v.minor}.{v.micro} (need 3.10+)",
            "Install Python 3.10 or newer, then re-run: python doctor.py",
        )
    return result("PASS", f"Python {platform.python_version()}")


def check_venv(ctx):
    vp = venv_python()
    if not vp.exists():
        return result(
            "FAIL", f"venv not found at {vp}",
            "Run: python setup.py",
        )
    try:
        r = subprocess.run([str(vp), "--version"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return result("FAIL", f"venv python did not run: {exc}", "Run: python setup.py")
    if r.returncode != 0:
        return result("FAIL", "venv python exists but failed to execute", "Run: python setup.py")
    return result("PASS", f"venv OK ({(r.stdout or r.stderr).strip()})")


def check_dependencies(ctx):
    vp = venv_python()
    if not vp.exists():
        return result("FAIL", "venv missing, cannot check dependencies", "Run: python setup.py")
    probe = (
        "import json\n"
        "mods = " + repr(IMPORT_NAME) + "\n"
        "out = {}\n"
        "for pkg, mod in mods.items():\n"
        "    try:\n"
        "        __import__(mod)\n"
        "        out[pkg] = None\n"
        "    except Exception as e:\n"
        "        out[pkg] = str(e)\n"
        "print(json.dumps(out))\n"
    )
    try:
        r = subprocess.run([str(vp), "-c", probe], capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        return result("FAIL", "dependency import probe timed out", "Run: python setup.py")
    try:
        out = json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return result(
            "FAIL", f"could not parse dependency probe output: {r.stdout[-500:]} {r.stderr[-500:]}",
            "Run: python setup.py",
        )
    failed = {pkg: err for pkg, err in out.items() if err}
    if failed:
        names = ", ".join(failed)
        return result(
            "FAIL", f"{len(failed)} package(s) not importable: {names}",
            "Run: python setup.py (re-installs requirements.txt into venv/)",
        )
    return result("PASS", f"all {len(IMPORT_NAME)} required packages importable")


def check_ffmpeg(ctx):
    found = shutil.which("ffmpeg")
    if not found:
        hint = FFMPEG_INSTALL_HINT.get(platform.system(), "install ffmpeg via your OS package manager")
        return result("FAIL", "ffmpeg not found on PATH", f"Install it: {hint}")
    try:
        r = subprocess.run([found, "-version"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as exc:
        hint = FFMPEG_INSTALL_HINT.get(platform.system(), "install ffmpeg via your OS package manager")
        return result("FAIL", f"ffmpeg found at {found} but would not run: {exc}", f"Reinstall it: {hint}")
    if r.returncode != 0:
        hint = FFMPEG_INSTALL_HINT.get(platform.system(), "install ffmpeg via your OS package manager")
        return result("FAIL", f"ffmpeg at {found} exited with an error", f"Reinstall it: {hint}")
    first_line = (r.stdout or r.stderr).splitlines()[0] if (r.stdout or r.stderr) else "ffmpeg"
    return result("PASS", f"{first_line} ({found})")


def check_tesseract(ctx):
    found = shutil.which("tesseract")
    if not found:
        hint = TESSERACT_INSTALL_HINT.get(platform.system(), "install tesseract-ocr via your OS package manager")
        return result(
            "WARN", "tesseract not found on PATH (free-tier OCR fallback only, easyocr is primary)",
            f"Optional: {hint}",
        )
    return result("PASS", f"found at {found}")


def check_playwright_browser(ctx):
    vp = venv_python()
    if not vp.exists():
        return result("FAIL", "venv missing, cannot check Playwright", "Run: python setup.py")
    probe = (
        "from playwright.sync_api import sync_playwright\n"
        "with sync_playwright() as p:\n"
        "    b = p.chromium.launch(headless=True)\n"
        "    page = b.new_page()\n"
        "    page.goto('about:blank')\n"
        "    b.close()\n"
        "print('OK')\n"
    )
    try:
        r = subprocess.run([str(vp), "-c", probe], capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        return result(
            "FAIL", "Playwright headless launch timed out",
            "Run: venv/Scripts/python.exe -m playwright install chromium (or venv/bin/python on macOS/Linux)",
        )
    if r.returncode != 0 or "OK" not in r.stdout:
        return result(
            "FAIL", f"Playwright could not launch a headless browser: {(r.stderr or r.stdout)[-500:]}",
            "Run: venv/Scripts/python.exe -m playwright install chromium (or venv/bin/python on macOS/Linux)",
        )
    return result("PASS", "headless chromium launched and closed cleanly")


def check_folders(ctx):
    missing = [f for f in FOLDERS if not (ROOT / f).is_dir()]
    if missing:
        return result(
            "FAIL", f"missing folder(s): {', '.join(missing)}",
            "Run: python setup.py",
        )
    return result("PASS", f"all {len(FOLDERS)} folders present")


def check_pipeline_config(ctx):
    if not CONFIG_PATH.exists():
        return result("FAIL", "pipeline_config.json missing", "Run: python setup.py")
    try:
        cfg = load_json(CONFIG_PATH)
    except (ValueError, OSError) as exc:
        return result(
            "FAIL", f"pipeline_config.json is not valid JSON: {exc}",
            "Fix or delete pipeline_config.json, then re-run: python setup.py",
        )
    if "config_version" not in cfg or "tier" not in cfg:
        return result(
            "WARN", "pipeline_config.json is missing expected fields (config_version/tier)",
            "Compare against a freshly generated config (delete and re-run python setup.py)",
        )
    ctx["config"] = cfg
    return result("PASS", f"valid, config_version={cfg.get('config_version')}, tier={cfg.get('tier')}")


def check_machine_profile(ctx):
    if not MACHINE_PROFILE_PATH.exists():
        return result("WARN", "machine_profile.json missing", "Run: python setup.py")
    try:
        load_json(MACHINE_PROFILE_PATH)
    except (ValueError, OSError) as exc:
        return result(
            "FAIL", f"machine_profile.json is not valid JSON: {exc}",
            "Delete machine_profile.json and re-run: python setup.py",
        )
    return result("PASS", "valid")


def check_env_file(ctx):
    if not ENV_PATH.exists():
        return result(
            "WARN", ".env missing (fine for free tier, required for any paid-tier stage)",
            "Run: python setup.py",
        )
    ctx["env"] = load_env()
    provided = [k for k, v in ctx["env"].items() if v]
    if provided:
        return result("PASS", f".env present, keys set: {', '.join(provided)}")
    return result("PASS", ".env present, no keys set (free tier)")


def check_disk_space(ctx):
    try:
        free_gb = shutil.disk_usage(str(ROOT)).free / (1024 ** 3)
    except OSError as exc:
        return result("WARN", f"could not check disk space: {exc}", "Check disk manually")
    if free_gb < DISK_FAIL_GB:
        return result(
            "FAIL", f"only {free_gb:.1f} GB free at {ROOT}",
            f"Free at least {DISK_FAIL_GB} GB before running any stage (video/audio output needs space).",
        )
    if free_gb < DISK_WARN_GB:
        return result(
            "WARN", f"only {free_gb:.1f} GB free at {ROOT}",
            f"Consider freeing space; long videos and intermediate frames can use tens of GB.",
        )
    return result("PASS", f"{free_gb:.1f} GB free at {ROOT}")


def check_network(ctx):
    targets = [("1.1.1.1", 443), ("8.8.8.8", 53)]
    for host, port in targets:
        try:
            with socket.create_connection((host, port), timeout=3):
                return result("PASS", f"internet reachable (connected to {host}:{port})")
        except OSError:
            continue
    return result(
        "FAIL", "no internet connection detected",
        "Check your network connection. Script generation, TTS, and chapter downloads all need network access.",
    )


def _key_configured(cfg, needle):
    return needle in json.dumps(cfg or {})


def check_anthropic_key(ctx):
    key = (ctx.get("env") or {}).get("ANTHROPIC_API_KEY", "")
    if not key:
        return result("SKIP", "no ANTHROPIC_API_KEY set (free tier does not need this)")
    if not _key_configured(ctx.get("config"), "claude"):
        return result("SKIP", "key set but no claude-based engine is configured")
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=key)
        list(client.models.list(limit=1))
    except Exception as exc:
        return result(
            "FAIL", f"ANTHROPIC_API_KEY did not validate: {exc}",
            "Check the key value in .env, or generate a new one at https://console.anthropic.com/",
        )
    return result("PASS", "ANTHROPIC_API_KEY validated (models.list)")


def check_gemini_key(ctx):
    key = (ctx.get("env") or {}).get("GEMINI_API_KEY", "")
    if not key:
        return result("SKIP", "no GEMINI_API_KEY set (free tier does not need this)")
    if not _key_configured(ctx.get("config"), "gemini"):
        return result("SKIP", "key set but no gemini-based engine is configured")
    try:
        from google import genai
        client = genai.Client(api_key=key)
        next(iter(client.models.list()), None)
    except Exception as exc:
        return result(
            "FAIL", f"GEMINI_API_KEY did not validate: {exc}",
            "Check the key value in .env, or generate a new one at https://aistudio.google.com/apikey",
        )
    return result("PASS", "GEMINI_API_KEY validated (models.list)")


def check_elevenlabs_key(ctx):
    key = (ctx.get("env") or {}).get("ELEVENLABS_API_KEY", "")
    if not key:
        return result("SKIP", "no ELEVENLABS_API_KEY set (free tier does not need this)")
    if not _key_configured(ctx.get("config"), "elevenlabs"):
        return result("SKIP", "key set but no elevenlabs-based engine is configured")
    try:
        import requests
        r = requests.get(
            "https://api.elevenlabs.io/v1/user",
            headers={"xi-api-key": key},
            timeout=10,
        )
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}")
    except Exception as exc:
        return result(
            "FAIL", f"ELEVENLABS_API_KEY did not validate: {exc}",
            "Check the key value in .env, or generate a new one at https://elevenlabs.io/app/settings/api-keys",
        )
    return result("PASS", "ELEVENLABS_API_KEY validated (GET /v1/user)")


CHECK_FUNCS = {
    "python_version": check_python_version,
    "venv": check_venv,
    "dependencies": check_dependencies,
    "ffmpeg": check_ffmpeg,
    "tesseract": check_tesseract,
    "playwright_browser": check_playwright_browser,
    "folders": check_folders,
    "pipeline_config": check_pipeline_config,
    "machine_profile": check_machine_profile,
    "env_file": check_env_file,
    "disk_space": check_disk_space,
    "network": check_network,
    "anthropic_key": check_anthropic_key,
    "gemini_key": check_gemini_key,
    "elevenlabs_key": check_elevenlabs_key,
}


def run_checks():
    """Run every check, write state, print the table. Returns (results_dict, hard_fail)."""
    ctx = {}
    results = {}
    log_heartbeat("[doctor] started")
    for name in CHECKS:
        CURRENT_CHECK["name"] = name
        results[name] = CHECK_FUNCS[name](ctx)
        mark_unit(STATE_PATH, name, results[name]["status"], **{
            k: v for k, v in results[name].items() if k != "status"
        })
    log_heartbeat("[doctor] finished")
    CURRENT_CHECK["name"] = None
    return results


def print_table(results):
    print(f"\n{'CHECK':22s} {'STATUS':6s} MESSAGE")
    print("-" * 90)
    for name, r in results.items():
        print(f"{name:22s} {r['status']:6s} {r['message']}")
        if r["status"] in ("WARN", "FAIL") and r.get("fix"):
            print(f"{'':22s} {'':6s} fix: {r['fix']}")
    print("-" * 90)
    counts = {"PASS": 0, "WARN": 0, "FAIL": 0, "SKIP": 0}
    for r in results.values():
        counts[r["status"]] += 1
    print(
        f"{counts['PASS']} passed, {counts['WARN']} warning(s), "
        f"{counts['FAIL']} failure(s), {counts['SKIP']} skipped\n"
    )


def main():
    parser = argparse.ArgumentParser(description="Diagnose this machine's readiness for the pipeline.")
    parser.add_argument("--dry-run", action="store_true", help="Print the plan and exit without checking anything.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON instead of a table.")
    args = parser.parse_args()

    if args.dry_run:
        plan_dry_run()
        return

    problems = preflight()
    if problems:
        print("Doctor's own preflight failed:\n")
        for p in problems:
            print(f"  - {p}")
        sys.exit(1)

    results = run_checks()

    if args.json:
        print(json.dumps(results, indent=2))
    else:
        print_table(results)

    if any(r["status"] == "FAIL" for r in results.values()):
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        with open(ERROR_LOG, "a", encoding="utf-8") as f:
            f.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] doctor.py failed\n")
            f.write(traceback.format_exc())
            f.write("\n")
        if CURRENT_CHECK["name"]:
            mark_unit(STATE_PATH, CURRENT_CHECK["name"], "FAILED", error=str(exc))
        print(f"\nDoctor crashed during check '{CURRENT_CHECK['name']}'.")
        print(f"What happened: {exc}")
        print(f"Details were saved to {ERROR_LOG}")
        print("Likely why: an unexpected environment issue doctor.py didn't anticipate.")
        print("What to do: read the traceback above, fix it, then resume with the exact same command:")
        print("    python doctor.py")
        sys.exit(1)
