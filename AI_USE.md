# AI use and coding assistance

## In the running application

The application defaults to demo mode: script jobs use an FFmpeg-generated 440 Hz test tone and visuals are generated color cards. Neither is presented as natural speech or licensed stock footage. Uploaded audio is used as audio and is not transcribed. No speech-recognition model or semantic-scoring model is bundled or used.

Optional external provider integrations are available only when an operator explicitly selects them in server configuration. Pexels is queried for video candidates from scene text; ElevenLabs can synthesize narration from script scenes. When enabled, those providers receive the relevant scene/script text. The application does not send API keys to the browser. This workspace's adapter tests used mocked HTTP responses, and no live provider or paid API request was made during this change. Therefore actual generated speech, real provider footage, service access, and provider billing have not been verified here.

No API keys, model files, or paid API usage are required for local demo operation. No model-generated dataset or third-party footage was used for the recorded test pack.

## Coding assistance

OpenAI Codex was used as an AI coding assistant in the workspace to inspect project files, edit code and documentation, and run local tests and media checks. The agent in this session identifies as GPT-6. This is coding-assistance disclosure, not a claim that the application uses or ships an OpenAI model. No OpenAI API request or paid model call was made by the application.

The human operator remains responsible for reviewing the generated code, evaluating video relevance, checking licensing, and completing any assessment submission. Human scene-relevance ratings have intentionally been left blank in `test-pack/human-relevance-ratings.csv`.
