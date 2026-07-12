#!/usr/bin/env python3
"""Stage 1: capture webtoon chapter pages as overlapping viewport screenshots.
Usage: python webtoon_capture.py --creator_id=X --series_id=Y --chapter_id=Z --url="..." [--dry-run] [--force]

Repointing this script at a different platform requires editing ALLOWED_DOMAINS
in this file directly -- it is intentionally not read from config.
"""
import argparse
import hashlib
import sys
import time
import traceback
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from state_manager import atomic_write_json, load_json, mark_unit  # noqa: E402
import doctor  # noqa: E402

# Hard-coded on purpose -- see module docstring. Do not read this from pipeline_config.json.
ALLOWED_DOMAINS = ["www.webtoons.com","webtoons.com"]

LOGS_DIR = ROOT / "logs"
JOBS_DIR = ROOT / "jobs"
ERROR_LOG = LOGS_DIR / "errors.log"
HEARTBEAT_LOG = LOGS_DIR / "heartbeat.log"
STATE_PATH = JOBS_DIR / "capture_state.json"
CHAPTERS_DIR = ROOT / "chapters"
AUTH_STATE_PATH = ROOT / "auth_state.json"
CONFIG_PATH = ROOT / "pipeline_config.json"

PREFLIGHT_CHECKS = ["python_version", "venv", "dependencies", "playwright_browser", "folders", "disk_space", "network"]

DEFAULT_CAPTURE_CONFIG = {
    "viewport_width": 1920,
    "viewport_height": 1080,
    "overlap_pct": 0.12,
    "scroll_settle_ms": 400,
    "image_load_timeout_ms": 15000,
    "max_segments": 400,
}

CURRENT_UNIT = {"name": None}


def log_heartbeat(msg):
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    with open(HEARTBEAT_LOG, "a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")


def load_capture_config():
    """Ensure pipeline_config.json has a `capture` block (added once, idempotent), return it merged with CLI-overridable defaults."""
    cfg = load_json(CONFIG_PATH, default={}) or {}
    if "capture" not in cfg:
        cfg["capture"] = dict(DEFAULT_CAPTURE_CONFIG)
        atomic_write_json(CONFIG_PATH, cfg)
        print("[config] added default `capture` block to pipeline_config.json")
    merged = dict(DEFAULT_CAPTURE_CONFIG)
    merged.update(cfg.get("capture") or {})
    return merged


def sanitize_id(name, label):
    if not name or "/" in name or "\\" in name or ".." in name:
        print(f"Invalid --{label}: {name!r} (must not contain '/', '\\\\', or '..')")
        sys.exit(1)
    return name


def check_domain_allowed(url):
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    return (parsed.hostname or "").lower()


def enforce_domain_guard(url):
    host = check_domain_allowed(url)
    if host not in [d.lower() for d in ALLOWED_DOMAINS]:
        print("Domain refused: this URL is not on the allow-list.")
        print(f"  URL host:        {host or '(unparseable)'}")
        print(f"  Allowed domains: {', '.join(ALLOWED_DOMAINS)}")
        print("Why: webtoon_capture.py hard-codes ALLOWED_DOMAINS to prevent accidentally")
        print("scraping a site this script was not authorized for.")
        print(f"Next step: repoint by editing ALLOWED_DOMAINS in {Path(__file__).name}, then re-run.")
        sys.exit(1)


def plan_dry_run(args, cap_cfg):
    print("=== webtoon_capture.py --dry-run ===")
    print(f"URL: {args.url}")
    print(f"Allowed domains: {', '.join(ALLOWED_DOMAINS)}")
    print(f"Output dir: {segments_dir(args.creator_id, args.series_id, args.chapter_id)}")
    print(f"Auth state: {'found, will reuse (headless)' if AUTH_STATE_PATH.exists() else 'MISSING -- will launch headful for manual login, then exit'}")
    print(f"Viewport: {cap_cfg['viewport_width']}x{cap_cfg['viewport_height']}, overlap {cap_cfg['overlap_pct']*100:.0f}%")
    print(f"Image load timeout: {cap_cfg['image_load_timeout_ms']}ms, max segments: {cap_cfg['max_segments']}")
    print("Estimate: page height is unknown until loaded; typical webtoon chapters produce ~15-40 segments.")
    print("Estimated time: ~1-3 minutes per chapter (network + render + lazy-load waits).")
    print("Estimated disk: ~200KB-1MB per PNG segment.")
    print("\nNo changes have been made. Re-run without --dry-run to execute.")


