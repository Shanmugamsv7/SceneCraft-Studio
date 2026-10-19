# SceneCraft Studio â€” Stage 5B assessment evidence report

Report date: 2026-10-18. This report describes the current workspace and the checks actually run for Stage 5B. It is not a claim that the project meets an unavailable assessor rubric or that assessment is complete.

## Follow-up: configurable real-generation adapters

The current implementation adds opt-in Pexels video retrieval and ElevenLabs speech generation while preserving the offline mock defaults and existing job/editor/artifact routes. Script scenes are sent separately to the selected speech provider; measured generated audio segment durations are used for contiguous frame-aligned scene and SRT timing. Pexels candidates retain the source-page URL, contributor attribution, provider license note, and third-party-rights warning. The browser reports the active mode without exposing credentials, displays candidate thumbnails/source links, previews the returned MP4, and loads synchronized caption cues from the SRT.

Verification for this follow-up: `python -m pytest -q --basetemp=.pytest-tmp` passed **61 tests** with one existing Starlette/httpx deprecation warning. That run predates the final Pexels URL allowlist change, which now accepts Pexels video-files URLs and the official API-documented Vimeo `/external/` MP4 form. The added URL allowlist tests could not be rerun in this session: `.venv` points to a removed Python 3.12 path and neither `python` nor `py` is available on PATH. `node --check app/static/app.js` was rerun and passed. Adapter tests use mocked HTTP responses and test-only generated media. `node --check app/static/app.js`, OpenAPI 3.1 YAML validation, and a Node frontend smoke test for submit/poll/MP4 preview/downloads/caption-track behavior passed. No live Pexels or ElevenLabs request with valid credentials has been made, so real downloaded footage, natural provider speech, live account access, and an actual two-provider end-to-end export remain **unverified**. No provider credentials or `.env` file were present. Docker CLI was unavailable; Compose startup was not attempted, though the YAML/environment mapping is covered by tests. Default demo mode remains mock graphics plus a test tone. Refer to `REAL-GENERATION-PLAN.md` for the acceptance steps still requiring provider credentials.

## Architecture and decisions

The app is a single-process FastAPI service with a static HTML/CSS/JavaScript editing UI. Requests create in-memory background jobs; a bounded worker pool validates inputs, splits narration into scenes, calls the configured mock or external providers, and uses FFmpeg/ffprobe to render and check MP4, SRT, timeline, and manifest files. Artifacts are stored in per-job directories under `data/jobs/`. The optional S3/MinIO service was not added to keep local use lightweight. Provider selection defaults to offline mock mode.

This design keeps the default usable without API credentials and keeps generated media clearly identified. Pexels search and ElevenLabs speech adapters are available when configured, but their live operation has not been verified in this workspace. The app still does not provide persistent queues, durable job state, authentication, automatic upload retention, independent semantic relevance scoring, or speech transcription.

## Implemented and checked

- Script and audio job creation, background status/progress, structured errors, health/readiness, idempotency, correlation IDs, and scene edit/rerender routes are implemented in `app/main.py` and documented by `openapi.yaml`.
- The editing page is served from `app/static/`. Scene narration/caption edits and visual selection are saved before a full render. Rerender rebuilds the entire video.
- Scene timing uses contiguous frame ranges. The timeline, captions, manifest, and final container are checked before a job is marked successful.
- Mock color cards and the 440 Hz tone are labelled as generated mock output. Uploaded audio is not transcribed. The adapters use external APIs only when explicitly configured; no live external API, paid service, or large model was used during verification.
- The full workspace test suite passed **53 tests** on the final run for this report, with one Starlette/httpx deprecation warning. See `test-pack/stage5b-test-results.txt` for the captured output.
- The local readiness endpoint returned `ready` with FFmpeg, ffprobe, storage, and provider checks true.
- The final ten-case host batch completed **10/10 jobs**. Elapsed times, measured by the runner from submission through polling and artifact download/validation, ranged from **1.125 s to 3.750 s**, with a **1.849 s mean**. These ten sequential runs are observations, not a benchmark.
- All **40** requested artifacts were downloaded into `test-pack/stage5b-final-artifacts/<job-id>/`. FFprobe checks on all ten MP4s verified H.264, AAC, 30 fps, and expected dimensions: 1920Ã—1080, 1080Ã—1920, or 1080Ã—1080. All ten video durations matched timeline and manifest durations; audio/video stream duration differed by less than 100 ms. All ten caption files had scene-count and timestamp agreement with the timeline.
- The ten jobs rendered 21 scenes. No selected visual ID repeated within a job (**0 repeated selections among 21 scenes**); these are generated cards, not footage clips. Provenance fields were present, score values remained null, and manifests correctly marked generated mock media. Checks are recorded per job in `test-pack/stage5b-final-export-validation.json`.
- The existing human-edited 1:1 sample bundle was revalidated read-only: the MP4, SRT, timeline, and manifest exist; manifest job ID matches; edited caption text/timing agrees; and video is H.264/AAC, 1080x1080 at 30 fps, 6.000 seconds. Record: `test-pack/stage5a-edited-output/stage5b-recheck.json`. The prior sample files were not overwritten.
- `test-pack/human-relevance-ratings.csv` has 21 rows with blank numeric scores and notes. No person has reviewed the scenes for relevance.
- Earlier Stage 4 and Stage 5A records document successful container build/readiness and Docker batch checks (`test-pack/stage4-verification.md`, `PLAN.md`, and `test-pack/stage5a-docker-batch-results.json`). Docker was not rebuilt in this Stage 5B pass because the app/container configuration was not changed; this report does not claim a fresh Stage 5B Docker build.
- A high-confidence secret-pattern scan checked 86 project text files without printing values; no key/token/private-key patterns were found. `.env.example` is the only root `.env*` file. The scan excluded `.venv`, pytest temporary files, and `data/` because runtime job data may contain user uploads. The workspace has no `.git` metadata, so tracked/untracked file status cannot be determined; see the checklist.

