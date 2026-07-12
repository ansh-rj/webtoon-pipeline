#!/usr/bin/env python3
"""One-command environment installer for the webtoon video pipeline.
Usage: python setup.py [--dry-run] [--non-interactive]
Safe to re-run: each unit skips itself if its output already exists / is valid.
"""
import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import threading
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
STATE_PATH = JOBS_DIR / "setup_state.json"
VENV_DIR = ROOT / "venv"
REQUIREMENTS_PATH = ROOT / "requirements.txt"
CONFIG_PATH = ROOT / "pipeline_config.json"
MACHINE_PROFILE_PATH = ROOT / "machine_profile.json"
ENV_PATH = ROOT / ".env"

REQUIRED_PY = (3, 10)

FOLDERS = ["chapters", "projects", "assets/music", "assets/end_cards", "logs", "jobs"]

REQUIREMENTS = [
    "playwright",
    "opencv-python",
    "pillow",
    "easyocr",
    "pytesseract",
    "edge-tts",
    "ffmpeg-python",
    "faster-whisper",
    "anthropic",
    "google-genai",
    "requests",
    "psutil",
]

UNITS = [
    "python_check",
    "venv",
    "requirements",
    "playwright_chromium",
    "ffmpeg_check",
    "folders",
    "pipeline_config",
    "machine_profile",
    "api_keys",
]

FFMPEG_INSTALL_HINT = {
    "Windows": "winget install ffmpeg",
    "Darwin": "brew install ffmpeg",
    "Linux": "sudo apt install ffmpeg",
}

# Approximate disk cost for --dry-run reporting only.
UNIT_ESTIMATES = {
    "python_check": ("instant", "0 MB"),
    "venv": ("~10s", "~20 MB"),
    "requirements": ("2-6 min (network dependent)", "~2.5 GB (easyocr/torch are large)"),
    "playwright_chromium": ("~30-60s", "~300 MB"),
    "ffmpeg_check": ("instant", "0 MB"),
    "folders": ("instant", "0 MB"),
    "pipeline_config": ("instant", "<1 MB"),
    "machine_profile": ("instant", "<1 MB"),
    "api_keys": ("interactive", "0 MB"),
}

CURRENT_UNIT = {"name": None}


def log_heartbeat(msg):
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    with open(HEARTBEAT_LOG, "a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")


class Heartbeat:
    """Appends a heartbeat line every ~30s while a long-running unit is active."""

    def __init__(self, label, interval=30):
        self.label = label
        self.interval = interval
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        while not self._stop.wait(self.interval):
            log_heartbeat(f"[{self.label}] still running...")

    def __enter__(self):
        log_heartbeat(f"[{self.label}] started")
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join(timeout=1)
        log_heartbeat(f"[{self.label}] finished")


def sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def venv_python():
    if platform.system() == "Windows":
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


def venv_bin_dir():
    return VENV_DIR / ("Scripts" if platform.system() == "Windows" else "bin")


def run_streamed(cmd, label):
    """Run a subprocess while a heartbeat thread logs progress every ~30s."""
    with Heartbeat(label):
        result = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"Command failed ({label}): {' '.join(cmd)}\n"
            f"--- stdout ---\n{result.stdout[-4000:]}\n"
            f"--- stderr ---\n{result.stderr[-4000:]}"
        )
    return result


def preflight(non_interactive):
    problems = []
    if sys.version_info < REQUIRED_PY:
        problems.append(
            f"Python {REQUIRED_PY[0]}.{REQUIRED_PY[1]}+ is required, found "
            f"{sys.version_info.major}.{sys.version_info.minor}. Install a newer Python "
            f"and re-run: python setup.py"
        )
    try:
        free_bytes = shutil.disk_usage(str(ROOT)).free
        if free_bytes < 3 * 1024 ** 3:
            problems.append(
                f"Only {free_bytes / (1024**3):.1f} GB free at {ROOT}. Setup needs ~3 GB "
                f"(easyocr/torch are large). Free up space and re-run: python setup.py"
            )
    except OSError:
        pass
    return problems


def plan_dry_run(non_interactive):
    print("=== setup.py --dry-run ===")
    print(f"OS: {platform.system()} {platform.release()}  Python: {platform.python_version()}")
    print(f"Project root: {ROOT}\n")
    print("Planned units:")
    for unit in UNITS:
        est_time, est_disk = UNIT_ESTIMATES[unit]
        print(f"  - {unit:22s} time~{est_time:30s} disk~{est_disk}")
    print("\nNo changes have been made. Re-run without --dry-run to execute.")


def unit_python_check(state, ctx):
    if sys.version_info < REQUIRED_PY:
        raise RuntimeError(
            f"Python {REQUIRED_PY[0]}.{REQUIRED_PY[1]}+ required, found "
            f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}."
        )
    return {"python_version": platform.python_version()}


