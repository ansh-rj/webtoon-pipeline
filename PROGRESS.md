# PROGRESS

Permanent handoff doc between build sessions. Read this first, resume at "Exact next step."

## Current micro-session
MS-04 — panel_splitter.py (strip.png -> ordered panel crops). NEW stage between stitch and
extraction. My chapters are CONTINUOUS vertical art (no blank gutters), so gap-splitting can't
find cuts. Built segment mode as primary + gap mode kept for future guttered series (config
`panel_split.mode`, default "segment").
- segment mode: slice to a target aspect (default 9:16 tall), ~5% overlap so nothing is lost at a
  seam, and place each cut on the LEAST-BUSY row (min mean-gradient / row-cost) within a search
  window of the ideal boundary -> avoids slicing a face/bubble without needing a blank row.
- Pure-CV stage like capture: NO tier/engine field. Config block `panel_split` (auto-added,
  idempotent). Atomic panel writes, hash-skip on unchanged strip, --dry-run + preflight
  (doctor.CHECK_FUNCS), heartbeat, global exception handler -- all per house style.
- VISUAL VERIFICATION drove two fixes on the test chapter (this is the important part):
  first run's window (15%) + flag reference (75th pct) were BOTH wrong -- sampled cuts showed one
  slicing through a character's EYES (panel 8) and one through a head/hair (panel 26), NEITHER
  flagged. Row-cost analysis proved calm (cost 0.0, solid-colour) rows existed nearby but outside
  the 15% window (p08 needed ~40%). Fixes: search_window_pct 0.15->0.40; flag vs MEDIAN row-cost
  (high_detail_mult 0.33) not a high quantile, since a chosen cut is always a local minimum.
  Re-ran: 67 panels, 0 flagged, both former bad cuts now land on solid-black rows BELOW the
  subject (face/head kept whole -- re-inspected the exact rows). 5 sample crops eyeballed clean.
- Files: panel_splitter.py (new); pipeline_config.json (+ panel_split block, tuned values).

## Last completed
MS-04 (panel_splitter.py) — pending commit. Prior: MS-01j (committed 1177255).

## State
DONE — ch01 strip 700x57096, ch02 strip 700x77406; both end exactly at the comic (title card /
"TO BE CONTINUED"), no comments/promos/share bar. 67/67 and 87/87 seams verified. ch01's raw
segments have been cleaned up (strip.png kept); ch02 segments still on disk. Working tree clean
except untracked scratch_pad/ (scratch, not committed).

## MS-01i summary (this session)
User captured a SECOND chapter (02, 88 segments) and reported comments still present in its strip.
The MS-01h chrome trim worked on ch01 but failed on ch02. Two distinct failures, both fixed:

1. COLOUR ASSUMPTION (the real bug): MS-01h's detect_chrome_rows keyed on the MEDIAN margin
   COLOUR (bg) and marked rows whose margins matched it as comic. ch01's comic margins are BLACK
   and its footer is WHITE, so that worked by luck. ch02's comic margins are WHITE -- the SAME
   colour as the comments background -- so bg=255 matched everything and nothing trimmed. Colour
   is not a discriminator. FIX: key on margin TEXTURE instead. Comic margins are a plain band
   (any colour); page chrome (nav/promos/share/carousel/COMMENTS/sidebars) spreads pixels full
   width, so its margins are textured (per-row std > stitch_chrome_margin_std=10).
2. PARTLY-TEXTURED COMMENTS: comment margins are only ~30-45% textured rows (uniform gaps between
   comment blocks), so no single textured run is large -- a per-row+bridge approach missed it.
   FIX: windowed chrome DENSITY (convolve over stitch_chrome_window_px=400; comic = density <
   stitch_chrome_density_max=0.10). Comic stretches sit near 0, chrome stays high despite flicker.
3. CENTRED SHARE BAR (residual): after the density cut ch02 still kept a ~170px "share this series"
   bar -- its icons sit in the CONTENT COLUMN, not the margins, so margin texture can't see it.
   FIX: a bounded page-bg trim (detect_chrome_rows now takes the assembled strip_gray) walks in
   from each end over bright page-bg rows (mean > stitch_page_bg_min=200), capped at one window so
   real bright artwork can't be eaten. ch02 77578 -> 77406; ch01 unaffected (its tail is black).

