# PROGRESS

Permanent handoff doc between build sessions. Read this first, resume at "Exact next step."

## Current micro-session
MS-01e — webtoon_capture.py: in-script `--stitch` mode (segments → strip.png), verified on REAL data

## Last completed
MS-01d — webtoon_stitch.py: overlap-detect + trim stitching of segments into seamless strip(s)

## State
DONE

## Files touched this session (MS-01e) — real state audited after a mid-run interruption
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
- REDUNDANCY (open decision): webtoon_stitch.py (MS-01d) and webtoon_capture.py --stitch now BOTH
  stitch. Different output paths (stitched/strip_####.png vs strip.png). User asked for the in-capture
  variant explicitly. Keep both, or delete webtoon_stitch.py? Awaiting user call — see open questions.

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
- webtoon_stitch.py stitches chapters/{creator}/{series}/{chapter}/segments/segment_*.png into
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
- webtoon_stitch.py: real-platform verification (stitch actual captured segments end to end) —
  only synthetic exact-crop data has been tested. Real screenshots may have sub-pixel/anti-alias
  differences at seams; the confidence threshold + fallback exist for this but are unproven on
  live captures. User should run capture→stitch on the live staging chapter and eyeball the strip.
- webtoon_capture.py: real-platform capture test (manual login, headless scroll capture) —
  needs to be run by the user locally against the actual staging platform, not this dev machine
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
  browser automation), but extraction.py (next-next session) will need it

## Exact next step
FIRST: user to resolve the webtoon_stitch.py vs webtoon_capture.py --stitch redundancy (open
question below) — decide which stitcher is canonical before extraction.py consumes its output
(the two write to DIFFERENT paths: stitched/strip_####.png vs strip.png, so extraction must know
which to read). Then:
MS-02: build extraction.py (chapter image → text stage) — read
tier/engine from pipeline_config.json's `extraction` block (tier "auto" → resolve via
tier_defaults[global tier]), resolve engine "auto" → tier_defaults[resolved_tier].extraction_engine,
implement the free path (easyocr, tesseract fallback) fully working with zero API keys since
that's what's verified installed; stub the paid path (claude_vision / gemini_vision) behind the
same interface so paid tier is wireable once a key is present in .env. extraction.py should call
doctor.py's checks (or import run_checks from doctor.py) as its own preflight gate rather than
reimplementing dependency checks.

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

## Decisions made prior session (MS-01d)
- Stitching is a SEPARATE stage script (webtoon_stitch.py), not folded into webtoon_capture.py —
  each pipeline stage is its own script with its own state file (jobs/stitch_state.json).
- Stitched output lives in chapters/{creator}/{series}/{chapter}/stitched/ as strip_####.png
  (already covered by the `chapters/*/` gitignore). segments/ is KEPT by default after a
  successful stitch (safer — allows re-stitch without re-capture); pass --cleanup-segments (or
  set stitch.cleanup_segments true) to delete them.
- Matcher searches the FULL plausible overlap range, not a narrow band around nominal overlap_pct.
  This is required to catch the capturer's clamped final segment (arbitrary large overlap).
  drift_margin_pct is therefore now vestigial (left in config for back-compat).
- Very tall canvases split at stitch.max_strip_height (default 20000px) into multiple strips so
  downstream OCR/vision stages get manageable images and no single PNG is unwieldy.

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
