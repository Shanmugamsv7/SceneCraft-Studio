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
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open <http://127.0.0.1:8000>. Paste a script or select one MP3, WAV, or M4A file; choose landscape 16:9, portrait 9:16, or square 1:1; then create a draft. The dashboard shows job status and progress, a storyboard, narration and caption editors, visual choices, preview, and downloads. Save the scene edits and accepted/rejected visual decisions before selecting **Render full video**. That action rebuilds the whole video; there is no partial-scene render or scene cache.

Each job stores the scenes, candidate visuals, and exports selected when that job was created. Matching-logic changes do not update existing jobs. Create a new job to run the current scene-query and relevance rules. Results without strong scene-specific metadata are marked for human review and are not accepted or rendered automatically; inspect the candidate's actual visual content before accepting it.

Uploaded audio must be non-empty, supported, audible, readable by FFmpeg, no longer than ten minutes, and within the configured byte limit. The upload is not transcribed. Its initial caption says transcription is unavailable. Script mode uses the labelled mock test tone; it is not TTS.

## Docker Compose

For an isolated container with FFmpeg and ffprobe installed:

```powershell
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
docker compose up --build
```

Compose starts one app service and mounts `./data` for job files. It does not start MinIO or contact an external provider. The default port is 8000; change `PORT` in `.env` if needed. Stop with `Ctrl+C`, or run `docker compose down` in another terminal. The container image includes Debian's FFmpeg package and is substantially larger than the Python service alone.

`.env.example` documents settings. The FastAPI process does not automatically load a `.env` file; for local runs set environment variables in the shell. Docker Compose reads `.env` for interpolation. Defaults include `BROLL_PROVIDER=mock`, `TTS_PROVIDER=mock`, `MAX_FOOTAGE_BYTES=26214400` (25 MiB per video), `MAX_UPLOAD_BYTES=26214400`, `JOB_WORKERS=2`, and `PROVIDER_TIMEOUT_SECONDS=10`. Empty credentials are safe while mock mode is selected.

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

Then restart with `docker compose up --build`. For local Uvicorn runs, set the same values as PowerShell environment variables before starting the server. The app fails at startup with a setting-specific message if a real provider is selected without its required credentials; it does not silently substitute demo media. Credentials stay on the server and are not returned by `GET /api/v1/providers`.

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

When enabled, Commons receives scene-search text and returns file metadata and provider-ranked candidates; selected video is downloaded into the private job folder. Pexels remains available to existing key holders. ElevenLabs receives each script scene as a separate TTS request. Uploaded audio is not sent to these services. Provider errors do not silently substitute mock output. Source-page URLs, creator metadata, and license information are carried into the storyboard and exports when available. Search order is not represented as an independent relevance score. Review each source page, attribution, license, people, property, trademarks, and sensitive-topic appropriateness before use.

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
| `GET /api/v1/jobs/{job_id}/scenes/{scene_id}` | Returns one scene and available visual suggestions. |
| `PATCH /api/v1/jobs/{job_id}/scenes/{scene_id}` | Edits narration, caption, and selected visual/decision. |
| `POST /api/v1/jobs/{job_id}/scenes/{scene_id}/regenerate` | Produces fresh mock color-card suggestions for one scene. |
| `POST /api/v1/jobs/{job_id}/render` | Queues a full render using saved edits; may change aspect ratio. |
| `GET /api/v1/jobs/{job_id}/artifacts/{filename}` | Downloads an allowlisted artifact from that job. |

Errors use `detail.error.code`, `message`, `retryable`, and `details`. Requests accept or receive `X-Correlation-ID`; JSON logs include it and a job ID when one is available. Idempotency, jobs, and editor state exist only in process memory and reset on restart. There is no login or authentication. Keep this development service on a trusted local machine.

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

## Current verification and limits

On 2026-10-18, the Stage 5B full test suite passed **53 tests** with one upstream Starlette/httpx deprecation warning. The final local ten-case batch completed 10/10 jobs; downloaded exports were checked with FFprobe for H.264/AAC, expected 1080p-class dimensions, 30 fps, duration agreement, caption timing, provenance, and mock-only rights labeling. The exact job IDs, timings, output directories, and per-file checks are in `test-pack/stage5b-final-evaluation-results.json` and `test-pack/stage5b-final-export-validation.json`. The prior scene-edited sample was rechecked without overwriting it (`test-pack/stage5a-edited-output/stage5b-recheck.json`). These results describe that run and do not measure human relevance or aesthetic quality.

Provider-adapter follow-up verification previously passed **61 tests** with the same upstream warning. After adding support for the official Pexels Vimeo-hosted MP4 URL form, this workspace could not rerun Python tests because its `.venv` launcher refers to a removed Python 3.12 installation (`C:\Users\SHANMUGAM S V\AppData\Local\Programs\Python\Python312\python.exe`) and no system `python`/`py` command is available. The new tests cover Vimeo URL handling and host/path rejection but remain unexecuted here. `node --check app/static/app.js` passes in this session. No API credentials were configured and no live Pexels/ElevenLabs request or real-provider export was attempted. The Docker CLI is unavailable, so Compose startup was not retested.

No browser surfaces were available to the automation tool, so the page still needs human visual review. To review manually, start the local app, open `http://127.0.0.1:8000` in a desktop browser, create a short script job, check all three ratio controls, edit one scene's narration/caption, choose and accept a different visual, save, render, preview the video, and download all four artifacts. Repeat at a narrow/mobile viewport and verify keyboard focus, labels, loading/error states, and that edits survive a status refresh.

Known limitations: live Commons/Pexels/ElevenLabs integrations have not been validated in this workspace; the default mode still uses generated graphics and test-tone audio; provider-ranked results are not independent semantic relevance scores; uploaded audio is not transcribed; local/Commons license claims are not independently verified; scene/job state and idempotency are in memory; uploaded audio and downloaded provider assets remain in local job folders without automatic expiration; the service has no authentication; the official assessment form/brief was not found in this workspace; and final browser review is pending. No real-provider cost was measured. See [SOURCES.md](SOURCES.md), [AI_USE.md](AI_USE.md), [REPORT.md](REPORT.md), and [test-pack/requirements-matrix.md](test-pack/requirements-matrix.md) for detailed provenance and evidence.