Verified this session (both real chapters, through the actual --stitch path):
- ch01: 700x57096, ends on the "Tower of God" title card; header 0px, footer 6740px trimmed.
- ch02: 700x77406, ends exactly at "TO BE CONTINUED" (final eye panel intact); footer 5726px
  trimmed. Comments, recommendation carousel, share bar all gone. Visually inspected both tails.
- 67/67 and 87/87 seams verified (0 concat) on both.

## Files touched this session (MS-01i)
- webtoon_capture.py: detect_chrome_rows rewritten (texture + windowed density, colour-agnostic;
  new strip_gray param for bounded page-bg trim); DEFAULT_CAPTURE_CONFIG chrome knobs replaced
  (removed stitch_chrome_gap_px; added stitch_chrome_margin_std/window_px/density_max/page_bg_min);
  stitch_segments passes strip_gray to detect_chrome_rows.
- pipeline_config.json: same chrome knob set updated.

## MS-01j summary (this session)
Usability fix. `--cleanup-segments` was only handled INSIDE the `--stitch` branch, so running it
alone fell through to capture mode and died with "Missing --url". Added a standalone cleanup
branch BEFORE the capture URL check: deletes segments/ for the chapter, but only if strip.png
already exists (otherwise refuses with a plain message, exit 1, so raw capture is never lost with
no stitched output). --dry-run supported. Tested all three paths; cleaned up ch01's 76 segments
for real. Committed 1177255.
- webtoon_capture.py: standalone --cleanup-segments branch added; help text updated.

## MS-01h summary (this session)
User re-captured the chapter (68 segments at the fixed 950px step) and asked to audit the strip:
"white padding" and a trailing comments section, sizes varying per chapter. Found and fixed three
real issues; the "white padding" was NOT an issue:

1. WHITE BANDS ARE CONTENT: the two >=100px near-white runs (~27% down the strip) are the
   chapter title card ("Tower of God" on white) — legitimate art, left alone. Verified visually.

2. CHROME TRIM (new): detect_chrome_rows() removes page chrome above/below the comic — nav bar,
   end promos, recommendation carousel, COMMENTS — by reading the side margins: comic rows keep
   margins uniform background; chrome rows put pixels there. Longest background run = comic;
   texture blips < stitch_chrome_gap_px (150) are bridged. Size-agnostic by construction, so a
   3-screen or 30-screen comments section trims identically across chapters. On this chapter:
   trimmed 50px header + 6558px footer. Config: stitch_trim_chrome true, stitch_chrome_gap_px 150.
   detect_content_column() also lost its 10px pad (was leaving margin-colored edge strips).

3. DUPLICATED "TO BE CONTINUED" (real stitch bug, fixed): the TBC text appeared TWICE in the
   strip. Root cause: ncc_at() required >=40 textured rows of support; a lone text line in a
   black gutter gave ~10 rows -> candidate rejected -> concat -> duplicate. Fix: thin support
   (<40 rows) is now accepted but must match at NCC >= max(0.9, threshold). Verified: TBC now
   appears exactly once.

4. FLAT-IDENTITY FALLBACK (fixed the remaining 7 concat seams): NCC is undefined on textureless
   overlaps (solid-black gutters between panels), so those seams concatenated and padded the
   strip with ~130px duplicate background each. New fallback: if the overlap at the nominal
   advance (H*(1-overlap_pct), ±2px) is pixel-identical, trim it — lossless by definition.
   Result: 67/67 seams verified (was 59/67), strip 700x57228 (was 700x58930 with dup padding).

Verified this session (all on the re-captured real chapter):
- TBC text: exactly 1 occurrence (template match >0.9 across full strip).
- Comments/footer content: absent from strip (best match conf 0.32).
- Random mid-chapter content bands: present exactly once each.
- Strip head/tail: comic art, no chrome, no padding rows.
- --stitch --dry-run prints the chrome-trim line; full run clean.

## Files touched this session (MS-01h)
- webtoon_capture.py: detect_chrome_rows() (new); stitch_segments calls it after vstack;
  ncc_at() returns (ncc, support) + thin-support threshold; flat-identity fallback in
  detect_verified_overlap(); detect_content_column pad removed; --stitch dry-run line added.
- pipeline_config.json: + stitch_trim_chrome true, stitch_chrome_gap_px 150.

