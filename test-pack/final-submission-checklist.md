# Final submission checklist

This is a project evidence checklist, not an assessment-complete declaration. A separate official rubric/form was not present in the workspace.

## Verified in this workspace

- [x] Updated Python test suite passed 61 tests in this provider-adapter follow-up; see `REPORT.md`. The `stage5b-test-results.txt` file remains the historical Stage 5B run.
- [x] Ten varied local mock jobs completed; IDs, states, elapsed time, result links, and validation are in `stage5b-final-evaluation-results.json`.
- [x] Forty downloaded artifacts are under `stage5b-final-artifacts/<job-id>/`.
- [x] FFprobe checks for H.264/AAC, 30 fps, expected dimensions, duration agreement, captions, and manifest provenance are in `stage5b-final-export-validation.json`.
- [x] All three aspect ratios are present in the measured outputs.
- [x] Human rating CSV created with blank scores and notes; no reviewer ratings are claimed.
- [x] Pexels video and ElevenLabs TTS adapters, server environment configuration, provider disclosures, source links, and mocked-HTTP tests are implemented.
- [x] Setup, API routes, provider limitations, dependency/media sources, and AI-use disclosures are documented.
- [x] Prior Stage 4/5A Docker build/readiness evidence exists. Compose now passes environment variables for optional providers, but no fresh build/smoke test was possible because Docker CLI is unavailable in this environment.
- [x] No external deployment, publication, submission, or push was performed.
- [x] High-confidence secret-pattern scan covered 86 project text files without printing matching values; no likely secret patterns were found. Runtime `data/` was excluded because it may contain user uploads.

## Pending human work

- [ ] Open the local website in a real browser. Check desktop and mobile layouts, keyboard/focus behavior, labels, loading/error states, and that edits survive polling. Automation reported no browser surfaces. Follow the manual steps in `README.md`.
- [ ] Review every scene visually and enter genuine 1–5 relevance scores and notes into `human-relevance-ratings.csv`; keep blank until reviewed.
- [ ] Compare the requirements matrix with the official assessor-issued brief and form, if available outside this workspace.
- [ ] Review final output playback and suitability for the intended audience. Automated codec/timing tests do not establish aesthetic or semantic quality.

## Blocked or not implemented

- [ ] Live credential-backed Pexels footage and ElevenLabs natural-speech generation, with playback and per-asset review. Adapters are implemented but not live-verified.
- [ ] Independent semantic matching and measured relevance scoring; Pexels API ranking is not an independent score.
- [ ] Speech transcription.
- [ ] Partial-scene render/cache reuse.
- [ ] Persistent job/edit state and automatic upload retention policy.
- [ ] Git tracked/untracked audit: the supplied workspace copy has no `.git` metadata.
- [ ] Exact assessment form-entry guidance: no submission form or official rubric file was found.

## Reproducible sequence

1. Create and activate a Python 3.12 virtual environment and install `requirements-dev.txt` as shown in `README.md`.
2. Ensure `ffmpeg` and `ffprobe` are on `PATH`.
3. Start `python -m uvicorn app.main:app --host 127.0.0.1 --port 8000`.
4. In a second terminal, run `python -m pytest -q --basetemp=.pytest-tmp`.
5. Run the ten-case command in `README.md`, with `--cases test-pack/evaluation-inputs.json` and `--download-dir test-pack/stage5b-final-artifacts`.
6. Inspect the run JSON and export validation JSON; do not copy results from earlier runs into a new run's report.
7. For the container path, create `.env` from `.env.example` only if it does not already exist, then run `docker compose up --build`, check `/ready`, and stop with `docker compose down`.
