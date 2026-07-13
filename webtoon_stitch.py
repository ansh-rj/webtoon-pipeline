#!/usr/bin/env python3
"""Stage 1b: stitch overlapping viewport segments into seamless chapter strip(s).
Usage: python webtoon_stitch.py --creator_id=X --series_id=Y --chapter_id=Z [--dry-run] [--force] [--cleanup-segments]

Reads segments from chapters/{creator}/{series}/{chapter}/segments/, aligns each
consecutive pair by template-matching (with pixel drift refinement around the
nominal overlap_pct), and writes deduplicated strip_####.png into stitched/.
"""
import argparse
import hashlib
import os
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from state_manager import atomic_write_json, load_json, mark_unit  # noqa: E402
import doctor  # noqa: E402

LOGS_DIR = ROOT / "logs"
JOBS_DIR = ROOT / "jobs"
ERROR_LOG = LOGS_DIR / "errors.log"
HEARTBEAT_LOG = LOGS_DIR / "heartbeat.log"
STATE_PATH = JOBS_DIR / "stitch_state.json"
CHAPTERS_DIR = ROOT / "chapters"
CONFIG_PATH = ROOT / "pipeline_config.json"

PREFLIGHT_CHECKS = ["python_version", "venv", "dependencies", "folders", "disk_space"]

# capture block supplies overlap_pct / viewport; these are stitch-only knobs.
DEFAULT_STITCH_CONFIG = {
    "match_confidence_threshold": 0.5,  # below this, fall back to nominal overlap
    "template_height": 180,             # rows sampled from the top of each segment
    "drift_margin_pct": 0.15,           # +/- search band around the nominal overlap
    "max_strip_height": 20000,          # split the canvas into multiple strips past this
    "cleanup_segments": False,          # delete segments/ after a successful stitch
}
DEFAULT_OVERLAP_PCT = 0.12

CURRENT_UNIT = {"name": None}


def log_heartbeat(msg):
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    with open(HEARTBEAT_LOG, "a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")


def load_stitch_config():
    """Ensure pipeline_config.json has a `stitch` block (added once, idempotent); return merged config incl. overlap_pct from `capture`."""
    cfg = load_json(CONFIG_PATH, default={}) or {}
    if "stitch" not in cfg:
        cfg["stitch"] = dict(DEFAULT_STITCH_CONFIG)
        atomic_write_json(CONFIG_PATH, cfg)
        print("[config] added default `stitch` block to pipeline_config.json")
    merged = dict(DEFAULT_STITCH_CONFIG)
    merged.update(cfg.get("stitch") or {})
    merged["overlap_pct"] = (cfg.get("capture") or {}).get("overlap_pct", DEFAULT_OVERLAP_PCT)
    return merged


def sanitize_id(name, label):
    if not name or "/" in name or "\\" in name or ".." in name:
        print(f"Invalid --{label}: {name!r} (must not contain '/', '\\\\', or '..')")
        sys.exit(1)
    return name


def segments_dir(creator_id, series_id, chapter_id):
    return CHAPTERS_DIR / creator_id / series_id / chapter_id / "segments"


def stitched_dir(creator_id, series_id, chapter_id):
    return CHAPTERS_DIR / creator_id / series_id / chapter_id / "stitched"


def list_segments(seg_dir):
    return sorted(seg_dir.glob("segment_*.png"))


def input_hash(segments):
    """Hash the segment set by name+size so a re-capture (which changes sizes) invalidates the stitch."""
    h = hashlib.sha256()
    for p in segments:
        h.update(p.name.encode("utf-8"))
        h.update(str(p.stat().st_size).encode("utf-8"))
    return h.hexdigest()[:16]


def preflight():
    print("Running preflight checks...")
    ctx = {}
    for name in PREFLIGHT_CHECKS:
        r = doctor.CHECK_FUNCS[name](ctx)
        print(f"  {name:20s} {r['status']:6s} {r['message']}")
        if r["status"] == "FAIL":
            print(f"\nPreflight failed on '{name}'.")
            print(f"What happened: {r['message']}")
            print("Likely why: environment is not fully set up for this pipeline.")
            if r.get("fix"):
                print(f"What to do: {r['fix']}")
            sys.exit(1)
    print("Preflight OK.\n")