## MS-01g summary (this session) — READ THIS BEFORE TRUSTING OLDER STITCH NOTES BELOW
User reported the real-chapter strip was "very uneven" and then that "panels have hidden too much
info" (content being trimmed away). Root-caused in two layers, both fixed:

1. CAPTURE BUG (the true root cause): the 46 real segments were captured when config said
   viewport_height=1600 but the browser was HARD-CODED to 1920x1080. Scroll step was computed
   from config (1600*0.88=1408px) while each screenshot only showed 1080px -> the page advanced
   1408px per shot, screenshots covered 1080px -> ~328px of comic MISSING between most segments,
   and lazy-load jitter made actual advances irregular (measured: only ~9-11 of 45 seams have any
   real overlap; ~22-35 are hard gaps). Content lost at capture time is UNRECOVERABLE by stitching.
   FIX (code): capture_chapter now passes config viewport_width/height into the Playwright context
   (single source of truth — step math and screenshot height can never diverge again). Config is
   1920x1080, so step=950 < 1080 -> guaranteed 130px overlap on every future capture.
   NOT YET RUN: needs a live re-capture by the user (browser+auth). Until then the current strip
   necessarily has gaps where the capture skipped content.

2. STITCHER: went through three algorithms this arc, in response to what real data showed:
   a) full-width NCC (MS-01e): WRONG on real data — the comic is a ~719px content column centred
      in 34% uniform black side margins (detected x=601..1320 of 1920); margins match at ANY
      offset, so overlaps came out 163-1080px garbage and the strip looked chopped/uneven.
   b) ORB feature voting on the content column: seams LOOKED continuous but a residual check
      (NCC of the supposedly-duplicate overlap region at the chosen advance) showed 33/45 seams
      trimming NON-matching (unique) content — this was the "panels hide info" complaint. ORB was
      voting on repeated art motifs; brute-force best-NCC proved most seams have NO good alignment
      (gaps), so ORB's confident-looking answers were unfalsifiable garbage.
   c) FINAL (in code now): detect_verified_overlap() — candidates from cv2.matchTemplate of
      textured 40px bands (BIDIRECTIONAL: cur-top bands searched in prev AND prev-bottom bands
      searched in cur, so small overlaps are reachable; black bands skipped), each candidate then
      VERIFIED by NCC over the entire implied overlap region on rows textured in BOTH segments.
      Trim ONLY if verified NCC >= stitch_overlap_min_ncc (0.7); otherwise CONCATENATE UNTRIMMED
      ("concat(gap?)" in the table) so unique content is never deleted. Lossless by construction.

Verified this session:
- Synthetic 4-segment set with known geometry (130px real overlaps at seams 1&3, a deliberate
  328px capture gap at seam 2): trims exactly 130px/130px at NCC 1.00, concatenates the gap seam
  (NCC 0.01). Exact expected behavior.
- Real chapter: 10/45 seams verified & trimmed (2516px of true duplicates, e.g. seam 45 footer
  784px @ NCC 1.00, seam 12 chapter-title card 40px @ NCC 1.00 — visually confirmed clean);
  35/45 concatenated untrimmed. strip.png now 719x47164 (content-cropped). A WARNING block prints
  when >1/3 of seams lack overlap, explaining the over-scroll capture problem and giving the exact
  re-capture command.
- Per-seam table now prints advance/trimmed/ncc/action/strip_y per seam.
- Config `capture` block final stitch knobs: stitch_crop_to_content true, stitch_content_std_frac
  0.15, stitch_overlap_min_ncc 0.7, stitch_min_advance_px 40 (ORB and slice/jitter knobs removed).

## Files touched this session (MS-01g)
- webtoon_capture.py: capture viewport unified with config; detect_content_column();
  detect_verified_overlap() (replaces detect_seam_overlap/ORB); stitch_segments rewritten
  (content-crop, verify-then-trim, gap warning); --stitch dry-run text updated.
- pipeline_config.json: stitch knob set updated to match (see above).

