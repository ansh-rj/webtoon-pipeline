#!/usr/bin/env python3
"""Stage 2: split a stitched strip.png into ordered panel crops.

OUTPUT TARGET: a LANDSCAPE 1920x1080 video frame. Source art is continuous vertical
webtoon (portrait, ~800x1000 native per source image, a fixed ceiling -- we are NOT
re-capturing or raising device scale). Portrait panels are HEIGHT-FIT into the 1920x1080
frame (top & bottom touch the frame edges), centered horizontally, with a blurred-dim
side fill built later at assembly ("pillarbox"). This stage only decides the cuts and
records display intent in manifest.json; it does NOT build the fill or the video.

My chapters are CONTINUOUS vertical art with no blank gutters, so gap-detection can't
find cut lines. Primary mode is "segment": slice the strip near a target HEIGHT, cutting
at the LEAST-BUSY horizontal row within a search window so cuts avoid faces/bubbles. A
"gap" mode (blank-row splitting) is kept for any future guttered series. Selected via
pipeline_config.json `panel_split.mode` (default "segment").

Usage:
  python panel_splitter.py --creator_id=X --series_id=Y --chapter_id=Z [--dry-run]
                           [--force] [--mode segment|gap] [--target-height 1000]
                           [--overlap-pct 0.05] [--search-window-pct 0.30]
                           [--crop-top-px 0]
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
    # --- video frame the panels will be shown in (LANDSCAPE 1920x1080) ---
    # Portrait panels are HEIGHT-FIT into this frame: scaled so their height reaches frame_height
    # (top & bottom touch the edges), centered horizontally, with a blurred-dim side fill added at
    # assembly. Sharpness therefore binds on HEIGHT, not width.
    "frame_width": 1920,
    "frame_height": 1080,
    # A small enlargement to reach 1080 tall is imperceptible, so allow up to this scale before
    # calling a panel blurry. An 800x1000 source needs 1080/1000 = 1.08x -> SHARP.
    "upscale_tolerance": 1.15,   # height-fit scale <= this is SHARP; above it is WOULD-UPSCALE
    "crop_top_px": 0,            # rows trimmed off the TOP of the strip before splitting (removes a nav/chrome bar)
    "pan_min_display_width_px": 450,  # if height-fitting a panel to frame_height would make it NARROWER than this
                                      # (a tall splash page shrunk to a sliver), mark it for a vertical PAN instead:
                                      # shown at native width, the frame scrolls top->bottom over it. Above this width,
                                      # a static height-fit pillarbox looks fine.
    # --- segment mode ---
    "target_height": 1000,       # aim each panel near this many native rows (matches ~800x1000 source art)
    "overlap_pct": 0.05,         # adjacent panels share this fraction so nothing is lost at a seam
    "search_window_pct": 0.60,   # FALLBACK ONLY: when no blank band exists in the whole legal range,
                                 # cut at the least-busy row within +/- this fraction of target height.
                                 # (Blank-band snapping searches the full [min, max] range regardless.)
    "high_detail_mult": 0.33,    # flag a cut whose row-cost exceeds this multiple of the strip's MEDIAN row-cost;
                                 # chosen cuts are local minima, so comparing to median (not a high quantile) is what catches a face/bubble seam
    "snap_blank_min_px": 12,     # within the search window, SNAP the cut to the midpoint of any blank band
                                 # (per-row std < gap_row_std_max) at least this tall -- a gutter/empty background
                                 # is the safest cut; fall back to least-busy row only when no band is in reach
    "min_segment_height": 940,   # HARD FLOOR: never cut a panel shorter than this (enforced in the cut
                                 # search, not just as a trailing merge). 1080/1.15 = 939, so >=940px native
                                 # guarantees height-fitting to 1080 stays within the upscale tolerance -> SHARP.
    "max_segment_height_mult": 2.6,  # HEIGHT BOUND: never cut a panel taller than this x target_height. Raised
                                     # from 1.6 for gutter-snapping: ch02's art blocks run up to ~2600px between
                                     # blank bands, so a 1600 ceiling FORCED mid-art cuts through faces. A taller
                                     # panel just downscales further (still SHARP); assembly pans it. Blocks
                                     # taller than this still get a least-busy mid-art cut (rare).
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


def manifest_path_for(creator_id, series_id, chapter_id):
    return panels_dir_for(creator_id, series_id, chapter_id) / "manifest.json"


def classify_sharpness(panels, strip_w, cfg):
    """Decide, per panel, how it is shown in the landscape frame without blur.

    Presentation model: a portrait panel is scaled so its HEIGHT reaches frame_height (top &
    bottom touch the frame edges), centered horizontally, with a blurred-dim side fill added at
    assembly. Binding axis is HEIGHT; the scale is frame_h / native_h. Three outcomes:

      fit_scale = frame_h / nh
      - fit_scale > tolerance (panel too SHORT): enlarging to reach 1080 would blur. WOULD-UPSCALE;
        shown at native size, centered (display_scale 1.0). display_mode "native_centered".
      - fit_scale <= tolerance AND the height-fit display width >= pan_min_display_width_px: a normal
        portrait panel. Height-fit to 1080 (a sub-tolerance enlargement is allowed, e.g. 1000->1080
        is 1.08x -> imperceptible). display_mode "height_fit_pillarbox".
      - fit_scale <= tolerance BUT height-fit width < pan_min_display_width_px (a TALL splash page
        that would shrink to a narrow sliver): instead of slivering it, mark it for a vertical PAN --
        shown at NATIVE width (display_scale 1.0), assembly scrolls a frame_h window top->bottom over
        it. display_mode "height_fit_pan"; pan_travel_px = nh - frame_h (how far the window scrolls).

    Invariant enforced: no panel is enlarged past `upscale_tolerance`. WOULD-UPSCALE and PAN panels
    are shown at native (display_scale 1.0); pillarbox panels at most tolerance. Returns per-panel dicts.
    """
    frame_w = int(cfg["frame_width"])
    frame_h = int(cfg["frame_height"])
    tol = float(cfg["upscale_tolerance"])
    pan_min_w = float(cfg["pan_min_display_width_px"])
    out = []
    for n, (y0, y1, seam_flag, cost) in enumerate(panels, start=1):
        nw, nh = strip_w, y1 - y0
        fit_scale = frame_h / nh
        would_upscale = fit_scale > tol  # too short: reaching 1080 would blur
        hf_display_w = nw * fit_scale    # width if we height-fit this panel to 1080
        pan_travel = 0
        if would_upscale:
            display_scale = 1.0          # show at native size, centered -- never blown up
            display_mode = "native_centered"
        elif hf_display_w < pan_min_w:
            display_scale = 1.0          # too tall to height-fit without slivering: native width, vertical pan
            display_mode = "height_fit_pan"
            pan_travel = max(0, nh - frame_h)  # frame_h window scrolls this many native px top->bottom
        else:
            display_scale = fit_scale     # height-fit to the frame (may be a sub-tolerance enlargement)
            display_mode = "height_fit_pillarbox"
        out.append({
            "order_index": n,
            "file": f"panel_{n:03d}.png",
            "y0": int(y0), "y1": int(y1),
            "native_width": int(nw), "native_height": int(nh),
            "would_upscale": bool(would_upscale),
            "display_mode": display_mode,
            "fill_style": "blurred_dim_copy",
            "fit_scale": round(float(fit_scale), 4),
            "display_scale": round(float(display_scale), 4),
            "display_width": int(round(nw * display_scale)),
            "display_height": int(round(nh * display_scale)),
            "pan_travel_px": int(pan_travel),
            "high_detail_seam": bool(seam_flag),
            "cut_cost": round(float(cost), 1),
        })
    return out


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


def plan_segment_cuts(H, W, cfg, row_cost, row_std=None):
    """Return (panels, seg_h, flag_thr). panels = list of (y0, y1, flagged, cost).

    Each panel is ~target_height tall. Cut preference, within +/- search window of the ideal
    boundary: (1) SNAP to the midpoint of the widest blank band (rows with near-zero std --
    a gutter or empty background) if one at least snap_blank_min_px tall is in reach; else
    (2) the least-busy row by gradient cost. Snapping matters for art with white/flat
    between-scene bands: the gradient argmin often lands at the EDGE of a band (still busy
    on one side) while the band's MIDDLE is a guaranteed-clean cut. Adjacent panels overlap
    by overlap_pct; a leftover slice shorter than min_segment_height merges into the previous
    panel so no panel is too short to height-fit the frame cleanly.
    """
    import numpy as np

    seg_h = max(1, int(round(cfg["target_height"])))
    overlap_px = int(round(seg_h * cfg["overlap_pct"]))
    win = int(round(seg_h * cfg["search_window_pct"]))
    max_h = int(round(seg_h * cfg["max_segment_height_mult"]))  # hard ceiling on panel height (anti-pan)
    min_h = int(round(cfg["min_segment_height"]))
    snap_min = int(round(cfg["snap_blank_min_px"]))
    # Flag relative to the strip's MEDIAN row-cost: a cut lands on a local minimum, so a
    # high-quantile reference would never trip. If even the calmest row in range is well
    # above typical art (median), the cut is slicing a face/bubble -- flag it.
    flag_thr = float(np.median(row_cost)) * cfg["high_detail_mult"]
    blank = row_std < cfg["gap_row_std_max"] if row_std is not None else None

    def pick_snap(lo, hi, ideal):
        """Midpoint of the best blank run in [lo, hi], preferring runs NEAR the ideal boundary.

        Score = run height minus a distance penalty, so a decent gutter near the target beats a
        slightly wider one far away (which would make panel heights lurch between extremes).
        Returns None if no run >= snap_min tall is in range.
        """
        if blank is None:
            return None
        best, best_score = None, None
        i = lo
        while i <= hi:
            if blank[i]:
                j = i
                while j <= hi and blank[j]:
                    j += 1
                if (j - i) >= snap_min:
                    mid = (i + j) // 2
                    score = (j - i) - abs(mid - ideal) * 0.15
                    if best_score is None or score > best_score:
                        best, best_score = mid, score
                i = j
            else:
                i += 1
        return best

    panels = []
    start = 0
    while start < H:
        target = start + seg_h
        if target >= H:  # last panel runs to the end, no cut search
            panels.append((start, H, False, 0.0))
            break
        # Blank-band snap searches the FULL legal range [min_h, max_h] -- a clean gutter anywhere
        # a legal panel could end beats a busy row near the target. The min-height floor is
        # enforced here too: the earliest allowed cut is start+min_h, so no panel ever comes out
        # too short to height-fit the frame within the upscale tolerance.
        snap_lo = start + min_h
        snap_hi = min(H - 1, start + max_h)
        if snap_lo >= H:        # not enough strip left for a full-height panel: absorb the tail
            panels.append((start, H, False, 0.0))
            break
        cut = pick_snap(snap_lo, min(snap_hi, H - 1), target) if snap_lo <= snap_hi else None
        if cut is None:
            # No blank band in the whole legal range: least-busy row near the target (window
            # is the fallback's reach, still clamped to the legal min/max bounds).
            lo = max(snap_lo, target - win)
            hi = min(snap_hi, target + win)
            if hi < lo:
                hi = lo  # window collapsed (min_h near max_h): take the floor row
            cut = lo + int(np.argmin(row_cost[lo:hi + 1]))
        cost = float(row_cost[cut])
        panels.append((start, cut, cost > flag_thr, cost))
        nxt = cut - overlap_px
        start = nxt if nxt > start else start + seg_h  # guarantee forward progress

    # Merge a trailing slice shorter than min_segment_height into the previous panel.
    if len(panels) > 1:
        y0, y1, _, _ = panels[-1]
        if (y1 - y0) < min_h:
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


def plan_dry_run(args, cfg):
    strip = strip_path_for(args.creator_id, args.series_id, args.chapter_id)
    print("=== panel_splitter.py --dry-run ===")
    print(f"Strip: {strip}")
    if not strip.exists():
        print("Strip NOT found -- run capture + --stitch for this chapter first.")
        return
    w, h = strip_size(strip)
    crop_top = int(cfg["crop_top_px"])
    eff_h = max(1, h - crop_top)
    print(f"Strip size: {w}x{h}" + (f"  (crop_top_px={crop_top} -> {w}x{eff_h} after nav-bar trim)" if crop_top else ""))
    print(f"Mode: {cfg['mode']}")
    print(f"Output dir: {panels_dir_for(args.creator_id, args.series_id, args.chapter_id)}")
    frame_w, frame_h = int(cfg["frame_width"]), int(cfg["frame_height"])
    tol = float(cfg["upscale_tolerance"])
    if cfg["mode"] == "segment":
        seg_h = max(1, int(round(cfg["target_height"])))
        overlap_px = int(round(seg_h * cfg["overlap_pct"]))
        step = max(1, seg_h - overlap_px)
        est = max(1, -(-eff_h // step))  # ceil
        print(f"Target height ~{seg_h}px, overlap {overlap_px}px, "
              f"search +/-{int(round(seg_h * cfg['search_window_pct']))}px, "
              f"min {int(cfg['min_segment_height'])}px / max {int(round(seg_h * cfg['max_segment_height_mult']))}px")
        print(f"Estimated panels: ~{est}")
        print(f"Estimated disk: ~{est * (w * seg_h * 3) // (1024 * 1024)}MB uncompressed / far less as PNG.")
        # Height-fit sharpness preview against the LANDSCAPE frame.
        fit = frame_h / seg_h
        verdict = ("SHARP (downscale to fit)" if fit <= 1.0
                   else f"SHARP (~{fit:.2f}x, within {tol:.2f}x tolerance)" if fit <= tol
                   else f"WOULD-UPSCALE (~{fit:.2f}x > {tol:.2f}x) -> shown native/centered")
        print(f"Video frame: {frame_w}x{frame_h} landscape, HEIGHT-FIT. A ~{seg_h}px-tall panel -> {verdict}.")
        print("Writes panels/ + panels/manifest.json (per-panel native size + height-fit display intent).")
    else:
        print(f"Gap mode: cut at blank bands (row std < {cfg['gap_row_std_max']}, "
              f">= {cfg['gap_min_px']}px), min panel {cfg['gap_min_panel_px']}px.")
    print("\nNo changes have been made. Re-run without --dry-run to split.")


def detect_chrome_bands(gray, cfg):
    """Heuristic: count near-uniform horizontal bands at the very top/bottom that look like UI chrome.

    A nav/toolbar band reads as many consecutive rows of near-constant colour (very low per-row std)
    right at an edge. We only REPORT what we find (and honour crop_top_px for the top); we do not
    auto-trim the bottom -- that stays a manual config decision. Returns (top_band_px, bottom_band_px).
    """
    import numpy as np

    std = gray.astype(np.float32).std(axis=1)
    flat = std < cfg["gap_row_std_max"]
    H = len(flat)

    top = 0
    while top < H and flat[top]:
        top += 1
    bottom = 0
    while bottom < H and flat[H - 1 - bottom]:
        bottom += 1
    return int(top), int(bottom)


def split_chapter(args, cfg):
    import cv2
    import numpy as np

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
    full_H, W = img.shape[:2]

    # (1) Trim a nav/chrome bar off the TOP before splitting, if configured.
    crop_top = max(0, int(cfg["crop_top_px"]))
    if crop_top >= full_H:
        raise RuntimeError(f"crop_top_px={crop_top} >= strip height {full_H}; nothing left to split.")
    if crop_top:
        img = img[crop_top:]
    H = img.shape[0]
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    # Detect (report only) repeated chrome bands at the edges of the trimmed strip.
    top_band, bottom_band = detect_chrome_bands(gray, cfg)
    print(f"[split] strip {W}x{full_H}"
          + (f", trimmed {crop_top}px off top -> {W}x{H}" if crop_top else "")
          + f", mode={cfg['mode']}")

    if cfg["mode"] == "gap":
        panels, n_gutters = plan_gap_cuts(gray, cfg)
        print(f"[split] gap mode: found {n_gutters} gutter(s) -> {len(panels)} panel(s).")
        if n_gutters == 0:
            print("[split] WARNING: no blank gutters found. This art is likely continuous --")
            print("  set panel_split.mode to \"segment\" in pipeline_config.json for this series.")
        flagged = []
        seg_h = 0
    else:
        log_heartbeat(f"[split] {unit_name} computing row cost")
        row_cost = row_cost_curve(gray)
        row_std = gray.astype("float32").std(axis=1)  # blank-band detector for gutter snapping
        panels, seg_h, flag_thr = plan_segment_cuts(H, W, cfg, row_cost, row_std)
        flagged = [n for n, p in enumerate(panels, 1) if p[2]]
        print(f"[split] segment mode: {len(panels)} panel(s), target height ~{seg_h}px, "
              f"high-detail flag threshold row-cost {flag_thr:.1f}.")

    # (6) Trim amount + chrome-band detection report.
    print(f"[split] top trim applied: {crop_top}px (crop_top_px).")
    if top_band or bottom_band:
        print(f"[split] chrome-band scan: {top_band}px near-uniform at TOP, {bottom_band}px at BOTTOM "
              f"of the (trimmed) strip.")
        if top_band > 0 and crop_top == 0:
            print(f"  If that top band is a nav bar, set panel_split.crop_top_px ~= {top_band} to remove it.")
    else:
        print("[split] chrome-band scan: no repeated near-uniform band at the strip edges.")

    # Per-panel table: cut position + NATIVE pixel size + SHARP / WOULD-UPSCALE + height-fit display.
    frame_w = int(cfg["frame_width"])
    frame_h = int(cfg["frame_height"])
    tol = float(cfg["upscale_tolerance"])
    records = classify_sharpness(panels, W, cfg)
    print(f"\n[split] video frame {frame_w}x{frame_h} landscape; a panel is SHARP if HEIGHT-FITTING it "
          f"to {frame_h}px needs no more than {tol:.2f}x enlargement.")
    MODE_LABEL = {"height_fit_pillarbox": "fit",
                  "height_fit_pan": "PAN", "native_centered": "native"}
    print("\n  panel   y0       y1       native_wxh     sharpness       fit     display(WxH)   show     seam")
    print("  -----   ------   ------   -----------    -------------   -----   ------------   ------   ----")
    for r in records:
        sharp = "WOULD-UPSCALE" if r["would_upscale"] else "SHARP"
        disp = f"{r['display_width']}x{r['display_height']}"
        show = MODE_LABEL.get(r["display_mode"], r["display_mode"])
        print(f"  {r['order_index']:5d}   {r['y0']:6d}   {r['y1']:6d}   "
              f"{r['native_width']:5d}x{r['native_height']:<5d}  {sharp:13s}  {r['fit_scale']:5.2f}x  {disp:12s}  {show:6s}  "
              f"{'HIGH' if r['high_detail_seam'] else ''}")

    paths = write_panels(panels, img, out_dir)

    # Manifest: assembly reads this to height-fit each panel (blurred_dim_copy side fill) or, for
    # WOULD-UPSCALE panels, place them at native size centered -- never enlarged past tolerance.
    upscale = [r["order_index"] for r in records if r["would_upscale"]]
    pan = [r["order_index"] for r in records if r["display_mode"] == "height_fit_pan"]
    pillarbox = [r["order_index"] for r in records if r["display_mode"] == "height_fit_pillarbox"]
    heights = [r["native_height"] for r in records]
    height_stats = {
        "count": len(heights),
        "min": int(min(heights)) if heights else 0,
        "median": int(np.median(heights)) if heights else 0,
        "max": int(max(heights)) if heights else 0,
    }
    manifest = {
        "unit": unit_name, "input_hash": ihash, "mode": cfg["mode"],
        "strip_width": int(W), "strip_height": int(full_H),
        "crop_top_px": crop_top, "effective_height": int(H),
        "chrome_band_top_px": top_band, "chrome_band_bottom_px": bottom_band,
        "frame_target": {"width": frame_w, "height": frame_h},
        "upscale_tolerance": tol,
        "pan_min_display_width_px": int(cfg["pan_min_display_width_px"]),
        "target_height": int(seg_h),
        "panel_count": len(records),
        "would_upscale_count": len(upscale),
        "display_mode_counts": {
            "height_fit_pillarbox": len(pillarbox),
            "height_fit_pan": len(pan),
            "native_centered": len(upscale),
        },
        "height_stats": height_stats,
        "panels": records,
    }
    mpath = manifest_path_for(args.creator_id, args.series_id, args.chapter_id)
    atomic_write_json(mpath, manifest)

    # (6) Height report.
    print(f"\n[split] height report: {height_stats['count']} panel(s); "
          f"min {height_stats['min']}px / median {height_stats['median']}px / max {height_stats['max']}px native.")

    if flagged:
        print(f"[split] {len(flagged)} panel(s) cut through a high-detail region "
              f"(no calm row in range): {flagged}")
        print("  These seams may clip a face/bubble; the overlap keeps the subject whole in the neighbour panel.")
    else:
        print("[split] all cuts landed on calm rows (no high-detail seams flagged).")

    # Invariant: no panel is enlarged past the upscale tolerance (pan & native panels stay at 1.0x).
    over_tol = [r["order_index"] for r in records if r["display_scale"] > tol + 1e-6]
    if over_tol:
        raise RuntimeError(f"internal: panels {over_tol} exceed upscale tolerance {tol} -- classify_sharpness bug")

    # Sharpness verdict. Under the landscape height-fit model an ~800x1000 panel needs ~1.08x to
    # reach 1080 tall -- within tolerance, so SHARP. Three presentation modes:
    #   fit  = height-fit pillarbox (normal portrait panel, blurred side fill)
    #   PAN  = too tall to height-fit without slivering -> shown native width, frame scrolls top->bottom
    #   native = too SHORT to reach 1080 within tolerance -> shown native size, centered (not stretched)
    print(f"[split] sharpness: {len(records) - len(upscale)}/{len(records)} panels SHARP "
          f"(no enlargement past {tol:.2f}x), {len(upscale)} WOULD-UPSCALE.")
    print(f"[split] display modes: {len(pillarbox)} height-fit pillarbox, {len(pan)} vertical PAN "
          f"(tall splash pages), {len(upscale)} native-centered (short panels).")
    if pan:
        print(f"[split] PAN panels (native height would height-fit narrower than "
              f"{int(cfg['pan_min_display_width_px'])}px wide): {pan}")
        print(f"  These are captured WHOLE (clean gutter cuts); assembly scrolls a {frame_h}px window down")
        print(f"  each rather than shrinking it to a sliver. Manifest carries pan_travel_px per panel.")
    if upscale:
        print(f"[split] native-centered panels (shorter than ~{int(round(frame_h/tol))}px native): {upscale}")
        print(f"  Shown at native size, centered (sharp but not touching the frame edges) rather than stretched.")
    print(f"[split] wrote {len(paths)} panel(s) + manifest.json to {out_dir}")

    # (6) Show 5 sample crop paths spread across the chapter (not just the first 5).
    if paths:
        n = len(paths)
        idxs = sorted(set(int(round(i * (n - 1) / 4)) for i in range(5))) if n >= 5 else list(range(n))
        print(f"\n[split] sample crops (spread across the chapter):")
        for i in idxs:
            print(f"    {paths[i]}")

    mark_unit(STATE_PATH, unit_name, "DONE", input_hash=ihash,
              mode=cfg["mode"], panel_count=len(paths),
              flagged_panels=flagged, would_upscale=upscale,
              strip_width=int(W), panels_dir=str(out_dir), manifest=str(mpath))


def main():
    parser = argparse.ArgumentParser(description="Split a stitched strip.png into ordered panel crops.")
    parser.add_argument("--creator_id", required=True)
    parser.add_argument("--series_id", required=True)
    parser.add_argument("--chapter_id", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true", help="Re-split even if valid panels already exist for this strip.")
    parser.add_argument("--mode", choices=["segment", "gap"], default=None, help="Override panel_split.mode.")
    parser.add_argument("--target-height", type=int, default=None, help="Override target panel height in px (segment mode).")
    parser.add_argument("--overlap-pct", type=float, default=None)
    parser.add_argument("--search-window-pct", type=float, default=None)
    parser.add_argument("--crop-top-px", type=int, default=None, help="Override rows trimmed off the top (nav-bar removal).")
    args = parser.parse_args()

    args.creator_id = sanitize_id(args.creator_id, "creator_id")
    args.series_id = sanitize_id(args.series_id, "series_id")
    args.chapter_id = sanitize_id(args.chapter_id, "chapter_id")

    cfg = load_split_config()
    if args.mode is not None:
        cfg["mode"] = args.mode
    if args.target_height is not None:
        cfg["target_height"] = args.target_height
    if args.overlap_pct is not None:
        cfg["overlap_pct"] = args.overlap_pct
    if args.search_window_pct is not None:
        cfg["search_window_pct"] = args.search_window_pct
    if args.crop_top_px is not None:
        cfg["crop_top_px"] = args.crop_top_px

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
