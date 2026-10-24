# SceneCraft Studio — Script-to-B-roll Video Engine

SceneCraft Studio is a local, human-guided editing workspace. A person supplies a script or voice-over, reviews scene timing and visual options, edits narration and captions, explicitly accepts visual choices, then requests a full video render. The API reports background job progress and provides the latest video and metadata downloads.

The default is an explicitly labelled offline demo. FFmpeg creates moving color cards and a 440 Hz test tone; neither is stock footage or spoken narration. Real generation is opt-in through server environment variables: Pexels video search/download for footage and ElevenLabs text-to-speech for script narration. Uploaded audio is still used as supplied and is not transcribed.

## Requirements

- Python 3.12
- FFmpeg and ffprobe available on `PATH`
- Docker Compose (optional)
- Windows PowerShell examples below; the Python commands work on other platforms with the matching interpreter/activation syntax

## Local setup and run

From the project folder:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
# Edit .env locally if you want a provider other than the offline mock.
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open <http://127.0.0.1:8000>. Paste a script or select one MP3, WAV, or M4A file; choose landscape 16:9, portrait 9:16, or square 1:1; then create a storyboard. Inspect candidates inside each scene card; selecting a card only previews it, while **Accept selected** explicitly commits the visual choice. Save edits, then choose **Create draft preview** to render a separate playable full video without accepting pending visuals. Use **Render full video** only after accepting every scene; it creates the final export separately. Both previews rebuild the complete timeline; partial-scene rendering is not implemented.

Each job stores the scenes, candidate visuals, and exports selected when that job was created. Matching-logic changes do not update existing jobs. Create a new job to run the current scene-query and relevance rules. Results without strong scene-specific metadata are marked for human review and are not accepted or rendered automatically; inspect the candidate's actual visual content before accepting it.

Uploaded audio must be non-empty, supported, audible, readable by FFmpeg, no longer than ten minutes, and within the configured byte limit. The upload is not transcribed. Its initial caption says transcription is unavailable. Script mode uses the configured TTS provider; the default mock provider produces a labelled test tone, not speech.

## Docker Compose

For an isolated container with FFmpeg and ffprobe installed:

```powershell
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
docker compose up --build
```

Compose starts one app service and mounts `./data` for job files. It does not start MinIO or contact an external provider. The default port is 8000; change `PORT` in `.env` if needed. Stop with `Ctrl+C`, or run `docker compose down` in another terminal. The container image includes Debian's FFmpeg package and is substantially larger than the Python service alone.

`.env.example` documents settings. Local startup loads `.env` from the project root; operating-system environment variables take precedence. Copy `.env.example` to `.env`, add your server-side provider settings there, and restart Uvicorn after changes. Docker Compose also reads `.env` for interpolation. The default footage provider is Wikimedia Commons; set `WIKIMEDIA_CONTACT` to a public URL or email for its API User-Agent. Selecting Commons without that contact does not substitute still images or mock footage: provider status and generation return `BROLL_NOT_CONFIGURED`. `TTS_PROVIDER` defaults to `mock`; selecting ElevenLabs without both credentials returns an actionable `TTS_NOT_CONFIGURED` error.

## Railway Docker deployment

Railway builds the included `Dockerfile`; it installs FFmpeg, ffprobe, libass, and common fonts, verifies the `subtitles` filter and a captioned MP4 during the image build, serves FastAPI on `0.0.0.0`, and reads Railway's injected `PORT` (falling back to `8000` locally). Do not set a Railway start command: use the Dockerfile command. `/ready` reports `ffmpeg_subtitles` separately and stays unavailable if caption rendering is not supported.

Before adding real-provider credentials, create a Railway Volume and mount it at `/app/data`. It must retain `/app/data/jobs` (uploaded audio, downloaded clips, generated narration, MP4s, SRTs, timelines, and manifests) and `/app/data/footage-library` (locally imported clips and sidecars). Railway's ephemeral filesystem is not sufficient for exports. The image normally runs as an unprivileged user; Railway mounts volumes as root, so set `RAILWAY_RUN_UID=0` for this service to allow the mounted volume to be written.

Set these Railway service variables in the dashboard, never in source control or a committed `.env` file:

