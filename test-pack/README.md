# Test pack index

| Path | Contents |
|---|---|
| `requirements-matrix.md` | Requirement-by-requirement audit with status, implementation, evidence, and remaining work. |
| `evaluation-inputs.json` | Ten original positive mock-provider scripts covering different narration types and aspect ratios. |
| `stage5b-final-evaluation-results.json` | Final actual 10-job batch result with job IDs, status, elapsed seconds, result links, downloads, and artifact checks. |
| `stage5b-final-artifacts/<job-id>/` | The downloaded MP4, captions, timeline, and manifest for each final evaluation job. |
| `stage5b-final-export-validation.json` | FFprobe and metadata checks per final downloaded export, including measured dimensions, codec/rate/duration, provenance, and repeated-visual counts. |
| `human-relevance-ratings.csv` | One row per evaluated scene; numeric ratings/notes are blank pending genuine human review. |
| `stage5b-test-results.txt` | Captured output of the final full pytest run. |
| `stage5b-final-uvicorn.stdout.log`, `stage5b-final-uvicorn.stderr.log` | Server logs from the final successful ten-case run. |
| `stage5b-uvicorn.stdout.log`, `stage5b-uvicorn.stderr.log` | Logs from the local evaluation service; app logs do not include script or file contents. |
| `stage5b-batch-runner-initial-failure.json` | Preserved early development attempt that failed before creating jobs due to a misplaced helper; the runner was fixed and tested before the successful 10/10 run. |
| `stage5b-evaluation-results.json`, `stage5b-artifacts/`, `stage5b-export-validation.json` | First successful 10/10 evaluation run, retained separately from the final runner run. |
| `stage5a-edited-output/` | Prior scene-edit and latest-artifact verification evidence. |
| `stage5a-edited-output/stage5b-recheck.json` | Read-only Stage 5B revalidation of the existing edited MP4/SRT/timeline/manifest bundle. |
| `stage4-verification.md`, `stage4-*.json`, `stage5a-*.json` | Earlier dated regression, container, and batch records. They remain separate from the new Stage 5B run. |
| `final-demo-script.md` | A truthful unedited local walkthrough capped below eight minutes. |
| `final-submission-checklist.md` | Verified, pending-human, and blocked/not-implemented items plus a reproducible run order. |

The evaluation input and results are for the local generated-mock provider. They are not relevance benchmarks, licensing evidence for stock media, or human ratings.