def segments_dir(creator_id, series_id, chapter_id):
    return CHAPTERS_DIR / creator_id / series_id / chapter_id / "segments"


def url_hash(url):
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


def preflight():
    print("Running preflight checks...")
    ctx = {}
    for name in PREFLIGHT_CHECKS:
        r = doctor.CHECK_FUNCS[name](ctx)
        status = r["status"]
        print(f"  {name:20s} {status:6s} {r['message']}")
        if status == "FAIL":
            print(f"\nPreflight failed on '{name}'.")
            print(f"What happened: {r['message']}")
            print("Likely why: environment is not fully set up for this pipeline.")
            if r.get("fix"):
                print(f"What to do: {r['fix']}")
            sys.exit(1)
    print("Preflight OK.\n")


def do_manual_login(url):
    from playwright.sync_api import sync_playwright

    print("No auth_state.json found -- manual login required.")
    print("A browser window will open. Log in on the site, then come back here and press Enter.")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(
            viewport={'width': 1920, 'height': 1080} #CHANGED 
        )
        page = context.new_page()

        page.set_viewport_size({"width": 1920, "height": 1080}) #CHANGED : ADDED LINE

        page.goto(url, wait_until="load", timeout=60000) #CHANGED from page.goto(url)

        input("Press Enter here once you have finished logging in... ")
        context.storage_state(path=str(AUTH_STATE_PATH))
        browser.close()
    print(f"Saved session to {AUTH_STATE_PATH}.")
    print("Re-run the same command to capture the chapter using this saved session:")
    print(f"    python {Path(__file__).name} " + " ".join(sys.argv[1:]))


def wait_for_images_loaded(page, timeout_ms):
    try:
        page.wait_for_function(
            "() => Array.from(document.images).every(img => img.complete && img.naturalHeight !== 0)",
            timeout=timeout_ms,
        )
    except Exception:
        print("  [warn] not all images reported loaded within timeout; capturing anyway")


def capture_chapter(args, cap_cfg):
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

    out_dir = segments_dir(args.creator_id, args.series_id, args.chapter_id)
    out_dir.mkdir(parents=True, exist_ok=True)

    uhash = url_hash(args.url)
    unit_name = f"{args.creator_id}/{args.series_id}/{args.chapter_id}"
    CURRENT_UNIT["name"] = unit_name

    if not args.force:
        state = load_json(STATE_PATH, default={}) or {}
        existing = (state.get("units") or {}).get(unit_name)
        if existing and existing.get("status") == "DONE" and existing.get("url_hash") == uhash:
            existing_segments = sorted(out_dir.glob("segment_*.png"))
            if existing_segments and len(existing_segments) == existing.get("segment_count"):
                print(f"Segments already captured for {unit_name} (unchanged URL), skipping.")
                print("Use --force to re-capture.")
                return

    mark_unit(STATE_PATH, unit_name, "IN_PROGRESS", url_hash=uhash)

    viewport_width = cap_cfg["viewport_width"]
    viewport_height = cap_cfg["viewport_height"]
    overlap_pct = cap_cfg["overlap_pct"]
    scroll_settle_ms = cap_cfg["scroll_settle_ms"]
    image_load_timeout_ms = cap_cfg["image_load_timeout_ms"]
    max_segments = cap_cfg["max_segments"]
    step = max(1, int(viewport_height * (1 - overlap_pct)))

    with sync_playwright() as p:  #CHANGE FROM HERE
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            storage_state=str(AUTH_STATE_PATH),
            viewport={'width': 1920, 'height': 1080},
            device_scale_factor=1
        )
        page = context.new_page()
        page.set_viewport_size({"width": 1920, "height": 1080}) #TO HERE

        print(f"[capture] navigating to {args.url}")

        page.goto(args.url, wait_until="load", timeout=60000) #CHANGED FROM page.goto(args.url, wait_until="load")

        wait_for_images_loaded(page, image_load_timeout_ms)

        scroll_y = 0
        prev_clamped = -1
        segment_index = 1
        last_heartbeat = time.time()

        while True:
            total_height = page.evaluate("document.documentElement.scrollHeight")
            max_scroll = max(0, total_height - viewport_height)
            clamped = min(scroll_y, max_scroll)
            if clamped == prev_clamped and segment_index > 1:
                break

            page.evaluate(f"window.scrollTo(0, {clamped})")
            page.wait_for_timeout(scroll_settle_ms)
            try:
                page.wait_for_load_state("networkidle", timeout=5000)
            except PlaywrightTimeoutError:
                pass


            # wait_for_images_loaded(page, image_load_timeout_ms) #CHANGED: COMMENTED THIS LINE
            #CHANGED: ADDED FROM 
            page.wait_for_function("""
                () => {
                    const images = Array.from(document.querySelectorAll('img'));
                    return images.every(img => img.complete && img.naturalWidth > 0);
                }
            """, timeout=15000)

            page.wait_for_timeout(200)
            #TO THIS

            seg_path = out_dir / f"segment_{segment_index:04d}.png"
            tmp_path = out_dir / f".segment_{segment_index:04d}.png.tmp"

            page.screenshot(path=str(tmp_path), type='png') #CHANGED: ADDED type
            tmp_path.replace(seg_path)
            print(f"[capture] segment {segment_index:04d} saved (scroll_y={clamped}, page_height={total_height})")

            prev_clamped = clamped
            segment_index += 1
            if segment_index > max_segments:
                raise RuntimeError(
                    f"Exceeded max_segments ({max_segments}) without reaching the bottom of the page. "
                    "The page may be infinitely scrolling or the height detection is wrong."
                )
            if clamped >= max_scroll:
                break
            scroll_y = clamped + step

            if time.time() - last_heartbeat > 30:
                log_heartbeat(f"[capture] {unit_name} at segment {segment_index}")
                last_heartbeat = time.time()

        browser.close()

    segment_count = segment_index - 1
    mark_unit(STATE_PATH, unit_name, "DONE", url_hash=uhash, segment_count=segment_count, output_dir=str(out_dir))
    print(f"Done: {segment_count} segments saved to {out_dir}")


