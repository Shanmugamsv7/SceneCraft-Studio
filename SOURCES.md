# Sources, dependencies, and media provenance

Audit date: 2026-10-18. Package versions below were read from the workspace's Python 3.12.10 virtual environment. `requirements.txt` and `requirements-dev.txt` intentionally use compatible version ranges rather than pinning those observed versions.

## Application dependencies

| Package | Use | Version observed | License reported by installed distribution |
|---|---|---:|---|
| FastAPI | HTTP API and request validation | 0.143.3 | MIT |
| Uvicorn | ASGI development server | 0.54.0 | BSD-3-Clause |
| python-multipart | Multipart script/audio uploads | 0.0.32 | Apache-2.0 |
| Starlette | FastAPI ASGI foundation | 1.8.0 | BSD-3-Clause |
| Pydantic | Request models and validation | 2.14.1 | MIT |
| pydantic-core | Pydantic validation implementation | 2.50.1 | MIT |
| anyio | Async/concurrency support | 4.15.1 | MIT |
| annotated-types | Pydantic type metadata | 0.8.0 | MIT |
| annotated-doc | FastAPI/Pydantic documentation metadata | 0.0.5 | MIT |
| typing-inspection | Pydantic type inspection | 0.4.4 | MIT |
| typing-extensions | Python typing compatibility | 4.16.0 | PSF-2.0 |
| click | Uvicorn command-line interface | 8.5.0 | BSD-3-Clause |
| h11 | Uvicorn HTTP implementation | 0.16.0 | MIT |
| idna | URL support dependency | 3.20 | BSD-3-Clause |
| pytest | Automated tests | 8.4.2 | MIT |
| httpx | FastAPI test client transport | 0.28.1 | BSD-3-Clause |
| PyYAML | OpenAPI/Compose contract tests | 6.0.3 | MIT |
| httpcore | HTTPX transport | 1.0.9 | BSD-3-Clause |
| certifi | HTTP certificate bundle used by HTTPX | 2026.7.22 | MPL-2.0 |
| iniconfig | pytest configuration parser | 2.3.1 | MIT |
| packaging | pytest compatibility utilities | 26.3 | Apache-2.0 OR BSD-2-Clause |
| pluggy | pytest plugin framework | 1.7.0 | MIT |
| Pygments | pytest terminal formatting | 2.21.0 | BSD-2-Clause |
| colorama | Windows terminal color support in test tooling | 0.4.6 | See the license file included with the distribution; installed metadata did not expose a license field. |

The Python application uses only standard-library modules for its batch runner and file/job handling beyond the packages listed above. Package metadata/license fields can differ across future resolved versions; review the installed distribution notices before redistributing a packaged environment.

## Media software and generated media

- **FFmpeg and ffprobe** are external executables used for audio checks, test-tone generation, visual-card creation, MP4 encoding, and output validation. The host build inspected for this evidence is `ffmpeg version 9.0.2-essentials_build-www.gyan.dev`; `ffmpeg -L` reports GNU GPL version 3 or later, and the build enables GPL codecs including libx264. See the [FFmpeg legal page](https://ffmpeg.org/legal.html) and [FFmpeg license text](https://ffmpeg.org/legal.html#license). The Dockerfile installs Debian's FFmpeg package rather than bundling the host binary; its exact package build is resolved at image build time.
- Default/demo visuals are generated locally by FFmpeg's `color` source and moving `drawbox` overlay. No footage is bundled with the repository or test pack. Mock timeline and manifest records identify these graphics as generated, set source URL to null, and do not claim stock licensing or relevance scores.
- Default/demo script narration is a locally generated 440 Hz sine test tone using FFmpeg. It is not speech or TTS.
- User uploads are provided by the operator. The app does not transcribe them; no uploaded user audio is included in this evidence pack.
- `test-pack/evaluation-inputs.json` contains original, short evaluation scripts written for this project. No outside script corpus or model-generated dataset was imported.

## APIs, models, and provider integrations

- The application exposes its own local FastAPI API, described by `openapi.yaml`.
- The standard-library `urllib` client implements the Pexels videos search API (`GET https://api.pexels.com/v1/videos/search`) and the ElevenLabs text-to-speech API (`POST https://api.elevenlabs.io/v1/text-to-speech/{voice_id}`). No additional Python HTTP or provider SDK dependency is installed.
- Pexels is enabled only with `BROLL_PROVIDER=pexels` and server-side `PEXELS_API_KEY`. Its official [API documentation](https://www.pexels.com/api/documentation/) requires an Authorization key and prominent Pexels link; contributor credit is shown where available. Its official [Pexels License](https://www.pexels.com/legal-pages/license) permits commercial use subject to restrictions including implied endorsement and redistribution as a stock platform. The official [commercial-use help page](https://help.pexels.com/hc/en-us/articles/360042295214-Can-I-use-the-photos-and-videos-for-a-commercial-project) notes depicted people, brands, and logos can carry separate rights. The app preserves source-page URL and contributor name and warns users to review third-party rights. API search order is not recorded as a numeric relevance score. No live Pexels request was made during this implementation; no Pexels footage is included in repository or test-pack examples.
- ElevenLabs TTS is enabled only with `TTS_PROVIDER=elevenlabs`, server-side `ELEVENLABS_API_KEY`, and `ELEVENLABS_VOICE_ID`. It sends each script scene to the configured voice/model and returns MP3 audio, which FFmpeg decodes for assembly. The default model ID is `eleven_multilingual_v2`. See the official [Create speech API reference](https://elevenlabs.io/docs/api-reference/text-to-speech/convert?explorer=true). Account access, commercial usage rights, quotas, and charges depend on the operator's current account and terms; no monetary cost is reported here.
- The provider adapters have tests using mocked HTTP responses. They have not been tested with valid service credentials, so actual API access, live provider ranking, returned-media rights, and natural speech from the configured voice are not claimed as verified. No transcription API, bundled model, model file, or model-generated test dataset is used.

## Attribution

No third-party media appears in the recorded generated evaluation videos. FFmpeg output attribution and mock-media provenance are written in their timeline and manifest files. At runtime, Pexels media is used only when explicitly configured; the UI and exports retain the Pexels link, source page, contributor attribution, license note, and third-party-rights warning. Live per-asset rights have not been reviewed because the provider has not been called with credentials.
