#!/usr/bin/env python3
"""Stage 2: split a stitched strip.png into ordered panel crops.

My chapters are CONTINUOUS vertical art with no blank gutters between panels, so
gap-detection can't find cut lines. Primary mode is "segment": slice the strip into
fixed-height pieces sized to a target aspect ratio, with a small overlap, choosing the
LEAST-BUSY horizontal row within a search window around each target cut so we avoid
slicing through a face or speech bubble. A "gap" mode (blank-row splitting) is kept for
any future guttered series. Selected via pipeline_config.json `panel_split.mode`.

Usage:
  python panel_splitter.py --creator_id=X --series_id=Y --chapter_id=Z [--dry-run]
                           [--force] [--mode segment|gap] [--aspect 9:16]
                           [--overlap-pct 0.05] [--search-window-pct 0.15]
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
STATE_PATH = JOBS_DIR / "panel_split_state.json"
CHAPTERS_DIR = ROOT / "chapters"
CONFIG_PATH = ROOT / "pipeline_config.json"

PREFLIGHT_CHECKS = ["python_version", "venv", "dependencies", "folders", "disk_space"]

# Pure computer-vision stage (like capture): no AI engine, so no tier/engine field.
DEFAULT_SPLIT_CONFIG = {
    "mode": "segment",           # "segment" (continuous art, default) or "gap" (guttered art)
    # --- segment mode ---
    "target_aspect_w": 9,        # target crop aspect (w:h); 9:16 = tall portrait
    "target_aspect_h": 16,
    "overlap_pct": 0.05,         # adjacent panels share this fraction so nothing is lost at a seam
    "search_window_pct": 0.40,   # search +/- this fraction of segment height for the calmest cut row
                                 # (needs to be wide: a tall face/bubble panel can be >25% before a calm row)
    "high_detail_mult": 0.33,    # flag a cut whose row-cost exceeds this multiple of the strip's MEDIAN row-cost;
                                 # chosen cuts are local minima, so comparing to median (not a high quantile) is what catches a face/bubble seam
    "min_last_panel_frac": 0.30, # a trailing sliver shorter than this fraction is merged into the prior panel
    # --- gap mode (guttered series only) ---
    "gap_row_std_max": 8.0,      # a row counts as background/gutter below this per-row std
    "gap_min_px": 24,            # a background run this tall or taller is a real gutter
    "gap_min_panel_px": 200,     # panels shorter than this are merged forward
}

CURRENT_UNIT = {"name": None}


def log_heartbeat(msg):
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    with open(HEARTBEAT_LOG, "a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")


def load_split_config():
    """Ensure pipeline_config.json has a `panel_split` block (added once, idempotent); return it merged over defaults."""
    cfg = load_json(CONFIG_PATH, default={}) or {}
    if "panel_split" not in cfg:
        cfg["panel_split"] = dict(DEFAULT_SPLIT_CONFIG)
        atomic_write_json(CONFIG_PATH, cfg)
        print("[config] added default `panel_split` block to pipeline_config.json")
    merged = dict(DEFAULT_SPLIT_CONFIG)
    merged.update(cfg.get("panel_split") or {})
    return merged


def sanitize_id(name, label):
    if not name or "/" in name or "\\" in name or ".." in name:
        print(f"Invalid --{label}: {name!r} (must not contain '/', '\\\\', or '..')")
        sys.exit(1)
    return name


def strip_path_for(creator_id, series_id, chapter_id):
    return CHAPTERS_DIR / creator_id / series_id / chapter_id / "strip.png"


def panels_dir_for(creator_id, series_id, chapter_id):
    return CHAPTERS_DIR / creator_id / series_id / chapter_id / "panels"


def file_hash(path):
    """sha256 of the file so an unchanged strip skips re-splitting; a re-stitch invalidates it."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def strip_size(path):
    """(width, height) from the PNG header only -- no full decode (used by --dry-run)."""
    from PIL import Image
    with Image.open(path) as im:
        return im.size  # (w, h)