def unit_venv(state, ctx):
    if venv_python().exists():
        return {"skipped": True, "reason": "venv already exists"}
    import venv as venv_mod

    venv_mod.EnvBuilder(with_pip=True).create(str(VENV_DIR))
    if not venv_python().exists():
        raise RuntimeError(f"venv creation reported success but {venv_python()} is missing.")
    return {"created": str(VENV_DIR)}


def unit_requirements(state, ctx):
    content = "\n".join(REQUIREMENTS) + "\n"
    tmp_exists = REQUIREMENTS_PATH.exists()
    if not tmp_exists or REQUIREMENTS_PATH.read_text(encoding="utf-8") != content:
        REQUIREMENTS_PATH.write_text(content, encoding="utf-8")
    req_hash = sha256_text(content)

    prev = (state.get("units", {}).get("requirements") or {})
    if prev.get("status") == "SUCCESS" and prev.get("req_hash") == req_hash:
        return {"skipped": True, "reason": "requirements.txt unchanged since last install", "req_hash": req_hash}

    run_streamed(
        [str(venv_python()), "-m", "pip", "install", "--upgrade", "pip"],
        "pip upgrade",
    )
    run_streamed(
        [str(venv_python()), "-m", "pip", "install", "-r", str(REQUIREMENTS_PATH)],
        "pip install requirements",
    )
    return {"req_hash": req_hash}


def unit_playwright_chromium(state, ctx):
    run_streamed(
        [str(venv_python()), "-m", "playwright", "install", "chromium"],
        "playwright install chromium",
    )
    return {}


def unit_ffmpeg_check(state, ctx):
    found = shutil.which("ffmpeg")
    if found:
        return {"found": found}
    hint = FFMPEG_INSTALL_HINT.get(platform.system(), "install ffmpeg via your OS package manager")
    print(f"\n[WARNING] ffmpeg was not found on PATH. Install it with:\n    {hint}\n")
    return {"found": None, "install_hint": hint}


def unit_folders(state, ctx):
    for folder in FOLDERS:
        (ROOT / folder).mkdir(parents=True, exist_ok=True)
    return {"folders": FOLDERS}


def default_pipeline_config():
    return {
        "mode": "full",
        "tier": "paid",
        "tier_defaults": {
            "free": {
                "extraction_engine": "easyocr",
                "tts_engine": "edge_tts",
                "extraction_fallback": "tesseract",
                "tts_fallback": "piper",
            },
            "paid": {
                "extraction_engine": "claude_vision",
                "tts_engine": "elevenlabs",
                "extraction_fallback": "gemini_vision",
                "tts_fallback": "openai_tts",
            },
        },
        "extraction": {"tier": "auto", "engine": "auto"},
        "tts": {"tier": "auto", "engine": "auto"},
        "script_generation": {"tier": "auto"},
        "compilation": {"tier": "auto"},
        "digest": {"tier": "auto"},
        "script_provider": "claude",
        "longform_script_provider": "gemini",
        "compilation_provider": "gemini",
        "filler_tolerance": 0.1,
        "compilation_mode": "budget",
        "target_minutes": 90,
        "chapter_range": None,
        "recap_density": "balanced",
        "narration_style": "dramatic",
        "retention": {
            "midroll_rehook_minutes": 12,
            "open_loop_teasers": True,
            "act_recap_lines": True,
        },
        "audio": {
            "music_enabled": True,
            "music_folder": "assets/music/",
            "music_duck_db": -14,
            "loudness_target_lufs": -14,
            "trim_silence_over_ms": 900,
        },
        "motion": {"ken_burns": True, "max_zoom": 1.08},
        "split_into_parts": {"enabled": False, "max_part_minutes": 120},
        "generate_trailer_cut": True,
        "trailer_cut_seconds": 90,
        "thumbnail": {"enabled": True, "variants": 2},
        "render_quality": "final",
        "fallback_enabled": True,
        "cost_guard": {
            "max_spend_usd": 25.0,
            "warn_at_pct": 80,
            "require_estimate_ack": True,
        },
        "review_gates": {
            "after_compilation_outline": True,
            "after_full_script": True,
        },
        "unattended": False,
        "notifications": {"desktop": True, "webhook_url": None},
        "voices": {
            "narrator": "Default Narrator",
            "male": "Default Male",
            "female": "Default Female",
        },
        "end_card_variant": "default",
        "machine_profile": "auto",
        "parallel_workers": "auto",
        "config_version": 8,
    }


def unit_pipeline_config(state, ctx):
    if CONFIG_PATH.exists():
        return {"skipped": True, "reason": "pipeline_config.json already exists, left untouched"}
    atomic_write_json(CONFIG_PATH, default_pipeline_config())
    return {"created": str(CONFIG_PATH)}