## Files touched in MS-01e (superseded detail, kept for history)
- webtoon_capture.py (MODIFIED, complete + compiles + runs) — added `--stitch` mode: stitches
  existing segments/ into a single chapters/{ids}/strip.png (NOT the separate stitched/strip_####
  layout of webtoon_stitch.py — this is the per-user-spec variant). Also added `--cleanup-segments`
  and made `--url` optional (only required for capture, not stitch). New funcs: strip_path_for,
  detect_seam_overlap, stitch_segments. preflight() now takes an optional check list (stitch skips
  network/playwright). NOTE: the interrupted session left BOTH files fully written — audited this
  session: valid JSON, compiles, end-to-end re-run reproduces identical strip. No repair needed.
- pipeline_config.json (MODIFIED, valid) — `capture` block gained 4 stitch knobs:
  stitch_slice_height 200, stitch_match_threshold 0.5, stitch_h_jitter 20, stitch_outlier_tol 0.25.
- NOT committed yet by the interrupted run; committing this session.
- REDUNDANCY RESOLVED (MS-01f): user chose webtoon_capture.py --stitch as canonical.
  webtoon_stitch.py DELETED (git rm). Its orphaned top-level `stitch` block removed from
  pipeline_config.json. jobs/stitch_state.json never existed on disk. The `capture` block's
  stitch_* knobs are the live tunables. Canonical stitched output = chapters/{ids}/strip.png.
- Also fixed stale config: capture.viewport_width/height set to 1920x1080 to match what the
  capturer actually produces (it hard-codes the browser to 1920x1080). No more drift-correction
  note on stitch; nominal overlap now 129px straight from config.

## What works (tested this session — MS-01e, REAL captured chapter test/my_series/01, 46 segments)
- 46 real 1920x1080 segments stitched into strip.png (1920x26707). Ran clean, reproducible.
- CONFIG-DRIFT SELF-CORRECT: real segments are 1920x1080 but config viewport_height was 1600
  (capturer hard-codes the browser to 1920x1080, overriding config). Nominal overlap is now derived
  from the ACTUAL median segment height (1080*0.12=129px), not config, and prints a note when they
  differ. This was a real bug found on live data — the config-derived nominal (192) was wrong.
- Uniform-region ambiguity handled TWO ways: (a) match confidence < threshold → nominal fallback,
  (b) low-variance template (std<3, a solid gutter) → nominal fallback (NCC goes degenerate on flat
  regions and reports spurious high scores; caught on a synthetic pure-white overlap that otherwise
  swallowed a whole segment). Verified both.
- Per-seam table prints overlap_px, conf, h_shift, source, and strip_y (pixel position of each seam
  in strip.png) so the user knows exactly where to scroll; FALLBACK/OUTLIER/hshift flagged. Outliers
  flagged vs median overlap (stitch_outlier_tol).
- VISUALLY INSPECTED the real strip myself (cropped ±120px around the riskiest seams and Read the
  images): seam 1 (dark art, continuous), seam 18 & 28 (CONTAINED dup-flagged; speech-bubble text
  flows across un-duplicated/un-clipped), seams 42-44 (the 3 FALLBACK seams) all land in the page
  FOOTER/UI chrome (Trending & Popular, creator info — near-white, mean~250), not comic content.
  No duplicated or missing slivers at any worst-case seam.
- 3 fallback + 35/45 outlier seams: NOT stitch failures — the live capture had irregular scroll
  distances (match conf mostly >0.85, two segments fully CONTAINED/duplicate). The stitcher adapts
  per-seam; only the 3 fallbacks are guesses and they sit in harmless footer whitespace.
- Raw segments KEPT (per spec) for the user's own visual confirm; --cleanup-segments removes them.

## What works (tested prior session — MS-01d, synthetic data on this dev machine)
## NOTE: webtoon_stitch.py was DELETED in MS-01f. This section is retained as history of the
## algorithm's validation; the same NCC/fallback approach lives on in webtoon_capture.py --stitch.
- webtoon_stitch.py stitched chapters/{creator}/{series}/{chapter}/segments/segment_*.png into
  deduplicated chapters/.../stitched/strip_####.png. Alignment is per-pair template matching
  (cv2.matchTemplate TM_CCOEFF_NORMED): a `template_height`-row band from the top of segment N is
  located in segment N-1; overlap = prev_h - match_row; segment N contributes only rows below the
  overlap. Confidence < threshold falls back to nominal overlap_pct.

