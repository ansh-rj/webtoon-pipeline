#!/usr/bin/env python3
"""Stage 1: capture webtoon chapter pages as overlapping viewport screenshots.
Usage: python webtoon_capture.py --creator_id=X --series_id=Y --chapter_id=Z --url="..." [--dry-run] [--force]

Repointing this script at a different platform requires editing ALLOWED_DOMAINS
in this file directly -- it is intentionally not read from config.
"""
import argparse
import hashlib
import os
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
    # --- stitching (segments -> strip.png) ---
    # Webtoon content is a narrow centred column inside wide uniform (black) side
    # margins; alignment is done on the detected content column. An overlap is only
    # trimmed when it is VERIFIED to duplicate (NCC on textured rows), else segments
    # are concatenated untrimmed so unique content is never deleted.
    "stitch_crop_to_content": True,  # detect the content column, ignore side margins, crop output to it
    "stitch_content_std_frac": 0.15, # a column counts as "content" if its std > this fraction of the peak
    "stitch_overlap_min_ncc": 0.7,   # only trim a seam if its overlap region matches at least this well
    "stitch_min_advance_px": 40,     # smallest plausible per-segment scroll advance
    # Page chrome (top nav bar, footer promos, comments, sidebars) puts TEXTURE in the
    # side margins; comic rows keep the margins plain (any colour). We flag rows with
    # textured margins, then keep the longest stretch whose windowed chrome DENSITY
    # stays low -- robust even when comments margins are only partly textured.
    "stitch_trim_chrome": True,         # trim page chrome (nav/footer/comments) above and below the comic
    "stitch_chrome_margin_std": 10,     # a margin row counts as "textured" (chrome) above this per-row std
    "stitch_chrome_window_px": 400,     # sliding window for chrome-density smoothing
    "stitch_chrome_density_max": 0.10,  # comic = rows whose windowed chrome density stays below this
    "stitch_page_bg_min": 200,          # trailing/leading rows brighter than this are page-bg (share/promo bars) -> trimmed
    # The chapter nav bar ("< #5 >") sits centred on a PLAIN dark band: its margins are
    # uniform, so the texture test above reads it as comic, and it is too dark for the
    # page-bg trim. It is a fixed-height element, so it is cut by configured size,
    # per-series tunable. Applied to the assembled strip BEFORE the auto chrome trim.
    "stitch_crop_top_px": 50,           # fixed chrome off the strip top (this series' nav bar is rows 0..49)
    "stitch_crop_bottom_px": 0,         # same for the strip bottom
    "stitch_chrome_repeat_min_ncc": 0.90,  # flag (never delete) mid-strip repeats of the cropped top band
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


def preflight(checks=None):
    print("Running preflight checks...")
    ctx = {}
    for name in (checks if checks is not None else PREFLIGHT_CHECKS):
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

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        # Browser viewport MUST match viewport_height used for `step`/`max_scroll`
        # below: each screenshot is viewport_height tall, so if the scroll step is
        # derived from a different height we either gap (over-scroll) or duplicate.
        context = browser.new_context(
            storage_state=str(AUTH_STATE_PATH),
            viewport={'width': viewport_width, 'height': viewport_height},
            device_scale_factor=1
        )
        page = context.new_page()
        page.set_viewport_size({"width": viewport_width, "height": viewport_height})

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


def strip_path_for(creator_id, series_id, chapter_id):
    return CHAPTERS_DIR / creator_id / series_id / chapter_id / "strip.png"


