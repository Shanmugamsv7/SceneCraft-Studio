# Final demo script (about 5–6 minutes; maximum 8 minutes)

This is an honest, unedited walkthrough of offline mock mode. Optional Pexels/ElevenLabs adapters exist but have not been live-verified with credentials; this demo makes no claim of real footage, natural TTS, transcription, semantic scoring, or partial rendering. Use a local server and browser on a trusted machine.

## 0:00–0:40 — State the scope

Say: “This is SceneCraft Studio, a local human-guided editing workspace. This run uses demo mode: generated color-card visuals and a 440 Hz test tone, both labelled as mock output. Optional real providers are configured separately and have not been live-verified in this workspace.”

Point out the script editor, audio upload, format controls, job status, storyboard, preview, and export panel.

## 0:40–1:20 — Create a script job

Use this short script: “At sunrise, fishing boats return to the harbor. A market opens along the waterfront.” Leave audio empty and choose 16:9. Submit and show the queued/running progress, then wait for the actual terminal state. Point out that generation happens in a background job.

## 1:20–2:20 — Review scenes and provenance

Show the scene list and timing. Explain that sentence boundaries define scenes and scene times cover the generated timeline. Read the visual label and source/rationale: generated mock graphic, FFmpeg local source, no source URL, no semantic relevance score. Do not call it stock footage.

## 2:20–3:30 — Make a human edit

Change one scene's narration and caption. Choose a different mock color card and explicitly accept it. Save the edit. Refresh job status and show the pending edit remains visible. Explain that the edit is explicit and that selecting a visual does not erase narration or caption text.

## 3:30–4:30 — Rebuild and preview

Choose portrait 9:16 and request **Render full video**. Explain that this rebuilds the entire video, not just the edited scene. Wait for the actual job completion. Play the preview briefly and point out that the audio is a test tone rather than speech.

## 4:30–5:20 — Download and provenance check

Download the MP4, captions SRT, timeline JSON, and manifest JSON. Show the edited caption in the SRT/timeline and the generated-media rights declaration in the manifest. Show that the resolution, frame rate, and codecs are recorded and checked.

## 5:20–6:00 — State remaining gaps

Say: “Live footage and speech providers still need valid credentials and a real end-to-end check. The project has no speech transcription, independent relevance scoring, persistent job database, or partial-scene rendering. The test pack records automated checks separately from human review.”

Stop without implying the assessment is complete or submitting anything externally.