The Stage 5B test pack includes 10 varied positive scripts and the existing pytest negative cases for empty/conflicting inputs, unsupported/oversized/malformed/silent/long audio, missing tools, invalid aspect ratios, provider timeout/rate-limit/unavailable/empty/invalid results, render interruption, missing artifacts, concurrent jobs, and artifact/job isolation. The exact tests reside in `tests/test_app.py`.

## Source and AI-use disclosure

The Python dependency versions and reported licenses observed in the active environment, generated media source, FFmpeg build, and Pexels candidate research are listed in `SOURCES.md`. The app can call Pexels and ElevenLabs only when explicitly configured, but neither was called during this verification. OpenAI Codex was used as coding assistance; see `AI_USE.md`. No paid provider was used, so cost was not measured and is recorded as unavailable rather than estimated.

## Known limitations and remaining work

- **Real footage and rights:** The Pexels adapter is implemented but has not been live-tested with a credential. No actual Pexels result, downloaded clip, or item-specific rights audit is evidenced in this workspace. Pexels' license does not remove the need to review third-party rights.
- **Semantic relevance:** Pexels results are retrieved using each scene's narration phrase and returned in the provider's own ranking; this is not an independent semantic score. Mock visuals do not match scene meaning. Human ratings are pending; do not report an average or high-relevance percentage.
- **Narration/audio:** Natural TTS is available only through the configured ElevenLabs adapter, but it has not been live-tested here. Default test-tone narration is not speech. Speech transcription is absent. Uploaded audio is used without transcription.
- **Editing:** Mock mode offers generated cards; Pexels mode can offer provider-sourced candidate clips. Every saved edit triggers a full-video rerender; partial rendering and scene-cache reuse are not implemented.
- **Browser inspection:** The available browser automation inventory returned no browser or app surfaces. Human visual/interaction review is pending; exact manual instructions are in `README.md` and `test-pack/final-submission-checklist.md`.
- **Reliability/security:** Job/edit/idempotency state is in memory. Uploaded source audio stays on disk without automatic expiration. The local service has no authentication and should not be exposed to an untrusted network.
- **Measurements:** CPU/memory consumption and cost were not measured. The black/frozen-frame test checks a representative render; all ten final exports were checked for stream and timing correctness but were not all frame-by-frame visually inspected. Automated checks do not establish aesthetic quality or subject relevance.
- **Assessment material:** No separate official rubric, assessment brief, or submission form was found in the workspace. Check the requirements matrix against the actual assessor material before making submission claims.
- **Repository state:** This directory has no `.git` metadata; the Git tracked/untracked status cannot be audited from this copy. A name/pattern scan for likely secrets must be completed and recorded without printing secret values.

## Reproduction

Follow setup and test commands in `README.md`. The ten-case run requires a running local API and uses `test-pack/evaluation-inputs.json`; the result record is `test-pack/stage5b-final-evaluation-results.json`. All run artifacts and export validation are under `test-pack/stage5b-final-artifacts/` and `test-pack/stage5b-final-export-validation.json`. This evaluation has no paid-service requirement.