def detect_content_column(grays, cap_cfg):
    """Find the horizontal span holding the actual comic.

    Webtoon readers render the strip as a narrow centred column with wide uniform
    (usually black) side margins. Those margins are identical in every segment and
    match at any vertical offset, wrecking alignment. Return (x0, x1) of the widest
    contiguous run of textured columns (per-column std above a fraction of the peak),
    sampled across the chapter. Falls back to the full width if nothing stands out.
    """
    import numpy as np

    sample_idx = list(range(0, len(grays), max(1, len(grays) // 15))) or [0]
    col_std = np.mean([grays[i].astype(np.float32).std(axis=0) for i in sample_idx], axis=0)
    width = len(col_std)
    thr = max(6.0, float(col_std.max()) * cap_cfg["stitch_content_std_frac"])
    active = col_std > thr
    best = (0, width)  # default: full width
    i = 0
    best_len = 0
    while i < width:
        if active[i]:
            j = i
            while j < width and active[j]:
                j += 1
            if j - i > best_len:
                best_len, best = j - i, (i, j)
            i = j
        else:
            i += 1
    x0, x1 = best
    if best_len < width * 0.05:  # nothing meaningful found -> keep full width
        return 0, width
    return x0, x1  # exact textured run; padding would show as margin-colored edge strips


def detect_verified_overlap(prev_col, cur_col, cap_cfg):
    """Find the vertical advance whose overlap region genuinely duplicates.

    prev_col/cur_col are float32 grayscale content columns of equal width & height H.
    For a candidate advance A, prev_col[A:H] must equal cur_col[0:H-A] (the shared,
    re-scrolled region).

    Candidates come from cv2.matchTemplate at 1px resolution: up to three textured
    bands from cur's upper portion are located inside prev (black bands are skipped --
    they match anywhere). Each candidate is then VERIFIED by NCC over the whole
    overlap region, restricted to rows textured in BOTH segments, so repeated motifs
    or gutters can't fake a match.

    Returns (advance, overlap, ncc, verified). If no candidate verifies above
    stitch_overlap_min_ncc there is no trustworthy overlap (a capture gap or an
    ambiguous seam) -> advance=H, overlap=0, verified=False: we CONCATENATE and
    trim nothing, so real content is never deleted.
    """
    import cv2
    import numpy as np

    H = prev_col.shape[0]
    lo = int(cap_cfg["stitch_min_advance_px"])
    a = prev_col[:, ::2]  # subsample columns for speed; alignment is vertical only
    b = cur_col[:, ::2]
    a_std = a.std(axis=1)
    b_std = b.std(axis=1)

    def ncc_at(A):
        """NCC over the implied overlap, on rows textured in BOTH segments.

        Returns (ncc, support_rows) or None. Thin support (a lone text band such
        as "TO BE CONTINUED" inside an otherwise flat overlap) is allowed through
        -- the caller demands a near-perfect NCC from it instead of rejecting it,
        because rejecting meant concat -> that text band got DUPLICATED in the strip.
        """
        ov = H - A
        if ov < 12 or A < lo:
            return None
        ra, rb = a[A:H], b[0:ov]
        m = (a_std[A:H] > 6) & (b_std[0:ov] > 6)
        sup = int(m.sum())
        if sup < 12:
            return None
        x = ra[m].ravel(); y = rb[m].ravel()
        x = x - x.mean(); y = y - y.mean()
        d = float(np.sqrt((x * x).sum() * (y * y).sum()))
        return None if d < 1e-6 else (float((x * y).sum() / d), sup)

    # Collect candidate advances from textured 40px bands: cur's upper bands located
    # within prev (A = match_y - band_row), and symmetrically prev's lower bands
    # located within cur (A = band_row - match_y), which catches small overlaps the
    # first direction can't fit. Every candidate is verified below before any trim.
    band_h = 40
    candidates = set()

    def add_candidates(src, dst, rows, sign):
        for r0 in rows:
            band = src[r0:r0 + band_h]
            if float(band.std(axis=1).mean()) < 8:
                continue
            res = cv2.matchTemplate(dst, band, cv2.TM_CCOEFF_NORMED)[:, 0]
            for y in np.argsort(res)[-8:]:
                A = (int(y) - r0) if sign > 0 else (r0 - int(y))
                if lo <= A <= H - band_h:
                    candidates.add(A)

    add_candidates(b, a, range(0, int(H * 0.6) - band_h, 120), +1)   # cur top bands in prev
    add_candidates(a, b, range(int(H * 0.4), H - band_h, 120), -1)   # prev bottom bands in cur

    best_A, best_c, best_sup = None, -2.0, 0
    for A0 in candidates:
        for A in (A0 - 1, A0, A0 + 1):  # tolerate 1px rounding
            r = ncc_at(A)
            if r is not None and r[0] > best_c:
                best_A, best_c, best_sup = A, r[0], r[1]

    # Thin support (< 40 textured rows, e.g. one line of text in a black gutter)
    # is trustworthy only if it matches near-perfectly; broad support uses the
    # configured threshold. Without the thin-support path, text-only overlaps
    # (chapter cards, "TO BE CONTINUED") failed verification, concatenated, and
    # showed up duplicated in the strip.
    need = cap_cfg["stitch_overlap_min_ncc"] if best_sup >= 40 else max(
        0.9, cap_cfg["stitch_overlap_min_ncc"])
    if best_A is not None and best_c >= need:
        return best_A, H - best_A, round(best_c, 2), True

    # Flat-identity fallback: NCC can't verify a textureless overlap (solid-black
    # gutters between panels), but if the overlap at the nominal capture advance is
    # PIXEL-IDENTICAL, trimming one copy is lossless by definition -- even at a
    # capture gap both flanks are pure background, so nothing visible is removed.
    # Without this, flat seams concatenated and padded the strip with ~130px of
    # duplicate background each (the "uneven black gaps" effect).
    A_nom = H - int(round(H * cap_cfg["overlap_pct"]))
    for A in range(max(lo, A_nom - 2), min(H - 12, A_nom + 3)):
        if float(np.abs(prev_col[A:H] - cur_col[0:H - A]).mean()) < 0.5:
            return A, H - A, 1.0, True

    return H, 0, (0.0 if best_c < -1 else round(best_c, 2)), False


def detect_chrome_rows(grays, advances, cx0, cx1, cap_cfg, strip_gray=None):
    """Locate the comic's vertical extent in strip coordinates; rows outside are page chrome.

    The comic strip fills only the centre content column; its side margins are one
    plain background band (any colour -- black on some series, white on others). Page
    chrome (nav bar, end-of-episode promos, share bar, recommendation carousel, the
    COMMENTS section, sidebars) spreads pixels ACROSS the full page width, so its side
    margins are TEXTURED. That texture -- not any particular colour -- is the tell.

    Two subtleties this handles:
      - COLOUR is not a discriminator: some comics have white side margins, the same
        colour as a comments background. So we key on per-row margin texture (std over
        a floor), not on matching a background colour. (The earlier colour-median
        approach misclassified a white-margined comic as chrome -- chapter 02.)
      - Comments margins are only PARTLY textured (uniform gaps between comment blocks),
        so no single textured run is large. We therefore look at chrome DENSITY in a
        sliding window: comic stretches sit near zero, chrome stretches stay high even
        though individual rows flicker. The comic is the longest run whose windowed
        chrome density stays below stitch_chrome_density_max.

    Finally, if the assembled strip is supplied (strip_gray), a bounded page-background
    trim removes a trailing/leading "share this series" bar or promo that sits centred
    on the white page: its icons live in the content column (not the margins), so the
    margin test can't see it, but it is always a run of bright page-bg rows just outside
    the comic. We walk in from each end over bright rows, capped at one window so real
    bright artwork can never be eaten wholesale.

    Sizes are never assumed -- a 3-screen or 30-screen comments section trims the same
    way. Returns (y0, y1) to keep, or None when there are no margins to read or the
    detection looks untrustworthy.
    """
    import numpy as np

    if cx1 - cx0 >= grays[0].shape[1] - 4:  # no side margins detected -> nothing to read
        return None

    floor = float(cap_cfg.get("stitch_chrome_margin_std", 10))

    def chrome_row(g):  # 1.0 where the side margins carry texture (i.e. page chrome)
        marg = np.hstack([g[:, :cx0], g[:, cx1:]])
        return (marg.std(axis=1) > floor).astype(np.float32)

    parts = [chrome_row(grays[0])]
    for i in range(1, len(grays)):
        parts.append(chrome_row(grays[i])[grays[i].shape[0] - advances[i - 1]:])
    chrome = np.concatenate(parts)
    n = len(chrome)

    win = int(cap_cfg.get("stitch_chrome_window_px", 400))
    thr = float(cap_cfg.get("stitch_chrome_density_max", 0.10))
    density = np.convolve(chrome, np.ones(win) / win, mode="same")
    comic = density < thr

    runs, i = [], 0
    while i < n:
        if comic[i]:
            j = i
            while j < n and comic[j]:
                j += 1
            runs.append([i, j])
            i = j
        else:
            i += 1
    if not runs:
        return None
    y0, y1 = max(runs, key=lambda r: r[1] - r[0])
    if y1 - y0 < n * 0.3:  # comic should dominate the page; if not, don't trust the split
        return None

    # Fine page-bg trim of a centred share/promo bar just outside the comic.
    if strip_gray is not None:
        page = float(cap_cfg.get("stitch_page_bg_min", 200))
        rmean = strip_gray.mean(axis=1)
        lim1 = max(y0, y1 - win)
        while y1 > lim1 and rmean[y1 - 1] > page:
            y1 -= 1
        lim0 = min(y1, y0 + win)
        while y0 < lim0 and rmean[y0] > page:
            y0 += 1

    return y0, y1


def stitch_segments(args, cap_cfg):
    """Merge segments/segment_*.png into one chapter strip.png; print per-seam offsets."""
    import cv2
    import numpy as np

    seg_dir = segments_dir(args.creator_id, args.series_id, args.chapter_id)
    segments = sorted(seg_dir.glob("segment_*.png"))
    unit_name = f"{args.creator_id}/{args.series_id}/{args.chapter_id}"
    CURRENT_UNIT["name"] = unit_name

    if not segments:
        print(f"No segments to stitch in {seg_dir}.")
        print("Likely why: capture has not run for this chapter yet.")
        print(f"What to do: run capture first (drop --stitch):")
        print(f"    python {Path(__file__).name} --creator_id={args.creator_id} "
              f"--series_id={args.series_id} --chapter_id={args.chapter_id} --url=\"...\"")
        sys.exit(1)

    print(f"[stitch] {len(segments)} segments in {seg_dir}")
    imgs = []
    for p in segments:
        im = cv2.imread(str(p), cv2.IMREAD_COLOR)
        if im is None:
            raise RuntimeError(f"Failed to read {p.name} (corrupt or not a PNG).")
        imgs.append(im)

    # Normalise width (crop to the narrowest) so vstack lines up.
    min_w = min(im.shape[1] for im in imgs)
    imgs = [im[:, :min_w] for im in imgs]
    grays = [cv2.cvtColor(im, cv2.COLOR_BGR2GRAY) for im in imgs]

    # Restrict alignment (and, by default, the output) to the comic content column.
    if cap_cfg["stitch_crop_to_content"]:
        cx0, cx1 = detect_content_column(grays, cap_cfg)
        if cx1 - cx0 < min_w:
            print(f"[stitch] content column x={cx0}..{cx1} ({cx1-cx0}px of {min_w}px); "
                  f"side margins ignored for alignment and cropped from output.")
    else:
        cx0, cx1 = 0, min_w
    col_gray = [g[:, cx0:cx1].astype(np.float32) for g in grays]

    # Measure each seam: trim ONLY where the overlap region is verified to duplicate;
    # otherwise concatenate (trim nothing) so we never delete unique content.
    seams = []  # idx, advance, overlap, ncc, verified
    last_heartbeat = time.time()
    for i in range(1, len(imgs)):
        adv, ov, ncc, verified = detect_verified_overlap(col_gray[i - 1], col_gray[i], cap_cfg)
        seams.append({"idx": i, "advance": adv, "overlap": ov, "ncc": ncc, "verified": verified})
        if time.time() - last_heartbeat > 30:
            log_heartbeat(f"[stitch] {unit_name} seam {i}/{len(imgs)-1}")
            last_heartbeat = time.time()

    # Build the strip (content-cropped colour) and print a per-seam table.
    imgs_col = [im[:, cx0:cx1] for im in imgs]
    parts = [imgs_col[0]]
    y_cursor = imgs_col[0].shape[0]
    print("\n  seam  segment  advance  trimmed  ncc    action       strip_y")
    print("  ----  -------  -------  -------  -----  -----------  -------")
    for s in seams:
        i = s["idx"]
        adv = s["advance"]
        action = f"trim {s['overlap']}px" if s["verified"] else "concat(gap?)"
        seam_y = y_cursor
        parts.append(imgs_col[i][imgs_col[i].shape[0] - adv:])
        y_cursor += adv
        print(f"  {i:4d}  {i+1:7d}  {adv:7d}  {s['overlap']:7d}  {s['ncc']:5.2f}  "
              f"{action:11s}  {seam_y:7d}")

    strip = np.vstack(parts)

    # --- chrome removal: ONE combined slice, applied before writing strip.png ---
    # (a) fixed crop: the chapter nav bar ("< #N >") sits on a plain band, so the
    #     margin-texture test below cannot see it; it is a fixed-height element cut
    #     by configured pixel count (per-series tunable).
    # (b) auto trim: margin-texture + density detection of nav/footer/promos/comments.
    # Final keep range = intersection of both.
    H_full = strip.shape[0]
    crop_top = max(0, int(cap_cfg.get("stitch_crop_top_px", 0) or 0))
    crop_bottom = max(0, int(cap_cfg.get("stitch_crop_bottom_px", 0) or 0))
    if crop_top + crop_bottom >= H_full:
        raise RuntimeError(
            f"stitch_crop_top_px ({crop_top}) + stitch_crop_bottom_px ({crop_bottom}) >= "
            f"strip height ({H_full}) — check the capture config; nothing would be left.")

    strip_gray = cv2.cvtColor(strip, cv2.COLOR_BGR2GRAY).astype(np.float32)
    keep0, keep1 = crop_top, H_full - crop_bottom
    auto = None
    if cap_cfg.get("stitch_trim_chrome", True):
        advances = [s["advance"] for s in seams]
        auto = detect_chrome_rows(grays, advances, cx0, cx1, cap_cfg, strip_gray)
        if auto is None:
            print("[stitch] chrome trim (auto): no readable side margins / untrustworthy split -- skipped.")
        else:
            keep0, keep1 = max(keep0, auto[0]), min(keep1, auto[1])
    if keep1 <= keep0:
        raise RuntimeError(
            f"Chrome trim would remove the whole strip (keep range {keep0}..{keep1} of {H_full}) — "
            f"fixed crop and auto trim disagree; check stitch_crop_top_px/bottom_px.")

    if keep0 or keep1 < H_full:
        a0, a1 = (auto or (0, H_full))
        print(f"[stitch] chrome trim: keeping strip rows {keep0}..{keep1} of {H_full}.")
        print(f"  top:    {keep0}px removed "
              f"(fixed nav-bar crop {crop_top}px, auto margin-texture {a0}px — larger wins)")
        print(f"  bottom: {H_full - keep1}px removed "
              f"(auto footer/comments {H_full - a1}px, fixed crop {crop_bottom}px — larger wins)")
        print("  Inspect the top edge of strip.png to confirm no art was lost; tune "
              "stitch_crop_top_px per series if the nav bar height differs.")
        if keep0:
            print(f"[stitch] note: seam strip_y values in the table above shift down by {keep0}px after this trim.")

    # (c) sticky-header check: if the cropped top band repeats mid-strip, the header
    #     scrolled with the page and was captured many times — a top crop can't fix
    #     that, and auto-deleting mid-strip rows on a template match could destroy
    #     art. Report loudly, change nothing.
    if crop_top:
        band = strip_gray[:crop_top]
        body = strip_gray[keep0:keep1]
        if float(band.std()) >= 3 and body.shape[0] >= crop_top * 2:
            res = cv2.matchTemplate(body, band, cv2.TM_CCOEFF_NORMED)[:, 0]
            min_ncc = float(cap_cfg.get("stitch_chrome_repeat_min_ncc", 0.90))
            hits, last = [], -crop_top
            for y in np.where(res >= min_ncc)[0]:
                if y - last >= crop_top:
                    hits.append((int(y), float(res[y])))
                    last = y
            if hits:
                print(f"\n[stitch] WARNING: the nav-bar band repeats {len(hits)} time(s) INSIDE the strip:")
                for y, ncc in hits:
                    print(f"  at strip row y={y} (match {ncc:.3f})")
                print("  The header is STICKY and was captured repeatedly — a one-time top crop cannot")
                print("  fix this, and these bands were NOT removed (deleting mid-strip rows is unsafe).")
                print("  Fix the capture (hide the sticky header before screenshotting) and re-capture;")
                print("  tell me if you want webtoon_capture.py to hide it via CSS automatically.\n")
            else:
                print(f"[stitch] sticky-header check: nav-bar band does not repeat inside the strip (good).")

    strip = strip[keep0:keep1]

    out_path = strip_path_for(args.creator_id, args.series_id, args.chapter_id)

    ok, buf = cv2.imencode(".png", strip)
    if not ok:
        raise RuntimeError("cv2 failed to PNG-encode the stitched strip.")
    tmp = out_path.parent / f".{out_path.name}.tmp"
    with open(tmp, "wb") as f:
        f.write(buf.tobytes())
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, out_path)

    n_trim = sum(1 for s in seams if s["verified"])
    n_gap = len(seams) - n_trim
    trimmed_px = sum(s["overlap"] for s in seams if s["verified"])
    print(f"\n[stitch] {n_trim}/{len(seams)} seams had verified overlap (trimmed {trimmed_px}px total); "
          f"{n_gap} seams concatenated with no trim (no reliable overlap).")
    if n_gap > len(seams) // 3:
        print("[stitch] WARNING: many seams lack real overlap. The segments were likely captured")
        print("  with too large a scroll step (over-scroll), so content is MISSING between them.")
        print("  This is a capture problem, not a stitch one — no stitcher can recover unseen pixels.")
        print("  Fix: re-capture this chapter (the scroll step is now corrected in webtoon_capture.py):")
        print(f"    python {Path(__file__).name} --creator_id={args.creator_id} "
              f"--series_id={args.series_id} --chapter_id={args.chapter_id} --url=\"...\" --force")
    print(f"[stitch] strip.png is {strip.shape[1]}x{strip.shape[0]} at {out_path}")
    print("Raw segments kept in segments/ — inspect strip.png, then re-run with --cleanup-segments to remove them.")

    mark_unit(STATE_PATH, unit_name, "STITCHED",
              strip_height=int(strip.shape[0]), seam_count=len(seams),
              trimmed_seams=n_trim, gap_seams=n_gap, trimmed_px=int(trimmed_px),
              strip_path=str(out_path))


def main():
    parser = argparse.ArgumentParser(description="Capture a webtoon chapter as overlapping viewport screenshots.")
    parser.add_argument("--creator_id", required=True)
    parser.add_argument("--series_id", required=True)
    parser.add_argument("--chapter_id", required=True)
    parser.add_argument("--url", default=None, help="Chapter URL (required for capture; not needed with --stitch).")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true", help="Re-capture even if valid segments already exist for this URL.")
    parser.add_argument("--stitch", action="store_true", help="Stitch existing segments/ into strip.png (no capture, no URL needed).")
    parser.add_argument("--cleanup-segments", action="store_true", help="Delete segments/ once strip.png exists. Alone: just deletes. With --stitch: stitches first, then deletes.")
    parser.add_argument("--viewport-width", type=int, default=None)
    parser.add_argument("--viewport-height", type=int, default=None)
    parser.add_argument("--overlap-pct", type=float, default=None)
    args = parser.parse_args()

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

    # --- stitch mode: operate on already-captured segments, no browser/URL/domain guard ---
    if args.stitch:
        if args.dry_run:
            seg_dir = segments_dir(args.creator_id, args.series_id, args.chapter_id)
            n = len(sorted(seg_dir.glob("segment_*.png")))
            print("=== webtoon_capture.py --stitch --dry-run ===")
            print(f"Segments dir: {seg_dir}")
            print(f"Segments found: {n}")
            print(f"Output: {strip_path_for(args.creator_id, args.series_id, args.chapter_id)}")
            print(f"Alignment: verified-overlap NCC on the detected content column "
                  f"(trim only when overlap matches >= {cap_cfg['stitch_overlap_min_ncc']}, else concatenate).")
            print(f"Crop output to content column: {cap_cfg['stitch_crop_to_content']}")
            print(f"Trim page chrome (nav/footer/comments, any size): {cap_cfg.get('stitch_trim_chrome', True)}")
            print(f"Fixed chrome crop: {cap_cfg.get('stitch_crop_top_px', 0)}px top, "
                  f"{cap_cfg.get('stitch_crop_bottom_px', 0)}px bottom (nav bar; per-series tunable)")
            print("No changes made. Re-run without --dry-run to stitch.")
            return
        preflight(["python_version", "venv", "dependencies", "folders", "disk_space"])
        stitch_segments(args, cap_cfg)
        if args.cleanup_segments:
            seg_dir = segments_dir(args.creator_id, args.series_id, args.chapter_id)
            removed = 0
            for p in sorted(seg_dir.glob("segment_*.png")):
                p.unlink()
                removed += 1
            print(f"[stitch] cleaned up {removed} segment(s) from {seg_dir}")
        return

    # --- standalone cleanup mode: delete segments/, but only if strip.png already exists ---
    if args.cleanup_segments:
        seg_dir = segments_dir(args.creator_id, args.series_id, args.chapter_id)
        strip_path = strip_path_for(args.creator_id, args.series_id, args.chapter_id)
        segs = sorted(seg_dir.glob("segment_*.png"))
        if args.dry_run:
            print("=== webtoon_capture.py --cleanup-segments --dry-run ===")
            print(f"Segments dir: {seg_dir}")
            print(f"Segments found: {len(segs)}")
            print(f"strip.png exists: {strip_path.exists()}")
            print("No changes made. Re-run without --dry-run to delete segments.")
            return
        if not strip_path.exists():
            print(f"Refusing to delete segments: {strip_path} does not exist yet. "
                  f"Run with --stitch first so the chapter is safely stitched into strip.png.")
            sys.exit(1)
        for p in segs:
            p.unlink()
        print(f"[cleanup] deleted {len(segs)} segment(s) from {seg_dir}")
        return

    # --- capture mode: URL required ---
    if not args.url:
        print("Missing --url. Capture needs a chapter URL (or pass --stitch to stitch existing segments).")
        sys.exit(1)
    enforce_domain_guard(args.url)

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