```text
BROLL_DATA_DIR=/app/data/jobs
BROLL_LOCAL_LIBRARY=/app/data/footage-library
BROLL_PROVIDER=wikimedia
WIKIMEDIA_CONTACT=<public contact URL or email for the Commons User-Agent>
TTS_PROVIDER=elevenlabs
ELEVENLABS_API_KEY=<Railway secret>
ELEVENLABS_VOICE_ID=<voice ID permitted for the account>
ELEVENLABS_MODEL_ID=eleven_multilingual_v2
SCENECRAFT_ACCESS_TOKEN=<long random secret>
RAILWAY_RUN_UID=0
JOB_WORKERS=2
PROVIDER_TIMEOUT_SECONDS=10
MAX_UPLOAD_BYTES=26214400
MAX_FOOTAGE_BYTES=26214400
```

`WIKIMEDIA_CONTACT` is not an API key, but it is required for Commons mode. Commons does not need `PEXELS_API_KEY`; leave that variable unset unless explicitly switching to Pexels. Keep the ElevenLabs key and the access token in Railway's secret-variable UI only. With `SCENECRAFT_ACCESS_TOKEN` set, all interactive pages, static assets, API routes, and artifact downloads require authentication; use the token as the **password** in the browser's Basic-auth prompt (any username), or send `Authorization: Bearer <token>` from an API client. `/health` and `/ready` remain unauthenticated only for platform checks.

