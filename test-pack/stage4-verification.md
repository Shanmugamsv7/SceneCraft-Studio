# Stage 4 verification record

Date: 2026-10-18 (host local date)

## Automated tests

Workspace environment command:

```powershell
& '.\.venv\Scripts\python.exe' -m pytest -q --basetemp=.pytest-tmp
```

Final output summary:

```text
43 passed, 1 warning in 19.18s
StarletteDeprecationWarning: Using `httpx` with `starlette.testclient` is deprecated; install `httpx2` instead.
```

Clean setup was also exercised using a temporary Python 3.12 virtual environment created with the installed interpreter (equivalent to the README's `py -3.12 -m venv .stage4-clean-env`):

```powershell
& 'C:\Users\SHANMUGAM S V\AppData\Local\Programs\Python\Python312\python.exe' -m venv .stage4-clean-env
& '.\.stage4-clean-env\Scripts\python.exe' -m pip install -r requirements-dev.txt
```

It was tested with:

```powershell
& '.\.stage4-clean-env\Scripts\python.exe' -m pytest -q --basetemp=.pytest-clean-tmp
```

It completed with `43 passed, 1 warning in 17.09s`. The temporary environment and its pytest output directory were removed after verification.

An earlier test run during implementation reported `35 passed, 1 failed`: the new audio/video drift assertion requested stream durations that its ffprobe command had not selected. The ffprobe query was corrected to request those fields. The final workspace and clean-environment runs both passed; the assertion remains enabled.

## Live batch

The app was started locally with Uvicorn on port 8766 and the existing batch runner was run with:

```powershell
& '.\.venv\Scripts\python.exe' -m app.batch_runner --base-url 'http://127.0.0.1:8766' --output 'test-pack/stage4-batch-results.json'
```

Recorded result: 2 attempted, 2 succeeded, 0 failed. The landscape job took 1.719 seconds and the portrait job 1.046 seconds according to the runner. These two local runs are not performance benchmarks. Full job IDs and result links are in `stage4-batch-results.json`.

## Docker and remaining checks

The regular shell did not find `docker` on PATH. The Docker Desktop CLI was available by its absolute path in the elevated shell (`C:\Users\SHANMUGAM S V\AppData\Local\Programs\DockerDesktop\resources\bin\docker.exe`; Docker version 29.8.2, Compose v5.6.0; daemon 29.8.2), so the real build and smoke test were run. Build command:

```powershell
$docker = 'C:\Users\SHANMUGAM S V\AppData\Local\Programs\DockerDesktop\resources\bin\docker.exe'
& $docker compose build --no-cache --progress plain
```

Result: `Image scripttobroll-app Built`; the Dockerfile's `ffmpeg -version` and `ffprobe -version` steps succeeded. The image measured 829,131,300 bytes (about 791 MiB). Compose was started with `PORT=8767`; `/ready` returned `{"status":"ready","checks":{"ffmpeg":true,"ffprobe":true,"job_storage":true,"provider":true}}`, `docker compose ps` reported `healthy`, and the live batch completed 2/2 jobs. FFprobe on the mounted artifacts confirmed H.264/AAC at 30 fps, 1920x1080 and 1080x1920, with timeline and file durations matching (4.4 s and 3.2 s). The container was stopped with `docker compose down`. Result JSON is in `stage4-docker-batch-results.json` and captured logs are in `stage4-docker-app.log`.

To repeat on this host, set `PORT=8767`; if `docker` is not on PATH, set `$docker` to the Docker Desktop executable path shown above:

```powershell
$env:PORT = '8767'
& $docker compose build --no-cache
& $docker compose up -d
& $docker compose ps
& $docker compose logs app
Invoke-RestMethod http://127.0.0.1:8767/ready
python -m app.batch_runner --base-url http://127.0.0.1:8767 --output test-pack/docker-batch-results.json
& $docker compose down
```

Automated frame checks verify that the generated mock render has no detected black segments and that sampled frame hashes change. Human review is still needed for actual playback, visual usefulness, semantic relevance, natural narration quality, and the authenticity of any future footage-provider rights evidence. There is no genuine licensed footage provider in this project yet. The large Debian FFmpeg package footprint is an unresolved container-size issue; a replacement FFmpeg source must retain required codecs and verified licence terms.
