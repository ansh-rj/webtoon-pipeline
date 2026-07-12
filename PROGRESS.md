# PROGRESS

Permanent handoff doc between build sessions. Read this first, resume at "Exact next step."

## Current micro-session
MS-01c — webtoon_capture.py: allow-list guard, auth, scroll, overlapping segment screenshots

## Last completed
MS-01b — doctor.py: diagnostics half, environment verified end to end

## State
DONE

## Files touched this session
- webtoon_capture.py (new)
- pipeline_config.json (added `capture` block: viewport_width 800, viewport_height 1600,
  overlap_pct 0.12, scroll_settle_ms 400, image_load_timeout_ms 15000, max_segments 400 —
  written automatically on first run if missing, same pattern as other stage configs)

## What works (tested this session)
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
- webtoon_capture.py: segment STITCHING into full chapter images (explicitly deferred to next
  session — this session stops at raw overlapping segment screenshots in
  chapters/{creator_id}/{series_id}/{chapter_id}/segments/)
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
MS-01d: build the STITCHING step for webtoon_capture.py's output — merge the overlapping
numbered segments in chapters/{creator_id}/{series_id}/{chapter_id}/segments/ into full
deduplicated chapter image(s), using the known overlap_pct (from pipeline_config.json's
`capture` block) to guide alignment (template matching / pixel-diff refinement for drift, since
lazy-load timing can make actual overlap vary slightly from the nominal 12%). Decide + implement
where stitched output lives (likely chapters/{creator_id}/{series_id}/{chapter_id}/stitched/) and
whether segments/ is kept or cleaned up after a successful stitch. Follow the same --dry-run /
preflight / hash-skip / heartbeat / exception-handler pattern. After stitching is solid, MS-02
(deferred from this session) is: build extraction.py (chapter image → text stage) — read
tier/engine from pipeline_config.json's `extraction` block (tier "auto" → resolve via
tier_defaults[global tier]), resolve engine "auto" → tier_defaults[resolved_tier].extraction_engine,
implement the free path (easyocr, tesseract fallback) fully working with zero API keys since
that's what's verified installed; stub the paid path (claude_vision / gemini_vision) behind the
same interface so paid tier is wireable once a key is present in .env. extraction.py should call
doctor.py's checks (or import run_checks from doctor.py) as its own preflight gate rather than
reimplementing dependency checks.

## Blockers
(none — ffmpeg is now installed and doctor.py confirms a clean pass end to end)

## Decisions made this session
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