def plan_dry_run(args, cfg, segments):
    seg_dir = segments_dir(args.creator_id, args.series_id, args.chapter_id)
    out_dir = stitched_dir(args.creator_id, args.series_id, args.chapter_id)
    print("=== webtoon_stitch.py --dry-run ===")
    print(f"Segments dir: {seg_dir}")
    print(f"Segments found: {len(segments)}")
    print(f"Output dir: {out_dir}")
    if not segments:
        print("Nothing to stitch: no segment_*.png found.")
        print("Run webtoon_capture.py for this chapter first.")
        return
    print(f"Nominal overlap: {cfg['overlap_pct']*100:.0f}% (refined per-pair by template match)")
    print(f"Confidence threshold: {cfg['match_confidence_threshold']} (below -> nominal fallback)")
    print(f"Max strip height: {cfg['max_strip_height']}px (taller canvas is split into multiple strips)")
    print(f"Cleanup segments after stitch: {cfg['cleanup_segments']}")
    # Rough estimate: each non-first segment contributes ~(1 - overlap) of its height.
    est_kept = 1 + (len(segments) - 1) * (1 - cfg["overlap_pct"])
    print(f"Estimated stitched height: ~{est_kept:.1f} segment-heights of pixels")
    print(f"Estimated disk: ~{len(segments)*0.4:.1f}-{len(segments)*1.0:.1f} MB of strip PNG(s)")
    print("\nNo changes have been made. Re-run without --dry-run to execute.")


def find_overlap(prev_gray, cur_gray, cfg):
    """Return (overlap_rows, confidence, used_fallback): how many top rows of cur duplicate the bottom of prev."""
    import cv2

    ph = prev_gray.shape[0]
    ch = cur_gray.shape[0]
    nominal = min(int(ph * cfg["overlap_pct"]), ph // 2, ch)
    template_h = max(1, min(cfg["template_height"], ch, ph))

    # Search the whole plausible overlap range, not just a narrow band around
    # nominal: the final segment is clamped by the capturer to an arbitrary
    # (often large) overlap, so a narrow band would miss it. cur's row 0 can
    # align anywhere from prev row (ph - min(ph,ch)) down to prev row (ph - template_h).
    template = cur_gray[0:template_h, :]
    s0 = max(0, ph - min(ph, ch))
    s1 = ph
    search = prev_gray[s0:s1, :]
    if search.shape[0] < template_h or search.shape[1] != template.shape[1]:
        return nominal, 0.0, True

    res = cv2.matchTemplate(search, template, cv2.TM_CCOEFF_NORMED)
    _, max_val, _, max_loc = cv2.minMaxLoc(res)
    if max_val < cfg["match_confidence_threshold"]:
        return nominal, float(max_val), True
    match_row = s0 + max_loc[1]
    overlap = max(0, min(ph - match_row, ch))
    return overlap, float(max_val), False


def stitch(args, cfg, segments):
    import cv2
    import numpy as np

    unit_name = f"{args.creator_id}/{args.series_id}/{args.chapter_id}"
    CURRENT_UNIT["name"] = unit_name
    ihash = input_hash(segments)
    out_dir = stitched_dir(args.creator_id, args.series_id, args.chapter_id)

    if not args.force:
        state = load_json(STATE_PATH, default={}) or {}
        existing = (state.get("units") or {}).get(unit_name)
        if existing and existing.get("status") == "DONE" and existing.get("input_hash") == ihash:
            strips = sorted(out_dir.glob("strip_*.png"))
            if strips and len(strips) == existing.get("strip_count"):
                print(f"Already stitched for {unit_name} (unchanged segments), skipping.")
                print("Use --force to re-stitch.")
                return

    mark_unit(STATE_PATH, unit_name, "IN_PROGRESS", input_hash=ihash)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Load all segments; normalise to a common width (crop to min).
    color = [cv2.imread(str(p), cv2.IMREAD_COLOR) for p in segments]
    for i, img in enumerate(color):
        if img is None:
            raise RuntimeError(f"Failed to read segment {segments[i].name} (corrupt or not a PNG).")
    min_w = min(img.shape[1] for img in color)
    color = [img[:, :min_w] for img in color]
    gray = [cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) for img in color]

    parts = [color[0]]  # first segment kept whole
    last_heartbeat = time.time()
    for i in range(1, len(color)):
        overlap, conf, fallback = find_overlap(gray[i - 1], gray[i], cfg)
        tag = f"fallback(conf={conf:.2f})" if fallback else f"conf={conf:.2f}"
        new_rows = color[i].shape[0] - overlap
        if new_rows <= 0:
            print(f"[stitch] segment {i+1:04d} fully contained in previous (overlap={overlap}, {tag}); skipped")
        else:
            parts.append(color[i][overlap:])
            print(f"[stitch] segment {i+1:04d} overlap={overlap}px new={new_rows}px {tag}")
        if time.time() - last_heartbeat > 30:
            log_heartbeat(f"[stitch] {unit_name} pair {i}/{len(color)-1}")
            last_heartbeat = time.time()

    canvas = np.vstack(parts)
    total_h = canvas.shape[0]

    # Split into multiple strips if taller than max_strip_height.
    max_h = int(cfg["max_strip_height"])
    bounds = list(range(0, total_h, max_h)) or [0]
    # Remove any pre-existing strips so a shorter re-stitch doesn't leave stale files.
    for old in out_dir.glob("strip_*.png"):
        old.unlink()
    strip_count = 0
    for idx, y0 in enumerate(bounds, start=1):
        y1 = min(y0 + max_h, total_h)
        chunk = canvas[y0:y1]
        strip_path = out_dir / f"strip_{idx:04d}.png"
        write_png_atomic(strip_path, chunk)
        print(f"[stitch] wrote {strip_path.name} ({chunk.shape[1]}x{chunk.shape[0]})")
        strip_count += 1

    if cfg["cleanup_segments"]:
        seg_dir = segments_dir(args.creator_id, args.series_id, args.chapter_id)
        for p in segments:
            p.unlink()
        print(f"[stitch] cleaned up {len(segments)} segment(s) from {seg_dir}")

    mark_unit(STATE_PATH, unit_name, "DONE", input_hash=ihash,
              strip_count=strip_count, total_height=int(total_h), output_dir=str(out_dir),
              segments_cleaned=bool(cfg["cleanup_segments"]))
    print(f"Done: {strip_count} strip(s), {total_h}px total, saved to {out_dir}")


