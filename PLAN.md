# SceneCraft Studio — Implementation Plan

This plan records the intended design and verification approach. A requirement is considered complete only after its implementation has been run and checked. Work will proceed stage by stage; later stages may adjust the design when testing exposes a better choice.

## Architecture and data flow

The application will use a small FastAPI service, a static HTML/CSS/JavaScript page, and FFmpeg for media inspection and rendering. The browser submits either script text or one audio file to the API. The API validates the request, creates a queued job, and returns its ID immediately. A background worker processes the job, updates its state and progress, and saves artifacts under a job-specific output directory. The browser polls the job endpoint and displays progress and links when rendering finishes.

For script jobs, a provider interface will isolate speech generation from the rest of the pipeline. The default development provider will be explicitly labelled mock narration; it must not be described as genuine TTS. Audio jobs will use FFmpeg/ffprobe to validate supported formats, duration, and audible content. A deterministic selector will split narration into sentence or phrase scenes and select eligible footage without repeating clips. The renderer will normalize scene clips, synchronize the narration, burn or package captions as appropriate, and emit the video plus timeline and provenance metadata.

No verified sample footage is bundled. The default provider generates locally-created color cards and labels them as mock graphics, never stock footage. The renderer exposes a provider contract for future video assets and rejects file-backed footage unless the file exists and its licence, attribution, and source URL are supplied as verified. No source will be scraped and no paid provider, external deployment, or public upload is planned.

## Proposed folder structure

```text
.
├── PLAN.md
├── README.md
├── SOURCES.md
├── AI_USE.md
├── REPORT.md
├── openapi.yaml
├── .env.example
├── .gitignore
├── pyproject.toml
├── Dockerfile
├── compose.yaml
├── app/
│   ├── main.py              # FastAPI routes and startup
│   ├── config.py            # Environment-backed settings
│   ├── models.py            # Typed request/job/error models
│   ├── jobs.py              # Job state and background orchestration
│   ├── providers/            # TTS/media provider interfaces and mock
│   ├── pipeline/             # Validation, segmentation, selection, render
│   └── static/               # HTML, CSS, browser JavaScript
├── assets/                   # Small documented licensed sample footage
├── data/                     # Local job inputs and outputs (ignored by Git)
├── tests/                    # Unit, API integration, and end-to-end tests
└── test-pack/                # Test input/output/failure/log index and evidence
```

The exact module boundaries may be simplified during Stage 1 to keep the initial version easy to follow. Runtime job data and uploaded files will not be committed.

## Dependencies and licences

Planned runtime dependencies are Python 3.12, FastAPI, Uvicorn, Pydantic (through FastAPI), and a small multipart form parser such as `python-multipart`. Tests will use pytest and FastAPI's test client. FFmpeg and ffprobe will be system executables in local development and the container; their build/package licensing will be documented. The browser interface will use plain HTML, CSS, and JavaScript without a frontend framework.

Optional MinIO/S3 integration will be considered in Stage 2 only if local storage plus Docker Compose can remain simple. Any storage client dependency and MinIO licence will be recorded if adopted. Every bundled footage/audio sample and any model/provider/API used will be listed in `SOURCES.md`; the exact licence and attribution will be checked before the asset enters default rendering. The AI coding tool actually used will be named in `AI_USE.md`.

## Stages and verification

### Stage 0 — Inspect and plan

- Inspect tracked/untracked workspace files and repository instructions before creating files.
- Record architecture, proposed folders, dependencies/licences, staged work, assessment checks, risks, and open decisions in this document.
- Verification: review the plan against the requested requirements and confirm the repository state was checked. No application feature is claimed at this stage.

### Stage 1 — Small working application

- Build the browser form and status display, job submission/status endpoints, request validation, useful logs, background job execution, mock provider, deterministic scene planning, and local artifact delivery.
- Add FFmpeg/ffprobe checks and render a playable MP4 from a compact eligible sample library. Provide SRT captions, complete scene timeline, and manifest.
- Validate script/audio exclusivity, empty scripts, supported audio extensions and content, corrupt media, silence, and the ten-minute duration limit.
- Verification: pytest for core validation and selection; API integration tests for script/audio submission, 202 response, status transitions, errors, and result links; an end-to-end run that inspects the actual MP4 and all required artifacts with ffprobe and JSON/SRT parsing. Record any environment-dependent checks.

### Stage 2 — Assessment integration