def unit_machine_profile(state, ctx):
    # psutil lives in venv/, not in the interpreter running setup.py, so probe via venv python.
    probe = (
        "import json, psutil; "
        "print(json.dumps({'cores': psutil.cpu_count(logical=False) or psutil.cpu_count(logical=True) or 2, "
        "'ram_bytes': psutil.virtual_memory().total}))"
    )
    result = run_streamed([str(venv_python()), "-c", probe], "machine probe")
    probed = json.loads(result.stdout.strip().splitlines()[-1])
    cores = probed["cores"]
    ram_gb = probed["ram_bytes"] / (1024 ** 3)
    parallel_workers = 2 if ram_gb <= 8 else max(1, cores // 2)
    resolution = "720p" if ram_gb <= 8 else "1080p"
    profile = {
        "os": platform.system(),
        "cpu_cores_physical": cores,
        "ram_gb": round(ram_gb, 1),
        "parallel_workers": parallel_workers,
        "render_defaults": {
            "resolution": resolution,
            "encode_preset": "veryfast" if ram_gb <= 8 else "medium",
        },
    }
    atomic_write_json(MACHINE_PROFILE_PATH, profile)
    return profile


def unit_api_keys(state, ctx):
    if ENV_PATH.exists():
        return {"skipped": True, "reason": ".env already exists, left untouched"}

    keys = {"ANTHROPIC_API_KEY": "", "ELEVENLABS_API_KEY": "", "GEMINI_API_KEY": ""}
    if not ctx["non_interactive"] and sys.stdin.isatty():
        print("\nAPI keys are optional. The free tier works with none of these. Press Enter to skip any.")
        for key_name in keys:
            try:
                keys[key_name] = input(f"  {key_name}: ").strip()
            except EOFError:
                break
    else:
        print("\n[non-interactive] Skipping API key prompts. Edit .env by hand to add keys later.")

    lines = [f"{k}={v}" for k, v in keys.items()]
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        os.chmod(ENV_PATH, 0o600)
    except OSError:
        pass
    provided = [k for k, v in keys.items() if v]
    return {"created": str(ENV_PATH), "keys_provided": provided}


UNIT_FUNCS = {
    "python_check": unit_python_check,
    "venv": unit_venv,
    "requirements": unit_requirements,
    "playwright_chromium": unit_playwright_chromium,
    "ffmpeg_check": unit_ffmpeg_check,
    "folders": unit_folders,
    "pipeline_config": unit_pipeline_config,
    "machine_profile": unit_machine_profile,
    "api_keys": unit_api_keys,
}


def run_setup(non_interactive):
    state = load_json(STATE_PATH, default={}) or {}
    ctx = {"non_interactive": non_interactive}

    for unit in UNITS:
        CURRENT_UNIT["name"] = unit
        already = (state.get("units", {}).get(unit) or {}).get("status")
        print(f"\n--- {unit} ---")
        result = UNIT_FUNCS[unit](state, ctx)
        if result.get("skipped"):
            print(f"  skipped: {result.get('reason')}")
        else:
            print(f"  done: {json.dumps({k: v for k, v in result.items() if k != 'keys_provided'})}")
        state = mark_unit(STATE_PATH, unit, "SUCCESS", **result)

    print("\n=== setup.py complete ===")
    print(f"Activate the venv with:")
    if platform.system() == "Windows":
        print(f"    {venv_bin_dir()}\\activate")
    else:
        print(f"    source {venv_bin_dir()}/activate")


def main():
    parser = argparse.ArgumentParser(description="Prepare this machine for the webtoon video pipeline.")
    parser.add_argument("--dry-run", action="store_true", help="Print the plan and exit without changing anything.")
    parser.add_argument("--non-interactive", action="store_true", help="Never prompt; skip API key entry.")
    args = parser.parse_args()

    if args.dry_run:
        plan_dry_run(args.non_interactive)
        return

    problems = preflight(args.non_interactive)
    if problems:
        print("Preflight checks failed:\n")
        for p in problems:
            print(f"  - {p}")
        sys.exit(1)

    run_setup(args.non_interactive)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        with open(ERROR_LOG, "a", encoding="utf-8") as f:
            f.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] setup.py failed\n")
            f.write(traceback.format_exc())
            f.write("\n")
        if CURRENT_UNIT["name"]:
            mark_unit(STATE_PATH, CURRENT_UNIT["name"], "FAILED", error=str(exc))
        print(f"\nSetup failed during step '{CURRENT_UNIT['name']}'.")
        print(f"What happened: {exc}")
        print(f"Details were saved to {ERROR_LOG}")
        print("Likely why: a network hiccup, missing system dependency, or a permissions issue.")
        print("What to do: fix the issue above, then resume with the exact same command:")
        print("    python setup.py")
        sys.exit(1)