## What works (tested this session — MS-01d, synthetic data on this dev machine)
- webtoon_stitch.py stitches chapters/{creator}/{series}/{chapter}/segments/segment_*.png into
  deduplicated chapters/.../stitched/strip_####.png. Alignment is per-pair template matching
  (cv2.matchTemplate TM_CCOEFF_NORMED): a `template_height`-row band from the top of segment N is
  located in segment N-1; overlap = prev_h - match_row; segment N contributes only rows below the
  overlap. Confidence < threshold falls back to nominal overlap_pct.
- PROVEN pixel-exact: built a 800x5000 content-rich source, sliced it into 4 overlapping viewport
  segments with per-segment drift (+7/-5/... rows simulating lazy-load timing), stitched, and the
  result was byte-for-byte identical to the source (delta 0px, no divergent row). Overlaps found:
  197, 181 (matching injected drift, conf 1.00).
- Clamped final-segment case handled: capture.py clamps the last scroll to max_scroll, giving an
  arbitrary/large final overlap. A first cut used a narrow drift band around nominal and MISSED it
  (fallback → 830px of duplicated content). Fixed by searching the full plausible overlap range
  [ph - min(ph,ch), ph]; the 1022px final overlap is now detected at conf 1.00. This was a real
  bug caught by the reconstruction check, not a hypothetical.
- Multi-strip split verified: with max_strip_height=2000 the 5000px canvas wrote strip_0001..0003
  (2000+2000+1000). Stale strips from a previous run are deleted before writing, so a shorter
  re-stitch never leaves orphans (verified 3 strips → 1 on re-run).
- hash-skip verified: 2nd run (no --force) prints "Already stitched ... skipping"; input_hash is
  sha256 of each segment's name+size so a re-capture invalidates it. --force re-stitches.
- --cleanup-segments verified: deletes segments/ after a successful stitch (default keeps them;
  decision below). --dry-run with no segments prints the plan and exits 0 without touching disk.