- Add the OpenAPI 3.1 file, API version consistency, idempotency, health/readiness checks, environment configuration, standard structured error responses, and JSON logs carrying timestamp, level, job ID, and correlation ID.
- Add Docker Compose and decide whether MinIO is useful for this lightweight implementation. Add a batch test runner that records actual results.
- Verification: compare served API behavior with `openapi.yaml`; test idempotent retries, health/readiness, errors, configuration, and log fields; run the documented batch command and Compose health checks.

### Stage 3 — Video correctness and provenance

- Support 16:9, 9:16, and 1:1 at 30 fps with appropriate 1080p-class dimensions, H.264 video, AAC audio, normalized clips, narration synchronization, and no repeated clip in one output.
- Record per-scene narration timing/text, chosen asset/source, verified licence and attribution, AI-generated status, selection reason/score, and warnings/fallbacks. Record providers, versions, settings, seed, timings, cost estimate status, and rights in the manifest.
- The current mock provider is a flat-color fallback, not genuine footage; semantic relevance is not scored and is recorded as unavailable. Uploaded audio is not transcribed; the timeline and captions state that plainly.
- Verification: render each ratio and inspect codec, dimensions, frame rate, audio stream/duration, full timeline coverage, clip uniqueness, and provenance fields. Exercise render failures and ensure missing or unverified file-backed assets are rejected.
- Verification record (2026-10-18): the complete suite passed (25 passed; one upstream Starlette/httpx deprecation warning). FFprobe checks confirmed H.264/AAC, 30 fps, and 1920x1080, 1080x1920, and 1080x1080 outputs. The local mock batch recorded 2/2 successful jobs. No genuine licensed footage was available or used.

### Stage 4 — Reliability and tests

- Expand tests for script/audio jobs, ratios, invalid/silent/long media, abstract or sensitive narration, ambiguous terms and named entities, missing footage, provider failures, corrupt downloads, concurrency, idempotency, and complete successful artifacts.
- Keep tests offline and independent of credentials. Document a one-command test run. Provider calls now have a configurable timeout and a two-attempt maximum; only failures marked retryable are retried.
- A job is marked succeeded only after its MP4, captions, timeline, and manifest are present, parseable, and mutually consistent. Uploaded audio and temporary render intermediates are removed at job completion; partial output artifacts are removed after failure.
- Verification: full pytest suite, live batch runner, and static checks of Dockerfile/Compose. Automated MP4 checks include streams, resolution, frame rate, durations, black-frame detection, and distinct sampled frame hashes; human review is still required for visual quality and relevance.
- Verification record (2026-10-18): workspace suite passed 43 tests (one upstream Starlette/httpx deprecation warning); the README dependency setup was installed in a fresh temporary Python 3.12 environment and all 43 tests passed there as well. The local batch recorded 2/2 successful jobs: landscape 1.719 s, portrait 1.046 s. The Docker image built successfully without cache; Compose readiness reported FFmpeg, ffprobe, storage, and provider checks all true, and the container remained healthy after a 2/2 batch. Container jobs were verified at H.264/AAC 30 fps with exact file/timeline durations. Docker image size is 829,131,300 bytes (about 791 MiB), which remains a size cost of the Debian FFmpeg package. Docker was found only through its absolute Docker Desktop path in the elevated shell; it was not visible on the regular shell PATH.

### Stage 5 — Documentation and evidence

- Finish `README.md`, `SOURCES.md`, `AI_USE.md`, `REPORT.md`, `.env.example`, `.gitignore`, OpenAPI, and the test-pack index.
- Report measured turnaround, relevance ratings, timeline coverage, rendering success, repeat-clip rate, resource use, and cost only from recorded runs. Mark unavailable measurements explicitly; do not estimate them as facts.
- Verification: follow the README from a clean local setup, inspect every referenced artifact/source, and ensure no secrets or generated private job data are tracked.

### Stage 5A — Human-guided editing workspace