In Railway service settings, set the HTTP health-check path to `/ready`; leave the custom start command blank so the Dockerfile `CMD` receives Railway's `PORT`. Railway detects a root-level `Dockerfile` automatically. These deployment details and the `RAILWAY_RUN_UID=0` volume requirement follow Railway's current [Dockerfile guidance](https://docs.railway.com/builds/dockerfiles), [start-command guidance](https://docs.railway.com/deployments/start-command), and [volume guidance](https://docs.railway.com/volumes).

Do not make this service publicly usable with real provider credentials and no access token: it has no user accounts, quotas, per-user artifact isolation, or billing controls. The built-in gate is the smallest shared-team safeguard; put a stronger identity-aware proxy in front of it for broader access.

Important persistence limitation: the files survive on the mounted volume, but jobs, idempotency records, progress, and scene/editor state are currently in process memory. A deploy, restart, or scale-out loses that state, so existing artifact routes cannot rediscover old jobs even though their files remain on disk. Run one replica and treat restart recovery/durable job history as a future database/queue requirement rather than assuming the volume solves it.

### Optional real providers

For Commons footage, set `BROLL_PROVIDER=wikimedia` and `WIKIMEDIA_CONTACT` to a public contact URL or email for the descriptive API User-Agent. Commons search is read-only and does not require an API key. Check each file page and its license terms before use; Commons file metadata is uploader-provided and is not independently guaranteed. Alternatively, set `BROLL_PROVIDER=local` and import clips you have rights to use through the editing dashboard. The existing Pexels adapter remains available to users with valid credentials. ElevenLabs requires an API key and voice ID; usage may be billable. No external request is made in default demo mode.

For Docker Compose, copy `.env.example` to the ignored `.env` file and set the following values there. Leave either provider on `mock` if you do not want to enable it:

```dotenv
BROLL_PROVIDER=wikimedia
WIKIMEDIA_CONTACT=<public-contact-url-or-email>
TTS_PROVIDER=elevenlabs
ELEVENLABS_API_KEY=<your-server-side-ElevenLabs-key>
ELEVENLABS_VOICE_ID=<voice-id-from-your-ElevenLabs-account>
ELEVENLABS_MODEL_ID=eleven_multilingual_v2
```

For Pexels video footage instead, use:

```dotenv
BROLL_PROVIDER=pexels
PEXELS_API_KEY=<your-server-side-Pexels-key>
```

The storyboard searches each scene with a scene-specific query (for example, `cyclist rides forest trail sunrise` and `quiet lake mist drifts water`), shows Pexels source, contributor, license note, and source-page link for every candidate, and downloads only the accepted candidate when rendering.

Then restart with `docker compose up --build`. For local Uvicorn runs, place the values in the root `.env` file or set them as PowerShell environment variables before starting the server. Credentials stay on the server and are not returned by `GET /api/v1/providers`. If the key or voice ID is missing/invalid, the app reports an actionable configuration/provider error; it does not silently substitute demo media.

### Choose an ElevenLabs voice allowed by your plan

`ELEVENLABS_VOICE_ID` must identify a voice this account is permitted to use through the API. ElevenLabs documents that Voice Library voices are unavailable through the API on the free tier ([Voice Library access rules](https://elevenlabs.io/docs/eleven-creative/voices/voice-library)). For a free-tier account, choose a voice from the account's own/default voices that the account shows as API-available; do not use a community/Voice Library voice. The API's [voice listing](https://elevenlabs.io/docs/api-reference/voices/search) supports `voice_type=non-community` to exclude library copies, but check the account's plan eligibility as well. SceneCraft does not choose or replace a voice automatically.

To change the voice, copy the eligible voice ID from the ElevenLabs account, edit only `ELEVENLABS_VOICE_ID` in the project-root `.env`, save it, and restart the application. For local Uvicorn, stop it with `Ctrl+C` and run the documented startup command again. For Docker Compose, run `docker compose up --build` again. If ElevenLabs reports HTTP 402, SceneCraft shows the provider's sanitized explanation and directs you to check voice eligibility; it does not generate a beep or switch to mock narration.

For local PowerShell, use these commands in the same terminal where Uvicorn will run (replace placeholders locally; do not paste keys into source files or chat):

```powershell
$env:BROLL_PROVIDER = "wikimedia"
$env:WIKIMEDIA_CONTACT = "<public-contact-url-or-email>"
$env:TTS_PROVIDER = "elevenlabs"
$env:ELEVENLABS_API_KEY = "<your-ElevenLabs-key>"
$env:ELEVENLABS_VOICE_ID = "<your-voice-id>"
$env:ELEVENLABS_MODEL_ID = "eleven_multilingual_v2"
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

To run Commons footage with demo narration, set `BROLL_PROVIDER=wikimedia` and `WIKIMEDIA_CONTACT`, leaving `TTS_PROVIDER=mock`. To use imported local clips, select `BROLL_PROVIDER=local`; imported rights remain your responsibility. To run natural speech with mock visuals, set the ElevenLabs variables and leave `BROLL_PROVIDER=mock`.

When enabled, Commons receives scene-search text and returns only supported video files with file-page, creator, and license metadata; selected video is downloaded into the private job folder. Pexels remains available to existing key holders. ElevenLabs receives each script scene as a separate TTS request. Uploaded audio is not sent to these services. Provider errors or empty Commons searches do not silently substitute still images or mock output. Source-page URLs, creator metadata, and license information are carried into the storyboard and exports when available. Search order is not represented as an independent relevance score. Review each source page, attribution, license, people, property, trademarks, and sensitive-topic appropriateness before use.

## Architecture

The browser submits a script or uploaded audio to FastAPI. FastAPI validates the request, creates an in-memory job, and queues FFmpeg and configured provider work in a bounded background worker pool. Mock mode uses deterministic cards and test-tone narration. Commons and Pexels modes search each scene phrase and download the selected clip; local mode searches imported footage by filename words. ElevenLabs mode generates speech per scene and measures each audio segment to set contiguous frame-aligned caption and scene timings. FFmpeg normalizes selected footage, assembles the MP4, and ffprobe verifies it before success. Each job has a separate directory under `data/jobs/<job-id>/`.

The export includes `video_{ratio}.mp4`, `captions.srt`, `timeline.json`, and `manifest.json`. The editor preview plays the returned MP4 and loads the SRT cues as synchronized browser captions. Manifests name the providers actually used. Mock assets remain labeled mock; Pexels source URLs, contributor attribution, license notes, and warnings travel with the selected media. No independent numeric relevance score is calculated.

## API routes

The OpenAPI 3.1 contract is in [openapi.yaml](openapi.yaml), served interactively at <http://127.0.0.1:8000/api/v1/docs>.

| Method and path | Purpose |
|---|---|
| `GET /health` | Process response check. |
| `GET /ready` | Readiness checks for FFmpeg, ffprobe, writable job storage, and configured provider. |
| `GET /api/v1/providers` | Reports active provider names and demo/real mode without exposing credentials. |
| `POST /api/v1/broll/jobs` | Accepts multipart script or audio and an aspect ratio; returns 202 and a job ID. Supports `Idempotency-Key`. |
| `GET /api/v1/jobs/{job_id}` | Returns job state, progress, error, results, and scene data. |
| `GET /api/v1/jobs/{job_id}/voice` / `POST` same path | Reads narration status or creates/reuses narration for the saved script scenes. A generated audio URL is returned when ready. |
| `GET /api/v1/jobs/{job_id}/voice/audio?signature=...` | Plays the current generated narration WAV; signatures prevent stale audio links from being used after text edits. |
| `GET /api/v1/jobs/{job_id}/scenes/{scene_id}` | Returns one scene and available visual suggestions. |
| `PATCH /api/v1/jobs/{job_id}/scenes/{scene_id}` | Edits narration, caption, and selected visual/decision. |
| `POST /api/v1/jobs/{job_id}/scenes` / `DELETE /api/v1/jobs/{job_id}/scenes/{scene_id}` | Adds or removes a saved storyboard scene; deletion requires confirmation. |
| `POST /api/v1/jobs/{job_id}/scenes/{scene_id}/split` | Splits an editable scene at a validated timeline position and clears both new visual decisions for review. |
| `POST /api/v1/jobs/{job_id}/scenes/{scene_id}/local-video` | Validates and stores a rights-declared local video for one scene, then explicitly selects it. |
| `POST /api/v1/jobs/{job_id}/scenes/{scene_id}/regenerate` | Produces fresh mock color-card suggestions for one scene. |
| `POST /api/v1/jobs/{job_id}/render` | Queues a full render using saved edits; may change aspect ratio. |
| `POST /api/v1/jobs/{job_id}/draft-preview` / `GET` same path | Queues and polls a separate full draft preview from saved scene choices; pending suggestions remain pending. |
| `GET /api/v1/jobs/{job_id}/scenes/{scene_id}/visuals/{visual_id}/preview` | Streams a candidate source image or video for inspection without accepting it. |
| `GET /api/v1/jobs/{job_id}/artifacts/{filename}` | Downloads an allowlisted artifact from that job. |

Errors use `detail.error.code`, `message`, `retryable`, and `details`. Requests accept or receive `X-Correlation-ID`; JSON logs include it and a job ID when one is available. Idempotency, jobs, and editor state exist only in process memory and reset on restart. Authentication is disabled by default for trusted local development; set `SCENECRAFT_ACCESS_TOKEN` for the deployment access gate described above.

## Tests and evaluation pack

Run the full suite:

```powershell
python -m pytest -q --basetemp=.pytest-tmp
```

With Uvicorn running in another terminal, run the ten varied evaluation scripts and download/validate the returned artifacts:

```powershell
python -m app.batch_runner --base-url http://127.0.0.1:8000 --cases test-pack/evaluation-inputs.json --output test-pack/stage5b-final-evaluation-results.json --download-dir test-pack/stage5b-final-artifacts
```

The result JSON records job IDs, statuses, measured end-to-end elapsed seconds, result links, artifact paths, and automated validation checks. It is a specific local run, not a performance benchmark. The ten scripts cover concrete and abstract narration, fictional statistics, ambiguous wording, named entities, a sensitive recovery topic, very short narration, and a longer multi-scene script. Negative cases are covered by pytest; the batch runner submits positive script cases only. Human relevance scores are deliberately blank in `test-pack/human-relevance-ratings.csv` until a person reviews the scenes.

## Footage provider and licensing

`app/providers.py` defines the swappable footage and speech provider interfaces. Offline mock, Wikimedia Commons, existing-key Pexels, and local-library footage providers are implemented; demo color cards are not licensed stock. The ElevenLabs adapter is available for script TTS. No footage dataset or provider clip is bundled in the repository.

The Commons adapter uses the read-only [MediaWiki Action API](https://commons.wikimedia.org/wiki/Commons:API/MediaWiki), with a descriptive User-Agent containing the configured public contact, bounded requests, HTTPS host/path checks, response media-type checks, download-size caps, and license/creator metadata from each file page. It accepts a limited set of commercial-use license labels, but the labels are uploader-supplied; inspect the linked file page and applicable third-party rights. HTTP 429 responses are surfaced with any `Retry-After` hint and are not immediately retried. The local footage importer stores operator-provided license and attribution declarations and does not verify them. Pexels remains available using its official [video search API](https://www.pexels.com/api/documentation/) and [license](https://www.pexels.com/legal-pages/license), with its existing server-side key requirement and the same bounded download checks. The [ElevenLabs TTS API](https://elevenlabs.io/docs/api-reference/text-to-speech/convert?explorer=true) uses a server-side key, voice ID, and model. No live Commons request, real-video export, or ElevenLabs speech generation has been verified in this workspace. The adapters have offline mocked-HTTP tests. Never put real keys in source control.

For script narration, set `TTS_PROVIDER=elevenlabs`, `ELEVENLABS_API_KEY`, and `ELEVENLABS_VOICE_ID`; `ELEVENLABS_MODEL_ID` defaults to `eleven_multilingual_v2`. These are server environment variables only—never put them in frontend code, source control, logs, or chat. The UI's **Generate narration** action saves current scene edits first, then queues/reuses narration and offers an audio player. Uploaded voice-over remains available and is used as supplied without transcription. In mock mode, the UI identifies the generated 440 Hz tone as DEMO and explicitly says it is not speech.

Generated speech is requested per scene so FFprobe-measured segment durations can set contiguous scene and caption timing. MP3 segments and normalized WAV audio are cached inside the job directory. Cache keys include provider, model, voice, scene text, and scene order; unchanged scene audio can be reused after another scene changes, while changing narration or voice configuration produces a different cache key. Draft and final renders reuse this same generated WAV instead of making another TTS request. Errors, timeouts, rate limits, and invalid audio fail visibly; real provider failures are not replaced with test tones.

No live Commons request, real-video export, or ElevenLabs speech generation has been verified in this workspace. The ElevenLabs adapter and cache behavior have offline mocked tests. Never put real keys in source control.

## Current verification and limits

On 2026-10-18, the Stage 5B full test suite passed **53 tests** with one upstream Starlette/httpx deprecation warning. The final local ten-case batch completed 10/10 jobs; downloaded exports were checked with FFprobe for H.264/AAC, expected 1080p-class dimensions, 30 fps, duration agreement, caption timing, provenance, and mock-only rights labeling. The exact job IDs, timings, output directories, and per-file checks are in `test-pack/stage5b-final-evaluation-results.json` and `test-pack/stage5b-final-export-validation.json`. The prior scene-edited sample was rechecked without overwriting it (`test-pack/stage5a-edited-output/stage5b-recheck.json`). These results describe that run and do not measure human relevance or aesthetic quality.

Provider-adapter follow-up verification previously passed **61 tests** with the same upstream warning. After adding support for the official Pexels Vimeo-hosted MP4 URL form, this workspace could not rerun Python tests because its `.venv` launcher refers to a removed Python 3.12 installation (`C:\Users\SHANMUGAM S V\AppData\Local\Programs\Python\Python312\python.exe`) and no system `python`/`py` command is available. The new tests cover Vimeo URL handling and host/path rejection but remain unexecuted here. `node --check app/static/app.js` passes in this session. No API credentials were configured and no live Pexels/ElevenLabs request or real-provider export was attempted. The Docker CLI is unavailable, so Compose startup was not retested.

No browser surfaces were available to the automation tool, so the page still needs human visual review. To review manually, start the local app, open `http://127.0.0.1:8000` in a desktop browser, create a short script job, check all three ratio controls, edit one scene's narration/caption, choose and accept a different visual, save, render, preview the video, and download all four artifacts. Repeat at a narrow/mobile viewport and verify keyboard focus, labels, loading/error states, and that edits survive a status refresh.

Known limitations: live Commons/Pexels/ElevenLabs integrations have not been validated in this workspace; the default mode still uses generated graphics and test-tone audio; provider-ranked results are not independent semantic relevance scores; uploaded audio is not transcribed; local/Commons license claims are not independently verified; scene/job state and idempotency are in memory; uploaded audio and downloaded provider assets remain in local job folders without automatic expiration; the optional shared-token gate is not a replacement for user accounts or durable job recovery; the official assessment form/brief was not found in this workspace; and final browser review is pending. No real-provider cost was measured. See [SOURCES.md](SOURCES.md), [AI_USE.md](AI_USE.md), [REPORT.md](REPORT.md), and [test-pack/requirements-matrix.md](test-pack/requirements-matrix.md) for detailed provenance and evidence.