def row_cost_curve(gray):
    """Per-row 'busyness': mean gradient magnitude, lightly smoothed.

    A horizontal cut through a face, text, or bubble edge crosses strong gradients, so
    low row-cost = a calm band (flat colour / gentle art) that is safe to cut on. We
    smooth over a few rows so we pick a genuinely quiet band, not a 1px noise dip.
    """
    import cv2
    import numpy as np

    g = gray.astype(np.float32)
    gx = cv2.Sobel(g, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(g, cv2.CV_32F, 0, 1, ksize=3)
    cost = (np.abs(gx) + np.abs(gy)).mean(axis=1)
    k = 7
    return np.convolve(cost, np.ones(k) / k, mode="same")


def plan_segment_cuts(H, W, cfg, row_cost):
    """Return (panels, seg_h, flag_thr). panels = list of (y0, y1, flagged, cost).

    Each panel is ~seg_h tall (from the target aspect). The bottom cut is placed on the
    least-busy row within +/- search window of the ideal boundary; adjacent panels
    overlap by overlap_pct so a subject straddling a seam survives in one of them.
    """
    import numpy as np

    seg_h = max(1, int(round(W * cfg["target_aspect_h"] / cfg["target_aspect_w"])))
    overlap_px = int(round(seg_h * cfg["overlap_pct"]))
    win = int(round(seg_h * cfg["search_window_pct"]))
    # Flag relative to the strip's MEDIAN row-cost: a cut lands on a local minimum, so a
    # high-quantile reference would never trip. If even the calmest row in range is well
    # above typical art (median), the cut is slicing a face/bubble -- flag it.
    flag_thr = float(np.median(row_cost)) * cfg["high_detail_mult"]

    panels = []
    start = 0
    while start < H:
        target = start + seg_h
        if target >= H:  # last panel runs to the end, no cut search
            panels.append((start, H, False, 0.0))
            break
        lo = max(start + 1, target - win)
        hi = min(H - 1, target + win)
        cut = lo + int(np.argmin(row_cost[lo:hi + 1]))
        cost = float(row_cost[cut])
        panels.append((start, cut, cost > flag_thr, cost))
        nxt = cut - overlap_px
        start = nxt if nxt > start else start + seg_h  # guarantee forward progress

    # Merge a trailing sliver into the previous panel.
    if len(panels) > 1:
        y0, y1, _, _ = panels[-1]
        if (y1 - y0) < seg_h * cfg["min_last_panel_frac"]:
            p0, _, pf, pc = panels[-2]
            panels[-2] = (p0, y1, pf, pc)
            panels.pop()
    return panels, seg_h, flag_thr


def plan_gap_cuts(gray, cfg):
    """Guttered-art mode: cut at the middle of each blank (low-texture) horizontal band."""
    import numpy as np

    H = gray.shape[0]
    std = gray.astype(np.float32).std(axis=1)
    bg = std < cfg["gap_row_std_max"]

    gutters, i = [], 0
    while i < H:
        if bg[i]:
            j = i
            while j < H and bg[j]:
                j += 1
            if j - i >= cfg["gap_min_px"]:
                gutters.append((i + j) // 2)
            i = j
        else:
            i += 1

    bounds = [0] + gutters + [H]
    panels = []
    for a, b in zip(bounds[:-1], bounds[1:]):
        if panels and (b - panels[-1][0]) and (a - panels[-1][1]) < 0:
            continue
        panels.append((a, b, False, 0.0))
    # Merge panels shorter than the minimum forward into the next.
    merged = []
    for y0, y1, f, c in panels:
        if merged and (merged[-1][1] - merged[-1][0]) < cfg["gap_min_panel_px"]:
            p0 = merged.pop()[0]
            merged.append((p0, y1, f, c))
        else:
            merged.append((y0, y1, f, c))
    return merged, len(gutters)


def write_panels(panels, img, out_dir):
    """Crop each (y0,y1) from img and write panels/panel_###.png atomically, in order."""
    import cv2

    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("panel_*.png"):  # clear stale panels so a shorter re-split leaves no orphans
        old.unlink()

    last_heartbeat = time.time()
    paths = []
    for n, (y0, y1, _flag, _cost) in enumerate(panels, start=1):
        crop = img[y0:y1]
        ok, buf = cv2.imencode(".png", crop)
        if not ok:
            raise RuntimeError(f"cv2 failed to PNG-encode panel {n}.")
        out = out_dir / f"panel_{n:03d}.png"
        tmp = out_dir / f".panel_{n:03d}.png.tmp"
        with open(tmp, "wb") as f:
            f.write(buf.tobytes())
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, out)
        paths.append(out)
        if time.time() - last_heartbeat > 30:
            log_heartbeat(f"[split] wrote panel {n}/{len(panels)}")
            last_heartbeat = time.time()
    return paths


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


def parse_aspect(s):
    try:
        w, h = s.lower().split(":")
        return int(w), int(h)
    except Exception:
        print(f"Invalid --aspect {s!r}; expected W:H like 9:16.")
        sys.exit(1)


def plan_dry_run(args, cfg):
    strip = strip_path_for(args.creator_id, args.series_id, args.chapter_id)
    print("=== panel_splitter.py --dry-run ===")
    print(f"Strip: {strip}")
    if not strip.exists():
        print("Strip NOT found -- run capture + --stitch for this chapter first.")
        return
    w, h = strip_size(strip)
    print(f"Strip size: {w}x{h}")
    print(f"Mode: {cfg['mode']}")
    print(f"Output dir: {panels_dir_for(args.creator_id, args.series_id, args.chapter_id)}")
    if cfg["mode"] == "segment":
        seg_h = max(1, int(round(w * cfg["target_aspect_h"] / cfg["target_aspect_w"])))
        overlap_px = int(round(seg_h * cfg["overlap_pct"]))
        step = max(1, seg_h - overlap_px)
        est = max(1, -(-h // step))  # ceil
        print(f"Target aspect: {cfg['target_aspect_w']}:{cfg['target_aspect_h']} "
              f"-> segment height ~{seg_h}px, overlap {overlap_px}px, "
              f"search +/-{int(round(seg_h * cfg['search_window_pct']))}px")
        print(f"Estimated panels: ~{est}")
        print(f"Estimated disk: ~{est * (w * seg_h * 3) // (1024 * 1024)}MB uncompressed / far less as PNG.")
    else:
        print(f"Gap mode: cut at blank bands (row std < {cfg['gap_row_std_max']}, "
              f">= {cfg['gap_min_px']}px), min panel {cfg['gap_min_panel_px']}px.")
    print("\nNo changes have been made. Re-run without --dry-run to split.")


def split_chapter(args, cfg):
    import cv2

    strip = strip_path_for(args.creator_id, args.series_id, args.chapter_id)
    out_dir = panels_dir_for(args.creator_id, args.series_id, args.chapter_id)
    unit_name = f"{args.creator_id}/{args.series_id}/{args.chapter_id}"
    CURRENT_UNIT["name"] = unit_name

    if not strip.exists():
        print(f"No strip.png for {unit_name} at {strip}.")
        print("Likely why: capture + stitch has not produced a strip for this chapter yet.")
        print("What to do: run capture, then --stitch, for this chapter first.")
        sys.exit(1)

    ihash = file_hash(strip)
    if not args.force:
        state = load_json(STATE_PATH, default={}) or {}
        existing = (state.get("units") or {}).get(unit_name)
        if existing and existing.get("status") == "DONE" and existing.get("input_hash") == ihash:
            have = sorted(out_dir.glob("panel_*.png"))
            if have and len(have) == existing.get("panel_count"):
                print(f"Panels already split for {unit_name} (unchanged strip), skipping.")
                print("Use --force to re-split.")
                return

    mark_unit(STATE_PATH, unit_name, "IN_PROGRESS", input_hash=ihash)
    print(f"[split] reading {strip}")
    img = cv2.imread(str(strip), cv2.IMREAD_COLOR)
    if img is None:
        raise RuntimeError(f"Failed to read {strip} (corrupt or not a PNG).")
    H, W = img.shape[:2]
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    print(f"[split] strip {W}x{H}, mode={cfg['mode']}")

    if cfg["mode"] == "gap":
        panels, n_gutters = plan_gap_cuts(gray, cfg)
        print(f"[split] gap mode: found {n_gutters} gutter(s) -> {len(panels)} panel(s).")
        if n_gutters == 0:
            print("[split] WARNING: no blank gutters found. This art is likely continuous --")
            print("  set panel_split.mode to \"segment\" in pipeline_config.json for this series.")
        flagged = []
    else:
        log_heartbeat(f"[split] {unit_name} computing row cost")
        row_cost = row_cost_curve(gray)
        panels, seg_h, flag_thr = plan_segment_cuts(H, W, cfg, row_cost)
        flagged = [n for n, p in enumerate(panels, 1) if p[2]]
        print(f"[split] segment mode: {len(panels)} panel(s), target height ~{seg_h}px, "
              f"high-detail flag threshold row-cost {flag_thr:.1f}.")

    # Per-panel table of chosen cut positions.
    print("\n  panel   y0       y1       height   cut_cost  flag")
    print("  -----   ------   ------   ------   --------  ----")
    for n, (y0, y1, flag, cost) in enumerate(panels, start=1):
        print(f"  {n:5d}   {y0:6d}   {y1:6d}   {y1-y0:6d}   {cost:8.1f}  {'HIGH' if flag else ''}")

    paths = write_panels(panels, img, out_dir)

    if flagged:
        print(f"\n[split] {len(flagged)} panel(s) had to cut through a high-detail region "
              f"(no calm row in range): {flagged}")
        print("  These seams may clip a face/bubble; the overlap keeps the subject whole in the neighbour panel.")
    else:
        print("\n[split] all cuts landed on calm rows (no high-detail seams flagged).")
    print(f"[split] wrote {len(paths)} panel(s) to {out_dir}")

    mark_unit(STATE_PATH, unit_name, "DONE", input_hash=ihash,
              mode=cfg["mode"], panel_count=len(paths),
              flagged_panels=flagged, panels_dir=str(out_dir))


def main():
    parser = argparse.ArgumentParser(description="Split a stitched strip.png into ordered panel crops.")
    parser.add_argument("--creator_id", required=True)
    parser.add_argument("--series_id", required=True)
    parser.add_argument("--chapter_id", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true", help="Re-split even if valid panels already exist for this strip.")
    parser.add_argument("--mode", choices=["segment", "gap"], default=None, help="Override panel_split.mode.")
    parser.add_argument("--aspect", default=None, help="Override target aspect as W:H, e.g. 9:16 (segment mode).")
    parser.add_argument("--overlap-pct", type=float, default=None)
    parser.add_argument("--search-window-pct", type=float, default=None)
    args = parser.parse_args()

    args.creator_id = sanitize_id(args.creator_id, "creator_id")
    args.series_id = sanitize_id(args.series_id, "series_id")
    args.chapter_id = sanitize_id(args.chapter_id, "chapter_id")

    cfg = load_split_config()
    if args.mode is not None:
        cfg["mode"] = args.mode
    if args.aspect is not None:
        cfg["target_aspect_w"], cfg["target_aspect_h"] = parse_aspect(args.aspect)
    if args.overlap_pct is not None:
        cfg["overlap_pct"] = args.overlap_pct
    if args.search_window_pct is not None:
        cfg["search_window_pct"] = args.search_window_pct

    if args.dry_run:
        plan_dry_run(args, cfg)
        return

    preflight()
    split_chapter(args, cfg)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        with open(ERROR_LOG, "a", encoding="utf-8") as f:
            f.write(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] panel_splitter.py failed\n")
            f.write(traceback.format_exc())
            f.write("\n")
        if CURRENT_UNIT["name"]:
            mark_unit(STATE_PATH, CURRENT_UNIT["name"], "FAILED", error=str(exc))
        print(f"\npanel_splitter.py crashed during '{CURRENT_UNIT['name']}'.")
        print(f"What happened: {exc}")
        print(f"Details were saved to {ERROR_LOG}")
        print("Likely why: an unexpected strip format, a missing strip.png, or an environment problem.")
        print("What to do: read the traceback above, fix it, then resume with the exact same command:")
        print(f"    python {Path(__file__).name} " + " ".join(sys.argv[1:]))
        sys.exit(1)
