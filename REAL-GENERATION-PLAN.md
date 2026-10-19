# SceneCraft Studio real-generation implementation plan

This plan describes the next implementation slice for real, script-relevant video and speech. The app will remain usable offline. Real providers are opt-in through server-side environment settings, and absent credentials must never trigger a silent mock substitution.

## Existing contracts to preserve

- FastAPI keeps `POST /api/v1/broll/jobs`, job status, scene read/edit/regenerate, full rerender, and artifact download routes and their current request/response fields.
- Jobs remain asynchronous and keep the existing states, progress values, correlation IDs, errors, in-memory storage, and per-job directories.
- The existing editor still stages narration/caption/visual changes and explicitly queues a complete rerender.
- Rendered files remain `video_{ratio}.mp4`, `captions.srt`, `timeline.json`, and `manifest.json`; the in-page preview must load the returned MP4 artifact URL.
- The offline mock provider and its test-tone/color-card labels remain available as the default demo mode.

## Proposed provider flow

For script jobs, split the source script into scene phrases, then ask a configured speech adapter to produce one audio segment for each phrase. Measure each segment with ffprobe, concatenate decoded audio, and derive contiguous frame-aligned scene/caption boundaries from cumulative measured segment durations. For uploaded-audio jobs, keep the current honest no-transcription behavior and use the uploaded recording unchanged.

For each scene, query a configured footage adapter with a concise phrase drawn from its narration. Pexels search results provide provider-ranked candidate clips, source-page URLs, contributor names, video-file URLs, and orientation metadata. Download only HTTPS files returned by the API, validate the response size and video stream, then normalize/crop them for the selected ratio with FFmpeg. Store the selected file under that job's private directory for the existing editor/rerender lifecycle. Never select the same clip twice within one render. Preserve Pexels source/photographer links and record that the Pexels License was reported for the API result while warning that model, property, trademark, and other third-party rights may still require review.

Selection relevance will not be represented as an invented numeric score. The provider's own search ranking is described as such; `relevance_score` remains null. Candidate alternatives remain visible/editable in the existing storyboard. A provider failure fails the job with the existing structured error shape rather than substituting mock media.

## Environment configuration

- `BROLL_PROVIDER=mock|pexels`, default `mock`.
- `PEXELS_API_KEY` required only for `BROLL_PROVIDER=pexels`.
- `TTS_PROVIDER=mock|elevenlabs`, default `mock`.
- `ELEVENLABS_API_KEY`, `ELEVENLABS_VOICE_ID`, and optionally `ELEVENLABS_MODEL_ID` required only for `TTS_PROVIDER=elevenlabs`.
- Existing provider timeout, upload, worker, FFmpeg, and data-directory settings remain in effect. Provider HTTP calls use finite timeouts and bounded response sizes.
- Credentials stay in the server environment or an untracked local `.env`; they are never returned by configuration/status endpoints or logged.

Real speech and real video may be selected independently. A generation is only called real for the provider(s) actually configured and used, and each job manifest identifies its speech and footage providers. Mock output remains explicitly mock.

## Stages and verification

1. **Provider contracts and configuration:** inspect current interfaces; add standard-library HTTP adapters and environment validation; prove missing keys produce actionable configuration errors while default mock mode still starts. Unit tests mock HTTP without network access.
2. **Relevant footage retrieval:** query Pexels using scene phrases and selected orientation; parse multiple alternatives; download bounded HTTPS MP4 candidates into job-private storage; validate and reject invalid/empty/repeated results. Tests cover auth, rate limit, timeout, malformed response, invalid URL/file, empty search, and no silent fallback.
3. **Natural speech and timing:** generate each script scene with the configured TTS adapter; measure segments, concatenate them, and calculate frame-aligned timings for video scenes and SRT cues. Tests verify actual audio streams, scene/caption coverage, provider errors, and edited narration on full rerender. Uploaded audio remains untranscribed.
4. **Video assembly and browser preview:** use the selected provider clip for every real scene; keep FFmpeg normalization, H.264/AAC, ratios, artifacts, and the existing job result URLs. Keep `<video>` bound to the actual exported MP4. Add provenance-aware UI labels and current provider-mode disclosure without discarding edits.
5. **Regression and live acceptance:** run all existing and new tests, Node syntax validation, the batch runner in mock mode, and Docker build/readiness when Docker is available. With valid user-provided Pexels and ElevenLabs credentials, create one short script job and verify actual source URLs/clip provenance, audible non-tone speech, codecs/duration/caption sync, artifact download, and playback of that same MP4. Without those credentials, mark the live external-provider acceptance check blocked and do not claim real generation verified.

## Dependencies, terms, and privacy

Use Python's standard-library `urllib` for HTTP to avoid a new runtime dependency. FFmpeg/ffprobe remain the media-processing dependencies. Pexels' official API documents `GET /v1/videos/search`, API-key authentication, video-file URLs, and a requirement for a prominent Pexels link; its official License permits commercial use subject to prohibited uses and warns that third-party permissions may still apply. ElevenLabs' official API documents `POST /v1/text-to-speech/{voice_id}` with `xi-api-key` authentication and audio output. API availability, quotas, price/plan access, and service terms are controlled by those providers and can change; this project will not call either service without an explicit provider selection and server-side credential.

When Pexels or ElevenLabs is enabled, scene text and/or narration text is sent to that provider. The UI and README must disclose this. No API key may be sent to the browser. No paid usage, external deployment, or real credential use is assumed by this plan.

## Known limits and acceptance boundary

- Real provider integrations cannot be called or described as end-to-end verified until valid credentials are configured and a resulting job is checked. Offline mocked-HTTP tests establish adapter behavior, not provider availability or actual account rights.
- Pexels API ranking is not an independent semantic-quality score. Human review remains necessary, particularly for sensitive topics, people, brands, and commercial context.
- ElevenLabs audio is natural provider-generated narration only when that adapter actually returns valid speech. The mock test tone remains labeled and must not be presented as speech.
- The current browser preview uses the MP4 result URL, so acceptance must verify that URL is the same rendered artifact downloaded by the user.
- Partial-scene rendering, transcription of uploaded audio, persistent jobs, and authentication remain outside this implementation slice.

## Official provider references reviewed

- Pexels [API documentation](https://www.pexels.com/api/documentation/) and [License](https://www.pexels.com/legal-pages/license).
- ElevenLabs [Create speech API](https://elevenlabs.io/docs/api-reference/text-to-speech/convert?explorer=true).