def write_png_atomic(path, img):
    """Encode to PNG in memory, then temp-file + os.replace so a strip file is never half-written."""
    import cv2

    ok, buf = cv2.imencode(".png", img)
    if not ok:
        raise RuntimeError(f"cv2 failed to PNG-encode {path.name}")
    tmp = path.parent / f".{path.name}.tmp"
    with open(tmp, "wb") as f:
        f.write(buf.tobytes())
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def main():
    parser = argparse.ArgumentParser(description="Stitch overlapping webtoon segments into seamless chapter strip(s).")
    parser.add_argument("--creator_id", required=True)
    parser.add_argument("--series_id", required=True)
    parser.add_argument("--chapter_id", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true", help="Re-stitch even if a valid strip already exists for these segments.")
    parser.add_argument("--overlap-pct", type=float, default=None, help="Override nominal overlap fraction (0-1).")
    parser.add_argument("--cleanup-segments", action="store_true", help="Delete segments/ after a successful stitch.")
    args = parser.parse_args()

    args.creator_id = sanitize_id(args.creator_id, "creator_id")
    args.series_id = sanitize_id(args.series_id, "series_id")
    args.chapter_id = sanitize_id(args.chapter_id, "chapter_id")

    cfg = load_stitch_config()
    if args.overlap_pct is not None:
        cfg["overlap_pct"] = args.overlap_pct
    if args.cleanup_segments:
        cfg["cleanup_segments"] = True

    seg_dir = segments_dir(args.creator_id, args.series_id, args.chapter_id)
    segments = list_segments(seg_dir)

    if args.dry_run:
        plan_dry_run(args, cfg, segments)
        return

    if not segments:
        CURRENT_UNIT["name"] = f"{args.creator_id}/{args.series_id}/{args.chapter_id}"
        print(f"No segments to stitch in {seg_dir}.")
        print("Likely why: webtoon_capture.py has not run for this chapter yet.")
        print(f"What to do: python webtoon_capture.py --creator_id={args.creator_id} "
              f"--series_id={args.series_id} --chapter_id={args.chapter_id} --url=\"...\"")
        sys.exit(1)

    preflight()
    stitch(args, cfg, segments)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        with open(ERROR_LOG, "a", encoding="utf-8") as f:
            f.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] webtoon_stitch.py failed\n")
            f.write(traceback.format_exc())
            f.write("\n")
        if CURRENT_UNIT["name"]:
            mark_unit(STATE_PATH, CURRENT_UNIT["name"], "FAILED", error=str(exc))
        print(f"\nwebtoon_stitch.py crashed during '{CURRENT_UNIT['name']}'.")
        print(f"What happened: {exc}")
        print(f"Details were saved to {ERROR_LOG}")
        print("Likely why: a corrupt/mismatched segment image or a low-memory condition while building the canvas.")
        print("What to do: read the traceback above, fix it, then resume with the exact same command:")
        print(f"    python {Path(__file__).name} " + " ".join(sys.argv[1:]))
        sys.exit(1)
