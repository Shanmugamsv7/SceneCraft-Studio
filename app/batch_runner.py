from __future__ import annotations

import argparse
import json
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


DEFAULT_CASES = [
    {"name": "short-landscape-script", "script": "Morning light reaches the harbor. Boats return to the quiet shore.", "aspect_ratio": "16:9"},
    {"name": "short-portrait-script", "script": "A small garden grows beside the city street.", "aspect_ratio": "9:16"},
]


def load_cases(path: Path) -> list[dict[str, str]]:
    """Load a small JSON evaluation pack without accepting malformed entries."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list) or not raw:
        raise ValueError("The case file must contain a non-empty JSON array.")
    cases: list[dict[str, str]] = []
    names: set[str] = set()
    for item in raw:
        if not isinstance(item, dict) or not all(isinstance(item.get(key), str) for key in ("name", "script", "aspect_ratio")):
            raise ValueError("Each case must include string fields: name, script, aspect_ratio.")
        if not item["name"].strip() or not item["script"].strip() or item["aspect_ratio"] not in {"16:9", "9:16", "1:1"}:
            raise ValueError("Each case needs a name, non-empty script, and supported aspect_ratio.")
        if item["name"] in names:
            raise ValueError(f"Case names must be unique: {item['name']}")
        names.add(item["name"])
        cases.append({key: item[key] for key in ("name", "script", "aspect_ratio")})
    return cases


class ApiClient:
    def __init__(self, base_url: str, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def create_job(self, case: dict[str, str]) -> dict[str, Any]:
        boundary = f"SceneCraft Studio-{uuid.uuid4().hex}"
        fields = {"script": case["script"], "aspect_ratio": case["aspect_ratio"]}
        body = b"".join(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode("utf-8")
            for name, value in fields.items()
        ) + f"--{boundary}--\r\n".encode("ascii")
        request = Request(
            f"{self.base_url}/api/v1/broll/jobs",
            data=body,
            method="POST",
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}", "Idempotency-Key": f"batch-{uuid.uuid4()}"},
        )
        return self._json(request)

    def get_job(self, status_url: str) -> dict[str, Any]:
        return self._json(Request(f"{self.base_url}{status_url}"))

    def _json(self, request: Request) -> dict[str, Any]:
        try:
            with urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {exc.code}: {body[:1000]}") from exc
        except URLError as exc:
            raise RuntimeError(f"API request failed: {exc.reason}") from exc

    def download(self, artifact_url: str, target: Path) -> None:
        parsed = urlsplit(artifact_url)
        if parsed.scheme or parsed.netloc or not parsed.path.startswith("/api/v1/jobs/") or ".." in parsed.path.split("/"):
            raise ValueError("The API returned an unexpected artifact URL.")
        with urlopen(f"{self.base_url}{parsed.path}", timeout=self.timeout) as response:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(response.read())


def _download_and_validate(client: Any, job_id: str, result: dict[str, str], download_dir: Path) -> dict[str, Any] | None:
    download = getattr(client, "download", None)
    if not callable(download):
        return None
    expected = {"video", "captions", "timeline", "manifest"}
    if not expected.issubset(result):
        return {"passed": False, "checks": {}, "error": "One or more required artifact links are missing."}
    job_dir = download_dir / job_id
    paths: dict[str, Path] = {}
    for kind in sorted(expected):
        url = result[kind]
        filename = Path(urlsplit(url).path).name
        if not filename or filename in {".", ".."} or not re.fullmatch(r"[A-Za-z0-9_.-]+", filename):
            return {"passed": False, "checks": {}, "error": "An artifact link contained an unsafe filename."}
        target = job_dir / filename
        download(url, target)
        paths[kind] = target
    checks: dict[str, bool] = {}
    checks["files_nonempty"] = all(path.is_file() and path.stat().st_size > 0 for path in paths.values())
    checks["mp4_signature"] = paths["video"].read_bytes()[:32].find(b"ftyp") >= 0
    timeline = json.loads(paths["timeline"].read_text(encoding="utf-8"))
    manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    scenes = timeline.get("scenes", [])
    cursor = 0.0
    coverage_ok = bool(scenes)
    for scene in scenes:
        try:
            start, end = float(scene["start"]), float(scene["end"])
        except (KeyError, TypeError, ValueError):
            coverage_ok = False
            break
        if abs(start - cursor) > 0.00001 or end <= start:
            coverage_ok = False
            break
        cursor = end
    checks["timeline_contiguous"] = coverage_ok and abs(cursor - float(timeline.get("duration_seconds", -1))) <= 0.001
    checks["manifest_job_id"] = manifest.get("job_id") == job_id
    srt = paths["captions"].read_text(encoding="utf-8-sig")
    checks["caption_cue_count"] = len(re.findall(r"(?m)^\d+\s*$", srt)) == len(scenes)
    return {
        "passed": all(checks.values()),
        "checks": checks,
        "downloaded_files": {key: str(path) for key, path in paths.items()},
        "duration_seconds": timeline.get("duration_seconds"),
    }

def run_batch(
    client: Any,
    output_path: Path,
    cases: list[dict[str, str]] | None = None,
    poll_interval: float = 0.5,
    job_timeout: float = 600.0,
    download_dir: Path | None = None,
) -> dict[str, Any]:
    started_at = datetime.now(timezone.utc).isoformat()
    results: list[dict[str, Any]] = []
    for case in cases or DEFAULT_CASES:
        started = time.monotonic()
        entry: dict[str, Any] = {"name": case["name"], "aspect_ratio": case["aspect_ratio"], "status": "failed"}
        try:
            accepted = client.create_job(case)
            entry["job_id"] = accepted["job_id"]
            deadline = time.monotonic() + job_timeout
            while True:
                job = client.get_job(accepted["status_url"])
                if job.get("status") in {"succeeded", "failed"}:
                    entry["status"] = job["status"]
                    entry["error"] = job.get("error")
                    entry["result"] = job.get("result")
                    if job.get("status") == "succeeded" and download_dir is not None:
                        try:
                            entry["artifact_validation"] = _download_and_validate(client, accepted["job_id"], job.get("result") or {}, download_dir)
                        except Exception as exc:
                            entry["artifact_validation"] = {"passed": False, "checks": {}, "error": f"Artifact check failed: {type(exc).__name__}: {exc}"}
                    break
                if time.monotonic() >= deadline:
                    entry["error"] = {"code": "BATCH_TIMEOUT", "message": "Job did not finish before the batch timeout."}
                    break
                time.sleep(poll_interval)
        except Exception as exc:
            entry["error"] = {"code": "BATCH_REQUEST_FAILED", "message": str(exc)}
        entry["elapsed_seconds"] = round(time.monotonic() - started, 3)
        results.append(entry)

    job_succeeded = sum(item["status"] == "succeeded" for item in results)
    validation_failed = sum(item.get("artifact_validation") is not None and not item["artifact_validation"].get("passed") for item in results)
    summary = {
        "started_at": started_at,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "attempted": len(results),
        "job_succeeded": job_succeeded,
        "validation_failed": validation_failed,
        "succeeded": sum(item["status"] == "succeeded" and (item.get("artifact_validation") is None or item["artifact_validation"].get("passed")) for item in results),
        "failed": sum(item["status"] != "succeeded" or (item.get("artifact_validation") is not None and not item["artifact_validation"].get("passed")) for item in results),
        "jobs": results,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Submit sample mock-provider jobs and record actual outcomes.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="Base URL for the running SceneCraft Studio API")
    parser.add_argument("--output", type=Path, default=Path("test-pack/batch-results.json"), help="Path for the result JSON")
    parser.add_argument("--timeout", type=float, default=600.0, help="Maximum seconds to wait for each job")
    parser.add_argument("--cases", type=Path, help="Optional JSON array of representative script jobs")
    parser.add_argument("--download-dir", type=Path, help="Download and validate succeeded artifacts into this directory")
    args = parser.parse_args()
    cases = load_cases(args.cases) if args.cases else None
    summary = run_batch(ApiClient(args.base_url), args.output, cases=cases, job_timeout=args.timeout, download_dir=args.download_dir)
    print(json.dumps({key: summary[key] for key in ("attempted", "succeeded", "failed")}, indent=2))
    return 1 if summary["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