def main():
    parser = argparse.ArgumentParser(description="Capture a webtoon chapter as overlapping viewport screenshots.")
    parser.add_argument("--creator_id", required=True)
    parser.add_argument("--series_id", required=True)
    parser.add_argument("--chapter_id", required=True)
    parser.add_argument("--url", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true", help="Re-capture even if valid segments already exist for this URL.")
    parser.add_argument("--viewport-width", type=int, default=None)
    parser.add_argument("--viewport-height", type=int, default=None)
    parser.add_argument("--overlap-pct", type=float, default=None)
    args = parser.parse_args()

    enforce_domain_guard(args.url)

    args.creator_id = sanitize_id(args.creator_id, "creator_id")
    args.series_id = sanitize_id(args.series_id, "series_id")
    args.chapter_id = sanitize_id(args.chapter_id, "chapter_id")

    cap_cfg = load_capture_config()
    if args.viewport_width is not None:
        cap_cfg["viewport_width"] = args.viewport_width
    if args.viewport_height is not None:
        cap_cfg["viewport_height"] = args.viewport_height
    if args.overlap_pct is not None:
        cap_cfg["overlap_pct"] = args.overlap_pct

    if args.dry_run:
        plan_dry_run(args, cap_cfg)
        return

    preflight()

    if not AUTH_STATE_PATH.exists():
        CURRENT_UNIT["name"] = "auth"
        do_manual_login(args.url)
        return

    capture_chapter(args, cap_cfg)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        with open(ERROR_LOG, "a", encoding="utf-8") as f:
            f.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] webtoon_capture.py failed\n")
            f.write(traceback.format_exc())
            f.write("\n")
        if CURRENT_UNIT["name"]:
            mark_unit(STATE_PATH, CURRENT_UNIT["name"], "FAILED", error=str(exc))
        print(f"\nwebtoon_capture.py crashed during '{CURRENT_UNIT['name']}'.")
        print(f"What happened: {exc}")
        print(f"Details were saved to {ERROR_LOG}")
        print("Likely why: an unexpected page structure, network issue, or environment problem.")
        print("What to do: read the traceback above, fix it, then resume with the exact same command:")
        print(f"    python {Path(__file__).name} " + " ".join(sys.argv[1:]))
        sys.exit(1)
