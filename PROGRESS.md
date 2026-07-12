# PROGRESS

Permanent handoff doc between build sessions. Read this first, resume at "Exact next step."

## Current micro-session
MS-01b — doctor.py: diagnostics half, environment verified end to end

## Last completed
MS-01 — Environment tooling: PROGRESS.md, git repo, setup.py installer

## State
DONE

## Files touched this session
- doctor.py (new)
- .claude/settings.local.json (permission allowlist additions, unrelated housekeeping)

## What works (tested)
- `python doctor.py --dry-run` prints the 15-check plan with per-check time estimates, makes no changes.
- `python doctor.py` (zero API keys) on this machine: 10 PASS / 1 WARN (tesseract absent, expected —
  free tier uses easyocr as primary) / 1 FAIL (ffmpeg absent, pre-existing documented blocker) /
  3 SKIP (no keys configured, correctly silent rather than failing). Exit code 1 on the ffmpeg FAIL,
  confirming other scripts can gate on doctor's exit code.
  Checks: python_version, venv, dependencies (subprocess import probe of all 12 packages into the
  venv interpreter), ffmpeg (found + runs + version parsed), tesseract (optional, WARN not FAIL),
  playwright_browser (actually launches+closes headless chromium in a subprocess), folders,
  pipeline_config (valid JSON + expected fields), machine_profile, env_file, disk_space
  (WARN <10GB / FAIL <2GB), network (socket connect to 1.1.1.1:443 / 8.8.8.8:53), and one cheap
  live-validation check per key (anthropic_key/gemini_key/elevenlabs_key) — each SKIPs silently if
  no key is set, and SKIPs even with a key set if no configured engine in pipeline_config.json
  actually needs that provider.
  `python doctor.py --json` prints machine-readable output (validated: parses as JSON, 15 entries).
- Failure-path proven without needing to break anything destructively: ffmpeg was already absent
  on this dev machine, so the `ffmpeg` check's FAIL path is exercised for real on every run (not simulated).
  Message reads "ffmpeg not found on PATH" with fix "Install it: winget install ffmpeg" — plain
  language, exact command, matches the FFMPEG_INSTALL_HINT setup.py already uses.
  Attempted `choco install ffmpeg` to also prove the PASS path; failed for an unrelated reason
  (chocolatey needs an elevated/admin shell to write C:\ProgramData\chocolatey\lib-bad — this dev
  shell isn't elevated). No files were left partially written by the failed choco run. PASS path
  for ffmpeg (found + `-version` runs + output parsed) is implemented and code-reviewed but not yet
  exercised live on this machine — first real pipeline run (or an elevated `winget install ffmpeg`)
  will confirm it.
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
- extraction stage
- tts stage
- script_generation stage
- compilation stage
- digest stage
- orchestrator / main pipeline entrypoint
- everything downstream of environment setup
- ffmpeg is NOT installed on this dev machine (setup.py correctly detected this and printed
  `winget install ffmpeg` rather than failing setup; user must run that manually before any
  stage that shells out to ffmpeg)
- no API keys are in .env yet (blank placeholders only) — fine for free tier, required before
  any "paid" tier stage (script_provider claude / longform+compilation gemini / elevenlabs tts)
  can run

## Exact next step
MS-02: build extraction.py (chapter image → text stage). Use state_manager.py's
atomic_write_json/load_json/mark_unit for all state, read tier/engine from
pipeline_config.json's `extraction` block (tier "auto" → resolve via tier_defaults[global tier]),
resolve engine "auto" → tier_defaults[resolved_tier].extraction_engine, implement the free path
(easyocr, tesseract fallback) fully working with zero API keys since that's what's verified
installed; stub the paid path (claude_vision / gemini_vision) behind the same interface so paid
tier is wireable once a key is present in .env. Follow the same --dry-run / preflight / hash-skip
/ heartbeat / exception-handler pattern as setup.py and doctor.py. extraction.py should call
doctor.py's checks (or import run_checks from doctor.py) as its own preflight gate rather than
reimplementing dependency checks.

## Blockers
- ffmpeg still not installed on this dev machine. `winget` is unavailable in this shell
  (git-bash doesn't expose it); `choco install ffmpeg` was attempted and failed because this
  shell is not elevated/admin (chocolatey couldn't write C:\ProgramData\chocolatey\lib-bad).
  Needs either: an elevated PowerShell/cmd running `winget install ffmpeg` or `choco install
  ffmpeg -y`, or a manual static-binary install added to PATH. doctor.py's ffmpeg check will
  go from FAIL to PASS automatically once this is done — no code changes needed.
  extraction/OCR stages don't need ffmpeg, so MS-02 is unblocked.

## Open questions
- `pipeline_config.json`'s top-level `"mode"` field had no specified value in the setup spec;
  defaulted to `"full"` as a placeholder. Confirm this is the right default (vs. e.g. `"chapters"`
  or null) before any stage starts reading it.
- `voices.narrator/male/female` are placeholder friendly names ("Default Narrator", etc.), not
  real engine voice IDs. MS-?? needs a friendly-name → engine-specific-voice-ID mapping table
  (edge_tts / elevenlabs / openai_tts / piper each have different ID formats) before tts stage
  can use them.
- `winget install ffmpeg` is the Windows hint setup.py prints; unconfirmed whether the target
  deployment machines actually have winget available (older Windows without App Installer would
  not). May need a fallback hint or a bundled static ffmpeg binary option later.
- No stage code exists yet to actually resolve `tier: "auto"` → global tier → engine "auto" →
  `tier_defaults[resolved_tier].<engine>`. That resolution logic is currently only described in
  prose (this file + the standing rules), not implemented anywhere. MS-02 should probably build
  this resolver as a small shared helper (e.g. in state_manager.py or a new config.py) rather
  than reimplementing it per-stage.