- Replace the one-screen submission page with a responsive studio containing script/audio input, aspect-ratio controls, progress and errors, rendered preview, scene storyboard, export panel, and a visual/provenance key.
- Return scene details in the job status response and expose single-scene read/edit endpoints. Scene changes are staged explicitly, validated against that job's scene and visual IDs, and preserved in the browser during status refreshes.
- Add accept/reject/replace decisions and fresh mock visual options. These are generated color cards only, have no semantic match score, and are not presented as licensed footage. Keep `FootageProvider.select_visual` compatible and add optional `visual_options` support.
- Add a full-job render endpoint. Validate all scene decisions before queueing; render in a private temporary directory, verify every required artifact, and then replace the job's prior files. Do not claim partial rerender or scene-cache reuse.
- Keep uploaded audio privately inside the job directory to enable rerenders; allow artifact downloads only for named output files. Document that local file retention currently has no automatic expiry/deletion and that process memory still holds job/edit state.
- Support changing output ratio on the initial render or a later full rerender. Update static OpenAPI 3.1 and document the mock-only provider configuration.
- Verification: add API checks for scene/narration/caption edits, unknown scene/visual IDs, reject/accept rules, fresh alternatives, latest-edit SRT/timeline/manifest/video downloads, uploaded-audio rerender, full-rerender failures, and the OpenAPI route contract. Run the complete suite, local batch, and Compose smoke test when the required tools are available. Inspect the interactive page in a browser where available; otherwise mark visual review pending.
- Verification record (2026-10-18): complete workspace suite passed (51 passed; one upstream Starlette/httpx deprecation warning). The browser JavaScript passed Node syntax checking. Host batch completed 2/2 jobs; Compose rebuilt, reached `/ready`, and its final batch completed 2/2 jobs. A live HTTP edit/rerender downloaded MP4/SRT/timeline/manifest and checked the latest human edits and selected option in the returned artifacts (`test-pack/stage5a-edited-output/`). The browser tool reported no available browser surfaces, so visual browser review remains outstanding.
- Remaining risks: only the generated-color offline provider exists; no semantic relevance or real licensed footage. Scene and job state remains in memory. Uploaded audio is retained in local job folders for editing, without automatic cleanup. Rerendering always rebuilds the entire video; no scene cache/partial render is implemented. The local HTTP app has no authentication.

### Stage 5B — Final assessment audit and evidence

- Audit the available plan, implementation, OpenAPI contract, tests, and prior test-pack records. Search the workspace for a separate official brief/form; if absent, state that the supplied conversation brief is the only available assessment source and do not infer missing rubric items.
- Record each requirement's status and evidence in `test-pack/requirements-matrix.md`. Complete source and AI-use disclosures, report, reproducible README, evaluation cases, blank human-rating template, demo script, and submission checklist.
- Review a possible documented footage API without credentials or downloads. Keep the application on its offline mock provider unless a verified adapter and its source/license requirements can be tested. Do not claim semantic matching, real relevance scores, natural narration, transcription, or partial-scene rendering.
- Run ten varied local jobs through the batch runner, download artifacts, record job IDs/status/timings/output paths, and validate MP4/SRT/timeline/manifest against actual FFprobe and parser output. Keep human relevance ratings blank pending review.
- Verification record (2026-10-18): local readiness succeeded; the final ten varied mock jobs completed 10/10 with downloads under `test-pack/stage5b-final-artifacts/`. All ten FFprobe and metadata records passed in `test-pack/stage5b-final-export-validation.json`, covering all three ratios, H.264/AAC, 30 fps, duration/caption/provenance consistency, and no repeated selected IDs among 21 mock scenes. Full suite: 53 passed, one upstream deprecation warning; exact captured output is `test-pack/stage5b-test-results.txt`. Pexels official API/license pages were reviewed as a possible future source; it is not implemented or used. Browser inventory had no surfaces; no separate official assessment brief/form or `.git` metadata exists in this workspace. A no-value-printing secret-pattern scan found no likely secrets among 86 text files; runtime `data/` was excluded because it may hold user uploads. Docker build was not repeated in Stage 5B; prior Stage 4/5A Compose records remain available.
- Assessment status: **not complete**. Human browser/relevance review, a verified real-footage source, an official assessment brief/form, and Git repository metadata remain unavailable or unimplemented.

### Stage 6 — Configurable real footage and narration adapters