- Strip writes are atomic: cv2.imencode('.png') in memory → temp file + os.replace (cv2.imwrite
  can't target a .tmp extension, so encode-then-write is used instead).
- Preflight reuses doctor.CHECK_FUNCS (python_version, venv, dependencies, folders, disk_space —
  no network/playwright needed for stitching). Exception handler + heartbeat follow capture.py.

## What works (tested prior session — MS-01c)
- Domain guard is hard-coded (`ALLOWED_DOMAINS = ["staging.local"]` at module top, not read from
  pipeline_config.json, per explicit requirement). Tested for real: `python webtoon_capture.py
  --creator_id=testc --series_id=tests --chapter_id=ch1 --url="https://google.com"` refused
  cleanly with a plain-language message and exit code 1, before any browser/network activity.
- `--dry-run` on an allow-listed staging.local URL prints the full plan (output dir, auth state
  status, viewport/overlap/timeout settings, segment/time/disk estimates) and makes no changes —
  confirmed no browser launched, no files written, exit code 0. Also confirmed this run
  auto-added the missing `capture` block to pipeline_config.json (idempotent, same pattern as
  machine_profile.json auto-creation in setup.py).
- NOT tested this session (by design — dummy staging.local domain doesn't resolve on this dev
  machine): manual-login flow (headful launch, storage_state save to auth_state.json), the
  actual headless capture loop (scroll-and-screenshot with overlap, lazy-image wait, state
  hash-skip on unchanged URL, max_segments safety cap), and the exception-handler path. The user
  will run the real capture test against the live staging platform locally and report back.

## What works (tested, prior sessions)
- `python doctor.py --dry-run` prints the 15-check plan with per-check time estimates, makes no changes.
- ffmpeg is now installed on this dev machine (via chocolatey, `ffmpeg version 8.1.2-essentials_build`,
  resolved at `C:\ProgramData\chocolatey\bin\ffmpeg.exe`). Confirmed on PATH and runnable.
- `python doctor.py` (zero API keys) on this machine, TRUE clean run with ffmpeg present:
  11 PASS / 1 WARN (tesseract absent, expected — free tier uses easyocr as primary) / 0 FAIL /
  3 SKIP (no keys configured, correctly silent rather than failing). Exit code 0.
  This is the first time the full check suite has actually passed clean end to end — previously
  ffmpeg being absent meant only a 10/1/1/3 result was ever observed.
  Checks: python_version, venv, dependencies (subprocess import probe of all 12 packages into the
  venv interpreter), ffmpeg (found + runs + version parsed), tesseract (optional, WARN not FAIL),
  playwright_browser (actually launches+closes headless chromium in a subprocess), folders,
  pipeline_config (valid JSON + expected fields), machine_profile, env_file, disk_space
  (WARN <10GB / FAIL <2GB), network (socket connect to 1.1.1.1:443 / 8.8.8.8:53), and one cheap
  live-validation check per key (anthropic_key/gemini_key/elevenlabs_key) — each SKIPs silently if
  no key is set, and SKIPs even with a key set if no configured engine in pipeline_config.json
  actually needs that provider.
  `python doctor.py --json` prints machine-readable output (validated: parses as JSON, 15 entries).
- Failure-path proven for real, non-destructively: prepended a temp directory containing a fake
  `ffmpeg.bat` (exits 127, prints to stderr) onto PATH for a single doctor.py invocation only
  (via `PATH="$FAKE_DIR:$PATH" venv/Scripts/python.exe doctor.py`; real ffmpeg install untouched).
  Windows' PATHEXT/`shutil.which` resolution meant a plain extensionless fake file was skipped in
  favor of the real ffmpeg.exe on first attempt — using `.bat` (a recognized PATHEXT extension)
  correctly shadowed it. Result: ffmpeg check FAILed with "ffmpeg at
  C:\...\tmp.xxx\ffmpeg.BAT exited with an error", fix line "Reinstall it: winget install ffmpeg",
  overall 10 passed/1 failure, exit code 1 — confirming other scripts can gate on doctor's exit code.
  Temp directory deleted immediately after. Re-ran doctor.py afterward with PATH restored: back to
  11 PASS/0 FAIL/exit 0, confirming the real ffmpeg install was never touched and the fix was
  transient and fully reversible.
- doctor.py writes jobs/doctor_state.json via mark_unit per check (verified valid JSON, 15 units)
  and appends heartbeat.log start/finish lines, following the exact pattern setup.py established.
- `python setup.py --dry-run` prints the unit plan with time/disk estimates, makes no changes.
- `python setup.py --non-interactive` on this machine (Windows, Python 3.14.3, 6 cores, 15.3GB RAM):
  creates venv/, writes+installs requirements.txt (playwright, opencv-python, pillow, easyocr,
  pytesseract, edge-tts, ffmpeg-python, faster-whisper, anthropic, google-genai, requests, psutil —
  all confirmed importable via `venv/Scripts/python.exe -m pip freeze`), runs
  `playwright install chromium`, detects missing ffmpeg and prints the correct Windows hint
  (`winget install ffmpeg`), creates all 6 folders, writes pipeline_config.json from the template,
  writes machine_profile.json (parallel_workers derived: 3 = cores/2 since RAM > 8GB), writes
  a blank-but-present .env with chmod 600 attempted.
- Re-running is idempotent: 2nd and 3rd runs skip venv/requirements/pipeline_config/.env with
  clear "skipped: reason" messages; only re-probes ffmpeg/folders/machine_profile (cheap, safe
  to redo) and playwright (playwright's own installer is a fast no-op if already present).
- Exception handler path verified for real: an actual bug (unit_machine_profile imported psutil
  in the *outer* interpreter running setup.py instead of the venv's interpreter) triggered it —
  state was marked FAILED for that unit, traceback appended to logs/errors.log, and the printed
  message gave the plain-language cause + exact resume command (`python setup.py`). Fixed by
  shelling out to venv_python() for the psutil probe instead of importing psutil directly.
- heartbeat.log confirmed appending every ~30s during the multi-minute pip install of
  easyocr/torch (~9 min on this machine's connection).

## What is NOT done
- Chrome trim now validated on TWO chapters (black-margin comic + white-margin comic). Still
  watch the "[stitch] chrome trim:" line on future chapters. Known limits of the current design:
  (a) a comic that draws edge-to-edge with NO side margins -> detect_chrome_rows returns None,
      full strip kept (safe). (b) the page-bg trim only catches BRIGHT (white) trailing bars;
      a dark-themed share bar just past a dark comic ending would be kept -- revisit if seen.
- extraction stage (chapter image → text)
- tts stage
- script_generation stage
- compilation stage
- digest stage
- orchestrator / main pipeline entrypoint
- everything downstream of environment setup
- no API keys are in .env yet (blank placeholders only) — fine for free tier, required before
  any "paid" tier stage (script_provider claude / longform+compilation gemini / elevenlabs tts)
  can run
- the tier "auto" → global tier → engine "auto" → tier_defaults resolver described in the open
  questions below is still unbuilt; not needed yet since capture has no tier/engine (it's pure
  browser automation), but extraction.py (MS-03, next session) will need it

## Exact next step
MS-04 (panel_splitter.py) is complete and (about to be) committed. NEXT is MS-03: build
extraction.py -- now operates on the PANEL CROPS from panel_splitter
(chapters/{ids}/panels/panel_###.png), not the whole strip. Read tier/engine from pipeline_config.json's `extraction` block (tier "auto" → resolve via
tier_defaults[global tier]), resolve engine "auto" → tier_defaults[resolved_tier].extraction_engine,
implement the free path (easyocr, tesseract fallback) fully working with zero API keys since
that's what's verified installed; stub the paid path (claude_vision / gemini_vision) behind the
same interface so paid tier is wireable once a key is present in .env. extraction.py should call
doctor.py's checks (or import run_checks from doctor.py) as its own preflight gate rather than
reimplementing dependency checks.
(Numbering note: "MS-02" only ever existed as a forward-reference in commit ff0f043; the setup and
capture/stitch work all shipped under MS-01a..MS-01j. Extraction is MS-03.)

## Blockers
(none — ffmpeg is now installed and doctor.py confirms a clean pass end to end)

## Decisions made this session (MS-01e)
- Per explicit user spec, stitching was ALSO built into webtoon_capture.py as `--stitch` mode,
  writing a single strip.png at chapters/{ids}/strip.png (no multi-strip split). This coexists
  with (duplicates) webtoon_stitch.py from MS-01d. Redundancy flagged for user to resolve.
- Nominal overlap derived from ACTUAL median segment height, not config viewport_height, so the
  fallback is correct even when the capturer overrode the configured viewport (which it does).
- Uniform/flat overlap regions get a variance guard (template std<3 → nominal fallback) on top of
  the confidence threshold, because NCC reports spurious high scores on textureless gutters.

## Decisions made prior session (MS-01d) — SUPERSEDED by MS-01f
- webtoon_stitch.py was a separate stage script writing stitched/strip_####.png with multi-strip
  splitting at max_strip_height. DELETED in MS-01f (user chose the in-capture --stitch variant).
  Kept here only as history: the multi-strip-split idea (cap very tall canvases so downstream
  OCR/vision gets manageable images) was NOT carried into --stitch, which writes one strip.png.
  If a chapter's strip ever proves too tall for a downstream stage, re-introduce splitting there.

## Decisions made prior session (MS-01c)
- `.gitignore` gained `auth_state.json` (holds live session cookies via Playwright storage_state
  — must never be committed) and `chapters/*/` (captured/stitched chapter images are large binary
  pipeline output, not source).

## Open questions
- `pipeline_config.json`'s top-level `"mode"` field had no specified value in the setup spec;
  defaulted to `"full"` as a placeholder. Confirm this is the right default (vs. e.g. `"chapters"`
  or null) before any stage starts reading it.
- `voices.narrator/male/female` are placeholder friendly names ("Default Narrator", etc.), not
  real engine voice IDs. MS-?? needs a friendly-name → engine-specific-voice-ID mapping table
  (edge_tts / elevenlabs / openai_tts / piper each have different ID formats) before tts stage
  can use them.
- `winget install ffmpeg` is the Windows hint setup.py/doctor.py print; CONFIRMED winget is not
  available in this dev machine's git-bash shell (`winget: command not found`). ffmpeg was
  actually installed here via `choco install ffmpeg -y` instead (chocolatey was present).
  setup.py/doctor.py's hint text is still winget-only — should add a chocolatey fallback hint,
  or detect which package manager is actually available, before this is portable to other
  Windows dev machines.
- No stage code exists yet to actually resolve `tier: "auto"` → global tier → engine "auto" →
  `tier_defaults[resolved_tier].<engine>`. That resolution logic is currently only described in
  prose (this file + the standing rules), not implemented anywhere. MS-02 should probably build
  this resolver as a small shared helper (e.g. in state_manager.py or a new config.py) rather
  than reimplementing it per-stage.