- Preserve the current routes, schemas, job lifecycle, editor, full rerender behavior, artifacts, Docker setup, and mock mode. Add `GET /api/v1/providers` to disclose active provider mode without returning secrets.
- Add an opt-in Pexels videos API adapter and an opt-in ElevenLabs text-to-speech adapter, configured only through server environment variables. Missing credentials for an explicitly selected provider must stop startup with a specific setting name rather than silently falling back to mock output.
- Use Pexels scene queries, returned video candidates, safe bounded downloads, source-page and contributor links, license notes, and third-party-rights warnings. Do not claim independent relevance scores. Keep candidate assets private under each job and avoid downloading unselected alternatives.
- Generate natural speech separately for each script scene when ElevenLabs is selected; measure each decoded segment with ffprobe and derive contiguous frame-aligned video, timeline, and caption boundaries from measured audio. Uploaded audio remains unchanged and untranscribed.
- Keep the offline mock's generated color cards and sine test tone visibly identified. The browser must show the active mode, expose provider/source disclosure, preview the actual returned MP4, and load synchronized captions from the exported SRT without changing existing route contracts.
- Use standard-library HTTP clients and finite timeouts/response-size limits to avoid new runtime packages. Add provider errors, missing-key, parsing/download, measured timing, and full render regression tests.
- Verification: run the complete pytest suite, `node --check app/static/app.js`, mock batch runner, and Docker Compose smoke test. Adapter tests with mocked HTTP are not evidence of live credentials. Only after real server-side credentials are configured may one real job be used to verify actual footage, audible natural speech, source/licence metadata, synchronized captions, final MP4 codecs/duration, artifact downloads, and same-file preview playback. Until then, record that acceptance check as pending.
- Current implementation status: provider adapters, server configuration, mode disclosure, measured TTS segment timing, source-aware storyboard, MP4 preview, and SRT-based browser captions are implemented. The real-provider adapter tests use mocked HTTP and local fixtures. Live Pexels/ElevenLabs acceptance remains pending until valid credentials are configured; demo mode remains available and honest.

## Assessment checklist and evidence

| Requirement area | Evidence to collect |
| --- | --- |
| UI, script/audio API, queued background jobs, status and results | Browser walkthrough plus API integration and end-to-end tests |
| Human editing: scene narration/captions, visual options and accept/reject flow, ratio changes, saved full rerender | Scene API tests plus downloaded artifact checks; manual browser review when a browser is available |
| Validation and structured errors | Positive and negative API tests; stable `error.code/message/retryable/details` shape |
| Scene logic and complete narration coverage | Unit tests for sentence splitting, deterministic choice, contiguous timings, and final end time |
| Media correctness | ffprobe output for codec, dimensions, frame rate, stream duration, and playable MP4 |
| Captions and provenance | Parse SRT, timeline JSON, and manifest; check every scene has rights and source evidence |
| Three ratios and no clip repetition | Render/inspect each ratio and compare selected clip identifiers per job |
| API contract, idempotency, health, config, and logs | Contract checks and API tests; inspect representative structured log records |
| Container and optional object storage | Docker Compose startup/readiness test; object-storage check only if included |
| Reliability and test pack | Failure-path tests, concurrency/idempotency tests, batch run index, and captured logs |
| Documentation and measured report | Clean-start walkthrough and measurements copied from real test-run outputs |

## Risks, limitations, and decisions still open

- **Real footage rights:** An opt-in Pexels adapter is implemented, but no credentials or live asset are available in this workspace. Generated-color output remains the default offline fallback; it is not genuine footage. Each live result still needs human review for third-party rights.
- **Mock speech:** A mock provider needs a local narration sample or a deterministic audio-generation method. Any generated tone/speech substitute will be visibly identified as test audio and cannot be described as natural TTS.
- **Media tooling:** FFmpeg may not be installed on the host. Container builds can provide it, but image availability and build access must be checked before claiming rendering works.
- **In-memory jobs:** The job queue and idempotency store are process-local and lose state on restart. Persistence and multi-process coordination are not provided.
- **Provider call cancellation:** A timed-out provider call is abandoned from the job's point of view, but Python cannot forcibly stop a provider thread that ignores its timeout. The adapters use finite HTTP timeouts, bounded responses, and bounded retries; the thread pool also bounds concurrent provider work.
- **Docker footprint:** The verified image is about 791 MiB. Container reliability is tested, but the FFmpeg package makes the image larger than the Python service alone; a smaller compatible FFmpeg distribution would need separate licence and codec verification.
- **Uploads and limits:** Audio duration is capped at ten minutes and `MAX_UPLOAD_BYTES` defaults to 25 MiB; uploaded source audio has no automatic retention/expiry policy.
- **Editor retention:** Stage 5A keeps uploaded audio in the local job folder for rerendering. No automatic cleanup/deletion endpoint exists yet; operators must remove local job directories when appropriate. Scene and job state remains process-local.
- **Provenance scores:** The mock selector does not compute semantic or relevance scores; exported score is null. No human scores exist until a reviewer fills the evaluation CSV.
- **Storage:** Local filesystem output is easiest for Stage 1. MinIO may add operational load and will be adopted only if it materially helps the assessment.
- **Open decisions:** Configure valid Pexels and ElevenLabs credentials and complete live-generation acceptance; decide whether a transcription source is needed; set a data retention policy; and confirm any official assessment criteria beyond the workspace/conversation materials.

