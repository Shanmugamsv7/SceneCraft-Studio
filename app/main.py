from __future__ import annotations

import json
import logging
import mimetypes
import os
import re
import base64
import binascii
import secrets
import shutil
import subprocess
import threading
import uuid
import contextvars
import hashlib
import math
import time
from fractions import Fraction
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, UploadFile, Body
from fastapi import Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse
from app.config import settings
from app.pipeline import FRAME_RATE, allocate_scene_timings, allocate_scene_timings_by_duration, analyze_scene, caption_chunks, segment_script
from app.providers import CommonsRequestLimiter, ElevenLabsSpeechProvider, FallbackFootageProvider, FootageProvider, LocalFootageLibraryProvider, MOCK_PROVIDER, PexelsFootageProvider, ProviderError, SceneVisual, UnconfiguredFootageProvider, WikimediaCommonsFootageProvider

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = settings.data_dir
STATIC_DIR = ROOT / "app" / "static"
MAX_UPLOAD_BYTES = settings.max_upload_bytes
MAX_SCRIPT_CHARACTERS = 20_000
ALLOWED_EXTENSIONS = {".mp3", ".wav", ".m4a"}
ALLOWED_FOOTAGE_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm", ".ogv", ".mkv"}
RATIOS: dict[str, tuple[int, int]] = {"16:9": (1920, 1080), "9:16": (1080, 1920), "1:1": (1080, 1080)}
LOCAL_FOOTAGE_PROVIDER = LocalFootageLibraryProvider(settings.local_footage_dir, settings.max_footage_bytes)
if settings.provider == "pexels":
    if settings.pexels_api_key:
        _primary_footage_provider: FootageProvider = PexelsFootageProvider(settings.pexels_api_key, DATA_DIR, settings.provider_timeout_seconds, settings.max_footage_bytes)
        FOOTAGE_PROVIDER: FootageProvider = FallbackFootageProvider(_primary_footage_provider, LOCAL_FOOTAGE_PROVIDER)
    else:
        FOOTAGE_PROVIDER = UnconfiguredFootageProvider(
            "pexels-unconfigured",
            "videos-api-v1",
            "Pexels footage is selected but PEXELS_API_KEY is missing. Add a valid PEXELS_API_KEY to the server environment or untracked .env file, then restart SceneCraft Studio. Demo graphics will not be used as footage.",
        )
elif settings.provider == "wikimedia":
    if settings.wikimedia_contact:
        _commons_request_limiter = CommonsRequestLimiter()
        _primary_footage_provider = WikimediaCommonsFootageProvider(DATA_DIR, settings.wikimedia_contact, settings.provider_timeout_seconds, settings.max_footage_bytes, request_limiter=_commons_request_limiter)
        # Commons mode intentionally offers video files only. Still images are
        # not footage and must not be presented as a video fallback.
        FOOTAGE_PROVIDER = FallbackFootageProvider(_primary_footage_provider, LOCAL_FOOTAGE_PROVIDER)
    else:
        FOOTAGE_PROVIDER = UnconfiguredFootageProvider(
            "wikimedia-commons-unconfigured",
            "mediawiki-action-api",
            "Wikimedia Commons footage is selected but WIKIMEDIA_CONTACT is missing. Add a public contact URL or email for the Commons API User-Agent to the server environment or untracked .env file, then restart SceneCraft Studio. No still images or demo graphics will be substituted as footage.",
        )
elif settings.provider == "local":
    FOOTAGE_PROVIDER = LOCAL_FOOTAGE_PROVIDER
else:
    FOOTAGE_PROVIDER = MOCK_PROVIDER


def _create_speech_provider(config: Any) -> ElevenLabsSpeechProvider | None:
    if config.speech_provider != "elevenlabs" or not config.elevenlabs_api_key or not config.elevenlabs_voice_id:
        return None
    return ElevenLabsSpeechProvider(config.elevenlabs_api_key, config.elevenlabs_voice_id, config.elevenlabs_model_id, config.provider_timeout_seconds)


SPEECH_PROVIDER = _create_speech_provider(settings)
logger = logging.getLogger("scenecraft")
correlation_context: contextvars.ContextVar[str] = contextvars.ContextVar("correlation_id", default="-")
job_context: contextvars.ContextVar[str] = contextvars.ContextVar("job_id", default="-")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "job_id": getattr(record, "job_id", job_context.get()),
            "correlation_id": getattr(record, "correlation_id", correlation_context.get()),
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


logger.handlers.clear()
logger.setLevel(logging.INFO)
logger.propagate = False
json_handler = logging.StreamHandler()
json_handler.setFormatter(JsonFormatter())
logger.addHandler(json_handler)
app = FastAPI(title="SceneCraft API", description="Local API for script-to-B-roll video drafts, scene edits, and artifact exports.", version="0.2.0", openapi_url="/api/v1/openapi.json", docs_url="/api/v1/docs", redoc_url=None)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
executor = ThreadPoolExecutor(max_workers=settings.job_workers, thread_name_prefix="broll")
provider_executor = ThreadPoolExecutor(max_workers=settings.job_workers, thread_name_prefix="broll-provider")
PROVIDER_TIMEOUT_SECONDS = settings.provider_timeout_seconds
PROVIDER_MAX_ATTEMPTS = 2
SPEECH_MAX_ATTEMPTS = 2
jobs: dict[str, dict[str, Any]] = {}
jobs_lock = threading.Lock()
idempotency_records: dict[str, tuple[str, dict[str, str]]] = {}


class SceneEditRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    narration_text: str | None = Field(default=None, max_length=2000)
    caption_text: str | None = Field(default=None, max_length=500)
    visual_id: str | None = Field(default=None, max_length=200)
    visual_decision: str | None = None


class SceneCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    narration_text: str = Field(min_length=1, max_length=2000)
    caption_text: str | None = Field(default=None, max_length=500)
    duration: float = Field(default=4.0, gt=0, le=120)
    insert_at: int | None = Field(default=None, ge=0)


class SceneSplitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    split_at: float = Field(gt=0)


class SceneRemoveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm: bool = False


class RenderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    aspect_ratio: str | None = None


def _visual_details(visual: SceneVisual) -> dict[str, Any]:
    return {
        "id": visual.asset_id,
        "color": visual.color,
        "provider": visual.provider,
        "source": visual.source,
        "media_type": visual.media_type,
        "selection_requires_review": visual.selection_requires_review,
        "source_url": visual.source_url,
        "license": visual.license,
        "license_verified": visual.license_verified,
        "rights_basis": visual.rights_basis,
        "attribution": visual.attribution,
        "ai_generated": visual.ai_generated,
        "selection_reason": visual.selection_reason,
        "relevance_score": visual.relevance_score,
        "keyword_overlap_score": visual.keyword_overlap_score,
        "warnings": list(visual.warnings),
        "thumbnail_url": visual.thumbnail_url,
    }


def _safe_provider_error_details(details: object) -> dict[str, str | int | float | bool]:
    """Keep useful diagnostics without exposing arbitrary provider payloads."""
    if not isinstance(details, dict):
        return {}
    allowed = {"stage", "elapsed_ms", "status", "status_code", "retry_after", "retry_after_seconds", "attempt", "max_attempts", "timeout_seconds", "exception_type", "orientation", "max_bytes", "provider_error_code", "provider_error_type", "provider_request_id"}
    safe: dict[str, str | int | float | bool] = {}
    for key in allowed:
        value = details.get(key)
        if type(value) in {str, int, float, bool} and (not isinstance(value, str) or len(value) <= 128):
            safe[key] = value
    if "status" not in safe and "status_code" in safe:
        safe["status"] = safe["status_code"]
    return safe


def _scene_response(scene: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in scene.items() if not key.startswith("_")}


def _error(code: str, message: str, status_code: int, details: Any = None, retryable: bool = False) -> HTTPException:
    return HTTPException(status_code, {"error": {"code": code, "message": message, "retryable": retryable, "details": details}})


def _ffmpeg_path() -> str | None:
    return shutil.which(settings.ffmpeg_bin)


def _ffprobe_path() -> str | None:
    return shutil.which(settings.ffprobe_bin)


def _error_body(code: str, message: str, retryable: bool = False, details: Any = None) -> dict[str, Any]:
    return {"detail": {"error": {"code": code, "message": message, "retryable": retryable, "details": details}}}


def _access_token_is_valid(request: Request) -> bool:
    """Accept an optional deployment token without exposing it to the UI."""
    expected = settings.access_token
    if not expected:
        return True
    authorization = request.headers.get("authorization", "")
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() == "bearer":
        return secrets.compare_digest(value, expected)
    if scheme.lower() != "basic":
        return False
    try:
        decoded = base64.b64decode(value, validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError, binascii.Error):
        return False
    _username, separator, password = decoded.partition(":")
    return bool(separator) and secrets.compare_digest(password, expected)


@app.middleware("http")
async def correlation_middleware(request: Request, call_next):
    supplied_id = request.headers.get("X-Correlation-ID", "")
    correlation_id = supplied_id if re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", supplied_id) else str(uuid.uuid4())
    correlation_token = correlation_context.set(correlation_id)
    match = re.search(r"/api/v1/jobs/([0-9a-fA-F-]{36})(?:/|$)", request.url.path)
    job_token = job_context.set(match.group(1) if match else "-")
    try:
        try:
            # Railway health checks remain available, while every interactive
            # route is protected when SCENECRAFT_ACCESS_TOKEN is configured.
            if settings.access_token and request.url.path not in {"/health", "/ready"} and not _access_token_is_valid(request):
                response = JSONResponse(
                    status_code=401,
                    content=_error_body("AUTH_REQUIRED", "This deployment requires its access token."),
                    headers={"WWW-Authenticate": 'Basic realm="SceneCraft Studio"'},
                )
            else:
                response = await call_next(request)
        except Exception as exc:
            response = await unexpected_error_handler(request, exc)
        response.headers["X-Correlation-ID"] = correlation_id
        route = request.scope.get("route")
        route_template = getattr(route, "path", "unknown")
        logger.info("request complete method=%s route=%s status=%s", request.method, route_template, response.status_code)
        return response
    finally:
        job_context.reset(job_token)
        correlation_context.reset(correlation_token)


@app.exception_handler(StarletteHTTPException)
async def http_error_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    if isinstance(exc.detail, dict) and "error" in exc.detail:
        body = {"detail": exc.detail}
    else:
        code = {404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED"}.get(exc.status_code, "HTTP_ERROR")
        body = _error_body(code, str(exc.detail) if exc.detail else "The request could not be completed.")
    return JSONResponse(status_code=exc.status_code, content=body)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    details = [{"location": list(error.get("loc", [])), "message": error.get("msg", "Invalid value"), "type": error.get("type", "value_error")} for error in exc.errors()]
    return JSONResponse(status_code=422, content=_error_body("VALIDATION_ERROR", "The request did not match the required format.", details=details))


@app.exception_handler(Exception)
async def unexpected_error_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.error("unhandled request exception error_type=%s", type(exc).__name__)
    return JSONResponse(status_code=500, content=_error_body("INTERNAL_ERROR", "An unexpected server error occurred.", retryable=True))


def _run(command: list[str], *, timeout: int = 120, cwd: Path | None = None, stage: str | None = None) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=True, cwd=cwd)
    except subprocess.CalledProcessError as exc:
        label = f" during {stage}" if stage else ""
        detail = (exc.stderr or "FFmpeg command failed").strip()
        raise RuntimeError(f"FFmpeg command failed{label}: {detail[-2000:]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"Media command timed out after {timeout} seconds.") from exc
    except FileNotFoundError as exc:
        executable = Path(command[0]).name if command else "media tool"
        raise RuntimeError(f"Required media executable is unavailable: {executable}.") from exc


def _require_nonempty_file(path: Path, stage: str) -> Path:
    path = path.resolve()
    try:
        if not path.is_file() or path.stat().st_size == 0:
            raise RuntimeError(f"FFmpeg stage '{stage}' completed without creating a non-empty file: {path.name}")
    except OSError as exc:
        raise RuntimeError(f"FFmpeg stage '{stage}' output could not be inspected: {path.name}") from exc
    return path


def _split_scenes(script: str) -> list[str]:
    return segment_script(script)


def _probe_audio(path: Path) -> tuple[float, float]:
    ffprobe = _ffprobe_path()
    ffmpeg = _ffmpeg_path()
    if not ffprobe or not ffmpeg:
        raise RuntimeError("FFmpeg and ffprobe are required to inspect and render media. Install FFmpeg and add it to PATH.")
    metadata = _run([ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(path)])
    try:
        duration = float(metadata.stdout.strip())
    except ValueError as exc:
        raise ValueError("The uploaded file could not be read as audio.") from exc
    if duration <= 0 or duration > 600:
        raise ValueError("Audio must be longer than zero and no more than 10 minutes.")
    try:
        stats = _run([ffmpeg, "-hide_banner", "-i", str(path), "-af", "volumedetect", "-f", "null", "-"], timeout=60)
    except RuntimeError as exc:
        message = str(exc)
        match = re.search(r"mean_volume:\s*(-?inf|-?\d+(?:\.\d+)?)\s*dB", message)
        if match and match.group(1) != "-inf" and float(match.group(1)) > -90:
            return duration, float(match.group(1))
        raise ValueError("The uploaded file is corrupt or contains no audible audio.") from exc
    match = re.search(r"mean_volume:\s*(-?inf|-?\d+(?:\.\d+)?)\s*dB", stats.stderr)
    mean_db = float(match.group(1)) if match and match.group(1) != "-inf" else float("-inf")
    if mean_db <= -90:
        raise ValueError("The uploaded audio is silent or too quiet to use.")
    return duration, mean_db


def _srt_timestamp(seconds: float) -> str:
    millis = round(seconds * 1000)
    hours, millis = divmod(millis, 3_600_000)
    minutes, millis = divmod(millis, 60_000)
    secs, millis = divmod(millis, 1000)
    return f"{hours:02}:{minutes:02}:{secs:02},{millis:03}"


def _caption_cues(text: str, start: float, end: float) -> list[dict[str, Any]]:
    chunks = caption_chunks(text)
    if not chunks:
        return []
    weights = [max(1, len(chunk.replace("\n", " ").split())) for chunk in chunks]
    total_weight = sum(weights)
    start_ms, end_ms = round(start * 1000), round(end * 1000)
    total_ms = max(len(chunks), end_ms - start_ms)
    output: list[dict[str, Any]] = []
    cursor = start_ms
    cumulative = 0
    for index, (chunk, weight) in enumerate(zip(chunks, weights)):
        cumulative += weight
        next_cursor = end_ms if index == len(chunks) - 1 else start_ms + round(total_ms * cumulative / total_weight)
        next_cursor = max(cursor + 1, min(next_cursor, end_ms - (len(chunks) - index - 1)))
        output.append({"start": cursor / 1000, "end": next_cursor / 1000, "text": chunk})
        cursor = next_cursor
    return output


def _retime_saved_scenes(scenes: list[dict[str, Any]]) -> None:
    """Keep editable scene timing contiguous until narration is regenerated."""
    cursor = 0.0
    for scene in scenes:
        duration = round(float(scene.get("duration", 0) or 0), 6)
        if duration <= 0:
            raise ValueError("Each scene duration must be greater than zero.")
        start, end = round(cursor, 6), round(cursor + duration, 6)
        scene.update(start=start, end=end, duration=round(end - start, 6))
        scene["caption_segments"] = _caption_cues(str(scene.get("caption_text", "")), start, end)
        cursor = end


def _split_scene_text(text: str, ratio: float) -> tuple[str, str]:
    """Prefer a sentence boundary near the requested timeline split."""
    text = text.strip()
    boundaries = [match.end() for match in re.finditer(r"(?<=[.!?])\s+", text)]
    if boundaries:
        target = len(text) * ratio
        cut = min(boundaries, key=lambda value: abs(value - target))
    else:
        words = list(re.finditer(r"\S+", text))
        if len(words) < 2:
            raise ValueError("A scene needs at least two words to split automatically. Edit the scene text first.")
        word_index = max(1, min(len(words) - 1, round(len(words) * ratio)))
        cut = words[word_index - 1].end()
    left, right = text[:cut].strip(), text[cut:].strip()
    if not left or not right:
        raise ValueError("Choose a split point that leaves narration on both scenes.")
    return left, right


def _mark_storyboard_changed(job: dict[str, Any], *, narration_changed: bool) -> None:
    if narration_changed and job.get("voice_input_type") != "user_upload":
        job["voice_generation"] = {
            "status": "stale",
            "provider": "elevenlabs" if settings.speech_provider == "elevenlabs" else MOCK_PROVIDER.name,
            "message": "Scene narration changed. Generate voice again to update the audio and timing.",
        }
    job["dirty"] = True


def _format_srt(cues: list[dict[str, Any]]) -> str:
    return "\n".join(f"{index}\n{_srt_timestamp(cue['start'])} --> {_srt_timestamp(cue['end'])}\n{cue['text']}\n" for index, cue in enumerate(cues, 1))


def _ffmpeg_supports_subtitles(ffmpeg: str) -> bool:
    """Return whether this FFmpeg build can render libass subtitles."""
    filters = _run([ffmpeg, "-hide_banner", "-filters"], timeout=30)
    return bool(re.search(r"^\s*\.\.\s+subtitles\s+V->V", filters.stdout, re.MULTILINE))


def _burn_captions_into_video(ffmpeg: str, source: Path, captions: Path, output: Path, height: int) -> None:
    """Render an SRT once into final video frames, retaining the existing audio."""
    source = source.resolve()
    captions = captions.resolve()
    output = output.resolve()
    if not _ffmpeg_supports_subtitles(ffmpeg):
        raise RuntimeError(
            "This FFmpeg build cannot burn captions because its subtitles/libass filter is unavailable. "
            "Install an FFmpeg build with --enable-libass, then rerender the final video."
        )
    if not captions.is_file() or captions.stat().st_size == 0:
        raise RuntimeError("Final captions are missing; the caption-burned MP4 cannot be created.")
    _require_nonempty_file(source, "caption input")
    font_size = max(22, round(height * 0.045))
    margin_v = max(28, round(height * 0.05))
    style = (
        f"FontName=Arial,FontSize={font_size},PrimaryColour=&H00FFFFFF,"
        "OutlineColour=&HAA000000,BorderStyle=1,Outline=2,Shadow=0,Alignment=2,"
        f"MarginV={margin_v}"
    )
    subtitle_filter = f"subtitles=filename='captions.srt':charenc=UTF-8:force_style='{style}'"
    _run(
        [
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(source),
            "-vf", subtitle_filter, "-map", "0:v:0", "-map", "0:a:0", "-c:v", "libx264",
            "-preset", "medium", "-pix_fmt", "yuv420p", "-c:a", "copy", "-movflags", "+faststart", str(output),
        ],
        cwd=captions.parent,
        stage="burn captions",
    )
    _require_nonempty_file(output, "burn captions")


def _merged_scene_captions_srt(scenes: list[dict[str, Any]]) -> str:
    """Build an SRT from the current saved scene text and absolute timeline."""
    cues: list[dict[str, Any]] = []
    seen: set[tuple[int, int, str]] = set()
    for scene in sorted(scenes, key=lambda item: (float(item.get("start", 0)), float(item.get("end", 0)))):
        text = str(scene.get("caption_text") or "").strip()
        start, end = float(scene.get("start", 0)), float(scene.get("end", 0))
        if not text or end <= start:
            continue
        for cue in _caption_cues(text, start, end):
            # Suppress only an accidental exact duplicate. Identical words at
            # distinct timestamps are legitimate captions and remain intact.
            key = (round(cue["start"] * 1000), round(cue["end"] * 1000), cue["text"])
            if key not in seen:
                seen.add(key)
                cues.append(cue)
    cues.sort(key=lambda cue: (cue["start"], cue["end"], cue["text"]))
    return _format_srt(cues) if cues else ""


def _probe_render(path: Path) -> dict[str, Any]:
    ffprobe = _ffprobe_path()
    if not ffprobe:
        raise RuntimeError("ffprobe is unavailable. Install FFmpeg (including ffprobe) and add it to PATH.")
    result = _run([ffprobe, "-v", "error", "-show_entries", "format=duration:stream=codec_type,codec_name,width,height,avg_frame_rate,duration", "-of", "json", str(path)])
    try:
        payload = json.loads(result.stdout)
        duration = float(payload["format"]["duration"])
        video = next(stream for stream in payload["streams"] if stream.get("codec_type") == "video")
        audio = next(stream for stream in payload["streams"] if stream.get("codec_type") == "audio")
    except (ValueError, KeyError, StopIteration, TypeError) as exc:
        raise RuntimeError("FFprobe could not verify the rendered video and audio streams.") from exc
    return {"duration": duration, "video": video, "audio": audio}


def _select_visual(scene_text: str, index: int, job_id: str) -> SceneVisual:
    """Run provider work with a bounded wait and at most one retry."""
    last_error: ProviderError | None = None
    for attempt in range(PROVIDER_MAX_ATTEMPTS):
        future = provider_executor.submit(FOOTAGE_PROVIDER.select_visual, scene_text, index, job_id)
        try:
            visual = future.result(timeout=PROVIDER_TIMEOUT_SECONDS)
        except FutureTimeout as exc:
            future.cancel()
            last_error = ProviderError(
                "PROVIDER_TIMEOUT",
                f"Footage provider did not respond within {PROVIDER_TIMEOUT_SECONDS:g} seconds.",
                retryable=True,
                details={"attempt": attempt + 1, "max_attempts": PROVIDER_MAX_ATTEMPTS},
            )
        except ProviderError as exc:
            last_error = exc
        except Exception as exc:
            # Do not expose arbitrary provider exception strings, which may
            # contain URLs, credentials, or private service responses.
            is_local_mock = FOOTAGE_PROVIDER is MOCK_PROVIDER
            last_error = ProviderError(
                "PROCESSING_FAILED" if is_local_mock else "PROVIDER_UNAVAILABLE",
                str(exc) if is_local_mock else "The footage provider could not complete the request.",
                retryable=True,
                details={"exception_type": type(exc).__name__, "attempt": attempt + 1},
            )
        else:
            if visual is None:
                raise ProviderError("PROVIDER_NO_MEDIA", "The footage provider returned no media for this scene.", retryable=False)
            if not isinstance(visual, SceneVisual):
                raise ProviderError("PROVIDER_INVALID_RESULT", "The footage provider returned an invalid media result.", retryable=False)
            if not visual.asset_id.strip() or not re.fullmatch(r"(?:0x[0-9a-fA-F]{6}|[A-Za-z]+)", visual.color):
                raise ProviderError("PROVIDER_INVALID_RESULT", "The footage provider returned invalid visual metadata.", retryable=False)
            return visual

        if not last_error.retryable or attempt + 1 == PROVIDER_MAX_ATTEMPTS:
            raise last_error
        time.sleep(min(0.05 * (attempt + 1), PROVIDER_TIMEOUT_SECONDS / 4))
    raise last_error or ProviderError("PROVIDER_UNAVAILABLE", "The footage provider could not complete the request.", retryable=True)


def _visual_options(scene_text: str, index: int, job_id: str, generation: int = 0, ratio: str = "16:9", exclude_asset_ids: set[str] | None = None) -> list[SceneVisual]:
    # Prefer ratio-aware alternatives, then generic alternatives. Older
    # providers that only implement select_visual remain supported below.
    ratio_method = getattr(FOOTAGE_PROVIDER, "visual_options_for_ratio", None)
    option_method = getattr(FOOTAGE_PROVIDER, "visual_options", None)
    if not callable(ratio_method) and not callable(option_method):
        return [_select_visual(scene_text, index, job_id)]
    last_error: ProviderError | None = None
    for attempt in range(PROVIDER_MAX_ATTEMPTS):
        started_at = time.monotonic()
        if callable(ratio_method):
            future = provider_executor.submit(ratio_method, scene_text, index, job_id, generation, ratio, exclude_asset_ids or set())
        else:
            future = provider_executor.submit(option_method, scene_text, index, job_id, generation)
        try:
            # The provider owns the actual HTTP timeout. Give it a small
            # scheduling grace to map a socket timeout into a safe,
            # structured ProviderError instead of racing it with this outer
            # watchdog. This does not lengthen the provider network timeout.
            completion_grace = min(0.25, PROVIDER_TIMEOUT_SECONDS)
            declared_budget = getattr(FOOTAGE_PROVIDER, "operation_timeout_seconds", None)
            operation_timeout = declared_budget if isinstance(declared_budget, (int, float)) and declared_budget >= PROVIDER_TIMEOUT_SECONDS else PROVIDER_TIMEOUT_SECONDS
            options = future.result(timeout=operation_timeout + completion_grace)
            break
        except FutureTimeout as exc:
            future.cancel()
            timeout_details = {
                "attempt": attempt + 1,
                "max_attempts": PROVIDER_MAX_ATTEMPTS,
                "timeout_seconds": operation_timeout,
                "stage": "alternatives_watchdog",
                "elapsed_ms": round((time.monotonic() - started_at) * 1000),
            }
            logger.warning(
                "footage alternatives provider error provider=%s stage=%s code=%s retryable=%s elapsed_ms=%s status=%s",
                getattr(FOOTAGE_PROVIDER, "name", "unknown"),
                timeout_details["stage"],
                "PROVIDER_TIMEOUT",
                True,
                timeout_details["elapsed_ms"],
                "-",
            )
            last_error = ProviderError("PROVIDER_TIMEOUT", "Footage alternatives did not respond before the provider timeout.", retryable=True, details=timeout_details)
        except ProviderError as exc:
            last_error = exc
            logger.warning(
                "footage alternatives provider error provider=%s stage=%s code=%s retryable=%s elapsed_ms=%s status=%s",
                getattr(FOOTAGE_PROVIDER, "name", "unknown"),
                _safe_provider_error_details(exc.details).get("stage", "alternatives_search"),
                exc.code,
                exc.retryable,
                _safe_provider_error_details(exc.details).get("elapsed_ms", "-"),
                _safe_provider_error_details(exc.details).get("status_code", "-"),
            )
        except Exception as exc:
            if FOOTAGE_PROVIDER is MOCK_PROVIDER:
                last_error = ProviderError("PROCESSING_FAILED", str(exc), retryable=True, details={"exception_type": type(exc).__name__})
            else:
                last_error = ProviderError("PROVIDER_UNAVAILABLE", "The footage provider could not return visual alternatives.", retryable=True, details={"exception_type": type(exc).__name__})
        if not last_error.retryable or last_error.code == "PROVIDER_RATE_LIMITED" or attempt + 1 == PROVIDER_MAX_ATTEMPTS:
            raise last_error
        time.sleep(min(0.05 * (attempt + 1), PROVIDER_TIMEOUT_SECONDS / 4))
    else:
        raise last_error or ProviderError("PROVIDER_UNAVAILABLE", "The footage provider could not return visual alternatives.", retryable=True)
    if not isinstance(options, list) or any(not isinstance(item, SceneVisual) for item in options):
        raise ProviderError("PROVIDER_INVALID_RESULT", "The footage provider returned invalid visual alternatives.", retryable=False)
    excluded = exclude_asset_ids or set()
    options = [item for item in options if item.asset_id not in excluded]
    if not options:
        raise ProviderError("PROVIDER_NO_MEDIA", "The provider returned no new visual alternatives for this scene.", retryable=False, details={"excluded_candidate_count": len(excluded)})
    asset_ids = [item.asset_id for item in options]
    if len(set(asset_ids)) != len(asset_ids):
        raise ProviderError("PROVIDER_INVALID_RESULT", "Visual alternative identifiers must be unique.", retryable=False)
    for visual in options:
        if not visual.asset_id.strip() or not re.fullmatch(r"(?:0x[0-9a-fA-F]{6}|[A-Za-z]+)", visual.color):
            raise ProviderError("PROVIDER_INVALID_RESULT", "The footage provider returned invalid visual metadata.", retryable=False)
        if visual.asset_path is not None and (not visual.license.strip() or not visual.attribution.strip() or (not visual.license_verified and visual.rights_basis not in {"user_attestation", "commons_file_metadata"}) or (not visual.source_url and visual.rights_basis != "user_attestation")):
            raise ProviderError("PROVIDER_UNVERIFIED_RIGHTS", "A visual option with unverified rights was rejected.", retryable=False)
        if visual.provider == "pexels" and (not visual.download_url or not visual.license_verified or not visual.source_url):
            raise ProviderError("PROVIDER_INVALID_RESULT", "The Pexels result is missing its verified rights or download reference.", retryable=False)
        if visual.provider == "wikimedia-commons" and (not visual.download_url or visual.rights_basis != "commons_file_metadata" or not visual.source_url):
            raise ProviderError("PROVIDER_INVALID_RESULT", "The Commons result is missing its license metadata or download reference.", retryable=False)
        if visual.provider == "wikimedia-commons-images" and (visual.media_type != "image" or not visual.download_url or visual.rights_basis != "commons_file_metadata" or not visual.source_url):
            raise ProviderError("PROVIDER_INVALID_RESULT", "The Commons still image is missing its license metadata or download reference.", retryable=False)
        if visual.asset_path is not None:
            if not visual.asset_path.is_file():
                raise ProviderError("PROVIDER_ASSET_MISSING", f"A provider visual file is missing: {visual.asset_id}", retryable=False)
            if visual.media_type == "image":
                _probe_image(visual.asset_path, visual.asset_id)
            else:
                _probe_footage(visual.asset_path, visual.asset_id)
    return options


def _synthesize_scene(text: str, destination: Path) -> dict[str, str | None]:
    if SPEECH_PROVIDER is None:
        if settings.speech_provider == "elevenlabs":
            raise ProviderError("TTS_NOT_CONFIGURED", "ElevenLabs narration is selected but not configured. Set ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID in the server environment or untracked .env file, then restart the server.", retryable=False)
        raise ProviderError("TTS_NOT_CONFIGURED", "No text-to-speech provider is configured.", retryable=False)
    last_error: ProviderError | None = None
    for attempt in range(SPEECH_MAX_ATTEMPTS):
        attempt_path = destination.with_name(f"{destination.stem}.attempt-{attempt}{destination.suffix}")
        future = provider_executor.submit(SPEECH_PROVIDER.synthesize, text, attempt_path)
        try:
            metadata = future.result(timeout=PROVIDER_TIMEOUT_SECONDS)
            if not isinstance(metadata, dict):
                raise ProviderError("PROVIDER_INVALID_RESULT", "The speech provider returned invalid metadata.", retryable=False)
            if not attempt_path.is_file() or attempt_path.stat().st_size == 0:
                raise ProviderError("PROVIDER_INVALID_RESULT", "The speech provider returned no audio file.", retryable=False)
            attempt_path.replace(destination)
            return metadata
        except FutureTimeout as exc:
            future.cancel()
            last_error = ProviderError("PROVIDER_TIMEOUT", f"Speech provider did not respond within {PROVIDER_TIMEOUT_SECONDS:g} seconds.", retryable=True, details={"attempt": attempt + 1, "max_attempts": SPEECH_MAX_ATTEMPTS})
        except ProviderError as exc:
            last_error = exc
        except Exception as exc:
            last_error = ProviderError("PROVIDER_UNAVAILABLE", "The speech provider could not complete the request.", retryable=True, details={"exception_type": type(exc).__name__, "attempt": attempt + 1})
        attempt_path.unlink(missing_ok=True)
        if not last_error.retryable or attempt + 1 == SPEECH_MAX_ATTEMPTS:
            raise last_error
        time.sleep(min(0.05 * (attempt + 1), PROVIDER_TIMEOUT_SECONDS / 4))
    raise last_error or ProviderError("PROVIDER_UNAVAILABLE", "The speech provider could not complete the request.", retryable=True)


def _voice_cache_paths(job_id: str, scene_texts: list[str]) -> tuple[Path, str]:
    provider = SPEECH_PROVIDER
    provider_identity = {
        "provider": getattr(provider, "name", "mock-local") if provider else "unconfigured-elevenlabs" if settings.speech_provider == "elevenlabs" else "mock-local",
        "version": getattr(provider, "version", MOCK_PROVIDER.version) if provider else MOCK_PROVIDER.version,
        "model": getattr(provider, "_model_id", None) if provider else None,
        "voice_id": getattr(provider, "_voice_id", None) if provider else None,
        "scenes": scene_texts,
    }
    digest = hashlib.sha256(json.dumps(provider_identity, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    job_root = (DATA_DIR / job_id).resolve()
    voice_dir = (job_root / "voice").resolve()
    if voice_dir.parent != job_root:
        raise RuntimeError("The narration cache path is outside the job directory.")
    return voice_dir, digest


def _prepare_speech_audio(job_id: str, scene_texts: list[str], scratch_dir: Path) -> dict[str, Any]:
    """Create or reuse one measured narration track, caching speech per scene text."""
    voice_dir, signature = _voice_cache_paths(job_id, scene_texts)
    voice_dir.mkdir(parents=True, exist_ok=True)
    output = voice_dir / f"narration-{signature}.wav"
    provider = SPEECH_PROVIDER
    if provider is None and settings.speech_provider == "elevenlabs":
        raise ProviderError("TTS_NOT_CONFIGURED", "ElevenLabs narration is selected but credentials are missing. Set ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID in the server environment or untracked .env file, then restart the server.", retryable=False)

    if provider is None:
        duration = MOCK_PROVIDER.narration_duration(" ".join(scene_texts))
        cached_output_valid = output.is_file() and output.stat().st_size > 0
        if cached_output_valid:
            try:
                _probe_audio(output)
            except RuntimeError:
                cached_output_valid = False
                output.unlink(missing_ok=True)
        if not cached_output_valid:
            ffmpeg = _ffmpeg_path()
            if not ffmpeg:
                raise RuntimeError("FFmpeg is unavailable; it is required to generate the labelled demo test tone.")
            exact_duration = math.ceil(duration * FRAME_RATE) / FRAME_RATE
            temporary = scratch_dir / "mock_narration.wav"
            _run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100", "-t", f"{exact_duration:.9f}", "-c:a", "pcm_s16le", str(temporary)])
            _probe_audio(temporary)
            os.replace(temporary, output)
        metadata = {"provider": MOCK_PROVIDER.name, "model": MOCK_PROVIDER.version, "voice_id": None, "mode": "demo_test_tone"}
        return {"path": output, "duration": _probe_audio(output)[0], "scene_durations": None, "metadata": metadata, "provider": MOCK_PROVIDER.speech_label, "signature": signature}

    ffmpeg = _ffmpeg_path()
    if not ffmpeg:
        raise RuntimeError("FFmpeg is unavailable; it is required to normalize generated narration.")
    provider_fingerprint = hashlib.sha256(json.dumps({
        "provider": getattr(provider, "name", "elevenlabs"),
        "version": getattr(provider, "version", "text-to-speech-v1"),
        "model": getattr(provider, "_model_id", None),
        "voice_id": getattr(provider, "_voice_id", None),
    }, sort_keys=True).encode("utf-8")).hexdigest()[:16]
    segment_wavs: list[Path] = []
    segment_durations: list[float] = []
    segment_metadata: list[dict[str, Any]] = []
    for index, text in enumerate(scene_texts):
        text_digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]
        # A scene's audio is reusable only when both its text and the configured
        # speech provider/voice/model match. The aggregate signature also
        # includes the complete scene list, but individual segments need this
        # narrower key so editing one scene can safely reuse the others.
        stem = f"scene-{index + 1:03}-{provider_fingerprint}-{text_digest}"
        mp3_path = voice_dir / f"{stem}.mp3"
        wav_path = voice_dir / f"{stem}.wav"
        metadata_path = voice_dir / f"{stem}.json"
        valid_mp3 = mp3_path.is_file() and mp3_path.stat().st_size > 0
        if valid_mp3:
            try:
                _probe_audio(mp3_path)
            except RuntimeError:
                mp3_path.unlink(missing_ok=True)
                wav_path.unlink(missing_ok=True)
                metadata_path.unlink(missing_ok=True)
                valid_mp3 = False
        if not valid_mp3:
            meta = _synthesize_scene(text, mp3_path)
            metadata_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        else:
            try:
                meta = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                meta = {"provider": getattr(provider, "name", "elevenlabs"), "model": getattr(provider, "_model_id", None), "voice_id": getattr(provider, "_voice_id", None)}
        cached_wav_valid = wav_path.is_file() and wav_path.stat().st_size > 0
        if cached_wav_valid:
            try:
                _probe_audio(wav_path)
            except RuntimeError:
                cached_wav_valid = False
                wav_path.unlink(missing_ok=True)
        if not cached_wav_valid:
            temporary_wav = scratch_dir / f"speech-{index + 1:03}.wav"
            _run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(mp3_path), "-vn", "-ac", "1", "-ar", "44100", "-c:a", "pcm_s16le", str(temporary_wav)])
            _probe_audio(temporary_wav)
            os.replace(temporary_wav, wav_path)
        segment_durations.append(_probe_audio(wav_path)[0])
        segment_wavs.append(wav_path)
        segment_metadata.append(meta if isinstance(meta, dict) else {})

    cached_output_valid = output.is_file() and output.stat().st_size > 0
    if cached_output_valid:
        try:
            _probe_audio(output)
        except RuntimeError:
            cached_output_valid = False
            output.unlink(missing_ok=True)
    if not cached_output_valid:
        scratch_segments: list[Path] = []
        for index, segment in enumerate(segment_wavs):
            scratch_segment = scratch_dir / f"concat-{index + 1:03}.wav"
            shutil.copyfile(segment, scratch_segment)
            scratch_segments.append(scratch_segment)
        concat_list = scratch_dir / "narration-segments.txt"
        concat_list.write_text("".join(f"file '{path.name}'\n" for path in scratch_segments), encoding="utf-8")
        temporary_output = scratch_dir / "narration.wav"
        _run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(concat_list), "-vn", "-ac", "1", "-ar", "44100", "-c:a", "pcm_s16le", str(temporary_output)])
        _probe_audio(temporary_output)
        os.replace(temporary_output, output)
    duration = _probe_audio(output)[0]
    provider_meta = {
        "name": getattr(provider, "name", "elevenlabs"),
        "version": getattr(provider, "version", "text-to-speech-v1"),
        "model": getattr(provider, "_model_id", None),
        "voice_id": getattr(provider, "_voice_id", None),
        "segments": segment_metadata,
    }
    return {"path": output, "duration": duration, "scene_durations": segment_durations, "metadata": provider_meta, "provider": getattr(provider, "name", "elevenlabs"), "signature": signature}


def _voice_result(job_id: str, prepared: dict[str, Any]) -> dict[str, Any]:
    metadata = prepared["metadata"]
    is_demo = str(prepared["provider"]).startswith("mock")
    voice_id = metadata.get("voice_id") if isinstance(metadata, dict) else None
    return {
        "status": "ready",
        "signature": prepared["signature"],
        "provider": prepared["provider"],
        "voice_id": voice_id,
        "mode": "demo_test_tone" if is_demo else "generated_speech",
        "label": "DEMO - 440 Hz test tone, not speech" if is_demo else "Generated speech narration",
        "duration_seconds": round(float(prepared["duration"]), 6),
        "timing_strategy": "measured per-scene audio duration" if prepared["scene_durations"] else "mock tone duration with narration-weighted scene timing",
        "audio_url": f"/api/v1/jobs/{job_id}/voice/audio?signature={prepared['signature']}",
    }


def _voice_status(job_id: str, job: dict[str, Any]) -> dict[str, Any]:
    if job.get("voice_input_type") == "user_upload":
        return {"status": "uploaded", "provider": "user-upload", "label": "Uploaded voice-over is used as supplied; speech is not transcribed."}
    texts = [scene.get("narration_text", "") for scene in job.get("scenes", [])]
    if not texts or any(not text for text in texts):
        return {"status": "not_ready", "provider": settings.speech_provider, "message": "Create a script storyboard before generating voice narration."}
    voice_dir, signature = _voice_cache_paths(job_id, texts)
    stored = job.get("voice_generation") or {}
    if stored.get("signature") == signature and stored.get("status") in {"queued", "running", "failed"}:
        return {key: value for key, value in stored.items() if key != "signature"} | {"signature": signature}
    expected = voice_dir / f"narration-{signature}.wav"
    if expected.is_file() and expected.stat().st_size > 0:
        try:
            duration = _probe_audio(expected)[0]
            if stored.get("signature") == signature and stored.get("status") == "ready":
                return dict(stored)
            provider = SPEECH_PROVIDER
            metadata: dict[str, Any] = ({"provider": getattr(provider, "name", "elevenlabs"), "model": getattr(provider, "_model_id", None), "voice_id": getattr(provider, "_voice_id", None)} if provider else {"provider": MOCK_PROVIDER.name, "model": MOCK_PROVIDER.version, "voice_id": None})
            return _voice_result(job_id, {"path": expected, "duration": duration, "scene_durations": [] if provider else None, "metadata": metadata, "provider": metadata["provider"], "signature": signature})
        except RuntimeError:
            expected.unlink(missing_ok=True)
    if stored.get("status") == "ready":
        return {"status": "stale", "provider": stored.get("provider"), "message": "Narration changed. Generate voice again to update the saved audio and scene timing."}
    if stored.get("status") == "stale":
        return {key: value for key, value in stored.items() if key != "signature"} | {"status": "stale"}
    return {"status": "not_generated", "provider": "elevenlabs" if settings.speech_provider == "elevenlabs" else MOCK_PROVIDER.name, "message": "Generate narration to create audio for the current saved scene text."}


def _generate_voice_task(job_id: str, signature: str) -> None:
    job_dir = DATA_DIR / job_id
    scratch_dir = job_dir / ".voice-rendering"
    scratch_dir.mkdir(parents=True, exist_ok=True)
    try:
        with jobs_lock:
            job = jobs.get(job_id)
            if not job:
                return
            texts = [scene.get("narration_text", "") for scene in job.get("scenes", [])]
            if _voice_cache_paths(job_id, texts)[1] != signature:
                job["voice_generation"] = {"status": "stale", "message": "Scene narration changed before voice generation began."}
                return
            job["voice_generation"] = {
                "status": "running",
                "progress": 5,
                "signature": signature,
                "provider": getattr(SPEECH_PROVIDER, "name", settings.speech_provider),
            }
        prepared = _prepare_speech_audio(job_id, texts, scratch_dir)
        result = _voice_result(job_id, prepared)
        if prepared["scene_durations"]:
            timings = allocate_scene_timings_by_duration(texts, prepared["scene_durations"])
        else:
            frames = max(1, math.ceil(prepared["duration"] * FRAME_RATE))
            timings = allocate_scene_timings(texts, frames)
        with jobs_lock:
            job = jobs.get(job_id)
            if not job:
                return
            current_texts = [scene.get("narration_text", "") for scene in job.get("scenes", [])]
            if _voice_cache_paths(job_id, current_texts)[1] != signature:
                job["voice_generation"] = {"status": "stale", "message": "Scene narration changed while voice generation was running. Generate narration again for the latest saved text."}
                return
            for scene, timing in zip(job.get("scenes", []), timings):
                scene.update(start=round(timing.start, 6), end=round(timing.end, 6), duration=round(timing.end - timing.start, 6))
            job["voice_generation"] = result
            job["dirty"] = True
    except ProviderError as exc:
        details = _safe_provider_error_details(exc.details)
        logger.warning(
            "speech provider request failed code=%s retryable=%s status_code=%s provider_error_code=%s message=%s",
            exc.code,
            exc.retryable,
            details.get("status_code", "-"),
            details.get("provider_error_code", "-"),
            str(exc),
            extra={"job_id": job_id},
        )
        with jobs_lock:
            if job_id in jobs:
                jobs[job_id]["voice_generation"] = {"status": "failed", "signature": signature, "provider": getattr(SPEECH_PROVIDER, "name", settings.speech_provider), "error": {"code": exc.code, "message": str(exc), "retryable": exc.retryable, "details": details}}
    except Exception as exc:
        logger.exception("voice generation failed")
        with jobs_lock:
            if job_id in jobs:
                jobs[job_id]["voice_generation"] = {"status": "failed", "signature": signature, "provider": getattr(SPEECH_PROVIDER, "name", settings.speech_provider), "error": {"code": "VOICE_GENERATION_FAILED", "message": "Narration audio could not be prepared. Check the provider response and FFmpeg installation, then retry.", "retryable": False, "details": {"exception_type": type(exc).__name__}}}
    finally:
        shutil.rmtree(scratch_dir, ignore_errors=True)


def _materialize_visual(visual: SceneVisual, job_id: str) -> SceneVisual:
    materialize = getattr(FOOTAGE_PROVIDER, "materialize", None)
    if not callable(materialize):
        raise ProviderError("PROVIDER_INVALID_RESULT", "The footage provider cannot download its selected video.", retryable=False)
    last_error: ProviderError | None = None
    for attempt in range(PROVIDER_MAX_ATTEMPTS):
        try:
            result = materialize(visual, job_id)
            if not isinstance(result, SceneVisual) or result.asset_path is None:
                raise ProviderError("PROVIDER_INVALID_RESULT", "The footage provider did not materialize a video file.", retryable=False)
            return result
        except ProviderError as exc:
            last_error = exc
        except Exception as exc:
            last_error = ProviderError("PROVIDER_DOWNLOAD_FAILED", "The selected footage could not be downloaded.", retryable=True, details={"exception_type": type(exc).__name__})
        if not last_error.retryable or last_error.code == "PROVIDER_RATE_LIMITED" or attempt + 1 == PROVIDER_MAX_ATTEMPTS:
            raise last_error
        time.sleep(min(0.05 * (attempt + 1), PROVIDER_TIMEOUT_SECONDS / 4))
    raise last_error or ProviderError("PROVIDER_DOWNLOAD_FAILED", "The selected footage could not be downloaded.", retryable=True)


def _probe_footage(path: Path, asset_id: str) -> None:
    ffprobe = _ffprobe_path()
    if not ffprobe:
        raise ProviderError("PROVIDER_UNAVAILABLE", "ffprobe is unavailable to validate selected footage.", retryable=True)
    try:
        result = _run([ffprobe, "-v", "error", "-show_entries", "format=duration:stream=codec_type", "-of", "json", str(path)], timeout=30)
        payload = json.loads(result.stdout)
        duration = float(payload["format"]["duration"])
        has_video = any(stream.get("codec_type") == "video" for stream in payload.get("streams", []))
    except (RuntimeError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ProviderError("PROVIDER_INVALID_ASSET", f"Selected footage is not a valid readable video: {asset_id}", retryable=False) from exc
    if not has_video or duration <= 0:
        raise ProviderError("PROVIDER_INVALID_ASSET", f"Selected footage contains no usable video: {asset_id}", retryable=False)


def _validate_scene_upload(path: Path, asset_id: str) -> None:
    """Apply stricter, bounded validation before retaining a scene upload."""
    _probe_footage(path, asset_id)
    ffprobe = _ffprobe_path()
    if not ffprobe:
        raise ProviderError("PROVIDER_UNAVAILABLE", "ffprobe is unavailable to validate selected footage.", retryable=True)
    try:
        result = _run([ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries", "format=duration:stream=width,height", "-of", "json", str(path)], timeout=30)
        payload = json.loads(result.stdout)
        duration = float(payload["format"]["duration"])
        stream = next(item for item in payload["streams"] if int(item.get("width", 0)) > 0 and int(item.get("height", 0)) > 0)
        width, height = int(stream["width"]), int(stream["height"])
    except (RuntimeError, ValueError, KeyError, TypeError, StopIteration, json.JSONDecodeError) as exc:
        raise ProviderError("PROVIDER_INVALID_ASSET", "The uploaded scene video could not be inspected.", retryable=False) from exc
    if duration > 600 or width > 7680 or height > 7680:
        raise ProviderError("PROVIDER_INVALID_ASSET", "The uploaded scene video exceeds the supported duration or dimensions.", retryable=False)


def _probe_image(path: Path, asset_id: str) -> None:
    ffprobe = _ffprobe_path()
    if not ffprobe:
        raise ProviderError("PROVIDER_UNAVAILABLE", "ffprobe is unavailable to validate the selected still image.", retryable=True)
    try:
        result = _run([ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_type,width,height", "-of", "json", str(path)], timeout=30)
        streams = json.loads(result.stdout).get("streams", [])
        image = next(stream for stream in streams if stream.get("codec_type") == "video")
        if int(image.get("width", 0)) <= 0 or int(image.get("height", 0)) <= 0:
            raise ValueError("invalid image dimensions")
    except (RuntimeError, ValueError, KeyError, TypeError, StopIteration, json.JSONDecodeError) as exc:
        raise ProviderError("PROVIDER_INVALID_ASSET", f"Selected still image is not a valid readable raster file: {asset_id}", retryable=False) from exc


def _srt_seconds(value: str) -> float:
    hours, minutes, rest = value.replace(",", ".").split(":")
    return int(hours) * 3600 + int(minutes) * 60 + float(rest)


def _verify_required_artifacts(job_id: str, job_dir: Path, ratio: str, expected_duration: float) -> None:
    names = {"video": f"video_{ratio.replace(':', 'x')}.mp4", "captions": "captions.srt", "timeline": "timeline.json", "manifest": "manifest.json"}
    for name in names.values():
        artifact = job_dir / name
        if not artifact.is_file() or artifact.stat().st_size == 0:
            raise RuntimeError(f"Required output artifact is missing or empty: {name}")
    try:
        timeline = json.loads((job_dir / names["timeline"]).read_text(encoding="utf-8"))
        manifest = json.loads((job_dir / names["manifest"]).read_text(encoding="utf-8"))
        captions = (job_dir / names["captions"]).read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("A required metadata artifact could not be parsed.") from exc
    scenes = timeline.get("scenes")
    if not isinstance(scenes, list) or not scenes or manifest.get("job_id") != job_id:
        raise RuntimeError("The timeline or manifest is incomplete or belongs to another job.")
    cursor = 0.0
    for scene in scenes:
        if not isinstance(scene, dict) or abs(float(scene.get("start", -1)) - cursor) > 0.00001:
            raise RuntimeError("The scene timeline contains a gap, overlap, or invalid start time.")
        end = float(scene.get("end", -1))
        if end <= cursor:
            raise RuntimeError("The scene timeline contains an empty or reversed scene.")
        cursor = end
    if abs(cursor - float(timeline.get("duration_seconds", -1))) > 0.00001 or abs(cursor - expected_duration) > 0.001:
        raise RuntimeError("The timeline duration does not agree with the rendered output.")
    cue_pattern = re.compile(r"^(\d{2}:\d{2}:\d{2},\d{3}) --> (\d{2}:\d{2}:\d{2},\d{3})$")
    cues: list[dict[str, Any]] = []
    for block in re.split(r"\r?\n\s*\r?\n", captions.strip()):
        lines = block.splitlines()
        if len(lines) < 3 or not lines[0].strip().isdigit():
            raise RuntimeError("Caption file contains a malformed cue.")
        match = cue_pattern.fullmatch(lines[1].strip())
        if not match:
            raise RuntimeError("Caption file contains an invalid cue time range.")
        cue_text = "\n".join(lines[2:])
        if any(len(line) > 42 for line in cue_text.splitlines()) or len(cue_text.splitlines()) > 2:
            raise RuntimeError("A caption cue exceeds the readable line length or line count.")
        cues.append({"start": _srt_seconds(match.group(1)), "end": _srt_seconds(match.group(2)), "text": cue_text})
    expected_cue_count = sum(len(scene.get("caption_segments", [])) for scene in scenes)
    if not cues or len(cues) != expected_cue_count:
        raise RuntimeError("Caption cue count does not match the scene timeline.")
    cue_index = 0
    for scene in scenes:
        segments = scene.get("caption_segments", [])
        if not segments:
            raise RuntimeError("A scene is missing its caption timing metadata.")
        actual_scene_text: list[str] = []
        for segment in segments:
            cue = cues[cue_index]
            cue_index += 1
            if abs(cue["start"] - float(segment["start"])) > 0.001 or abs(cue["end"] - float(segment["end"])) > 0.001:
                raise RuntimeError("Caption cue timing does not match its scene metadata.")
            if cue["start"] < float(scene["start"]) - 0.001 or cue["end"] > float(scene["end"]) + 0.001 or cue["end"] <= cue["start"]:
                raise RuntimeError("A caption cue extends outside its scene or has invalid timing.")
            actual_scene_text.append(cue["text"].replace("\n", " "))
        if " ".join(" ".join(actual_scene_text).split()) != " ".join(str(scene.get("caption_text", "")).split()):
            raise RuntimeError("Caption text does not match the narration scene captions.")
    measured = _probe_render(job_dir / names["video"])
    if abs(measured["duration"] - expected_duration) > 0.001:
        raise RuntimeError("The final video duration does not match its metadata.")
    if abs(float(manifest.get("timings", {}).get("rendered_duration_seconds", -1)) - expected_duration) > 0.001:
        raise RuntimeError("The manifest duration does not match the final video.")
    ratio_settings = manifest.get("settings", {})
    expected_size = RATIOS[ratio]
    if ratio_settings.get("aspect_ratio") != ratio or ratio_settings.get("resolution") != f"{expected_size[0]}x{expected_size[1]}":
        raise RuntimeError("The manifest aspect ratio or resolution does not match the rendered video.")
    if measured["video"].get("codec_name") != "h264" or measured["audio"].get("codec_name") != "aac":
        raise RuntimeError("The final video is missing a required H.264 or AAC stream.")


def _render(job_id: str, script: str, ratio: str, audio_path: Path | None, correlation_id: str, edited_scenes: list[dict[str, Any]] | None = None, draft_preview: bool = False, parent_job_id: str | None = None) -> None:
    job_dir = (DATA_DIR / job_id).resolve()
    render_dir = (job_dir / ".rendering").resolve()
    render_dir.mkdir(parents=True, exist_ok=True)
    correlation_token = correlation_context.set(correlation_id)
    job_token = job_context.set(job_id)
    started_at = time.monotonic()
    succeeded = False
    failure_error: dict[str, Any] | None = None
    uploaded_audio = audio_path is not None
    public_scenes: list[dict[str, Any]] = []
    visual_catalog: dict[str, list[SceneVisual]] = {}

    def _sync_parent_progress(prog: int, stat: str = "running") -> None:
        if parent_job_id and parent_job_id in jobs and "draft_preview" in jobs[parent_job_id]:
            jobs[parent_job_id]["draft_preview"]["status"] = stat
            jobs[parent_job_id]["draft_preview"]["progress"] = prog

    try:
        with jobs_lock:
            jobs[job_id].update(status="running", progress=5)
            _sync_parent_progress(5)
        logger.info("job started")
        ffmpeg = _ffmpeg_path()
        if not ffmpeg:
            raise RuntimeError("FFmpeg is unavailable. Install FFmpeg and add it to PATH, then retry the job.")
        if not _ffprobe_path():
            raise RuntimeError("ffprobe is unavailable. Install FFmpeg (including ffprobe) and add it to PATH.")

        if audio_path is not None:
            audio_path = audio_path.resolve()

        scenes = ([scene["narration_text"] for scene in edited_scenes] if edited_scenes is not None else (_split_scenes(script) if script else ["Transcript unavailable: uploaded voice-over"]))
        tts_scene_durations: list[float] | None = None
        speech_metadata: dict[str, str | None] | None = None
        if audio_path:
            try:
                duration, _ = _probe_audio(audio_path)
            except RuntimeError as exc:
                raise ValueError(f"The uploaded file could not be read as valid audio: {exc}") from exc
            voice_source = "user_upload"
            caption_texts = ([scene["caption_text"] for scene in edited_scenes] if edited_scenes is not None else ["Transcript unavailable: uploaded voice-over"])
            source_audio_duration = duration
        else:
            caption_texts = ([scene["caption_text"] for scene in edited_scenes] if edited_scenes is not None else scenes)
            owner_job_id = parent_job_id or job_id
            prepared = _prepare_speech_audio(owner_job_id, scenes, render_dir)
            audio_path = prepared["path"]
            duration = prepared["duration"]
            tts_scene_durations = prepared["scene_durations"]
            speech_metadata = prepared["metadata"]
            voice_source = prepared["provider"]
            source_audio_duration = duration
            voice_generation = _voice_result(owner_job_id, prepared)
            with jobs_lock:
                if owner_job_id in jobs:
                    jobs[owner_job_id]["voice_generation"] = voice_generation
            for index in range(len(scenes)):
                with jobs_lock:
                    prog = 5 + int(10 * (index + 1) / len(scenes))
                    jobs[job_id]["progress"] = prog
                    _sync_parent_progress(prog)
        total_frames = max(1, math.ceil(duration * FRAME_RATE))
        render_duration = total_frames / FRAME_RATE
        if not audio_path:
            raise RuntimeError("Narration audio was not prepared for rendering.")

        timings = allocate_scene_timings_by_duration(scenes, tts_scene_durations) if tts_scene_durations else allocate_scene_timings(scenes, total_frames)
        timeline: list[dict[str, Any]] = []
        caption_cues: list[dict[str, Any]] = []
        width, height = RATIOS[ratio]
        concat_file = render_dir / "scenes.txt"
        scene_paths: list[Path] = []
        selected_asset_ids: set[str] = set()
        unavailable_scenes: list[dict[str, Any]] = []
        for index, (scene_timing, caption_text) in enumerate(zip(timings, caption_texts)):
            scene_text = scene_timing.text
            start = scene_timing.start
            end = scene_timing.end
            scene_frames = scene_timing.end_frame - scene_timing.start_frame
            scene_path = render_dir / f"scene_{index:03}.mp4"
            scene_id = f"scene-{index + 1}"
            try:
                if edited_scenes is not None:
                    requested = edited_scenes[index]
                    options = jobs[job_id]["_visual_catalog"].get(requested["scene_id"], [])
                    if requested.get("visual_decision") != "accepted" and not draft_preview:
                        raise ValueError(f"Scene {index + 1} needs an accepted visual before rendering.")
                    visual = next((item for item in options if item.asset_id == requested.get("selected_visual_id")), None)
                    if visual is None:
                        raise ValueError(f"Scene {index + 1} visual choice is no longer available.")
                else:
                    options = _visual_options(scene_text, index, job_id, ratio=ratio, exclude_asset_ids=selected_asset_ids)
                    visual = options[0]
                if visual.selection_requires_review and edited_scenes is None:
                    visual_catalog[scene_id] = options
                    scene_analysis = analyze_scene(scene_text)
                    detail = {
                        "scene_id": scene_id, "index": index + 1, "narration_text": scene_text,
                        "code": "SCENE_RELEVANCE_REVIEW_REQUIRED",
                        "message": "Available source metadata does not establish a confident visual match.",
                        "retryable": False, "provider_details": {},
                        "recovery": "Review the alternatives, choose the closest accurate scene visual, and accept it. Import a better-matching rights-cleared local clip if none fit.",
                    }
                    unavailable_scenes.append(detail)
                    public_scenes.append({
                        "scene_id": scene_id, "narration_text": scene_text, "caption_text": caption_text,
                        "start": round(start, 6), "end": round(end, 6), "duration": round(end - start, 6),
                        "selected_visual_id": None, "selected_visual": None,
                        "visual_options": [_visual_details(item) for item in options],
                        "visual_decision": "rejected", "scene_analysis": scene_analysis, "error": detail,
                    })
                    with jobs_lock:
                        prog = 10 + int(45 * (index + 1) / len(scenes))
                        jobs[job_id]["progress"] = prog
                        _sync_parent_progress(prog)
                    continue
                if visual.download_url and visual.asset_path is None:
                    try:
                        visual = _materialize_visual(visual, job_id)
                        options = [visual if item.asset_id == visual.asset_id else item for item in options]
                    except ProviderError:
                        local_options = LOCAL_FOOTAGE_PROVIDER.visual_options_for_ratio(scene_text, index, job_id, 0, ratio, selected_asset_ids)
                        if visual.provider == "local-library" or not local_options:
                            raise
                        visual = local_options[0]
                        options = [visual] + [item for item in options if item.asset_id != visual.asset_id]
                        visual = SceneVisual(**{**visual.__dict__, "warnings": visual.warnings + ("A local library clip replaced a remote video that could not be downloaded or validated.",)})
                if visual.asset_id in selected_asset_ids:
                    alternate = next((item for item in options if item.asset_id not in selected_asset_ids), None)
                    if alternate is None:
                        local_options = LOCAL_FOOTAGE_PROVIDER.visual_options_for_ratio(scene_text, index, job_id, 0, ratio, selected_asset_ids)
                        alternate = local_options[0] if local_options else None
                    if alternate is None:
                        raise ProviderError("PROVIDER_NO_MEDIA", "All eligible video candidates were already used in an earlier scene.", retryable=False)
                    visual = alternate
                if visual.download_url and visual.asset_path is None:
                    visual = _materialize_visual(visual, job_id)
                    options = [visual if item.asset_id == visual.asset_id else item for item in options]
                if visual.asset_path is not None:
                    if not visual.asset_path.is_file():
                        raise ProviderError("PROVIDER_ASSET_MISSING", f"Selected footage asset is missing: {visual.asset_id}", retryable=False)
                    if not visual.license.strip() or not visual.attribution.strip() or (not visual.license_verified and visual.rights_basis not in {"user_attestation", "commons_file_metadata"}) or (not visual.source_url and visual.rights_basis != "user_attestation"):
                        raise ProviderError("PROVIDER_UNVERIFIED_RIGHTS", f"Selected footage asset has unverified rights and was rejected: {visual.asset_id}", retryable=False)
                    if visual.media_type == "image":
                        _probe_image(visual.asset_path, visual.asset_id)
                    else:
                        _probe_footage(visual.asset_path, visual.asset_id)
            except ProviderError as exc:
                recoverable = exc.code in {"PROVIDER_NO_MEDIA", "PROVIDER_RATE_LIMITED", "PROVIDER_UNAVAILABLE", "PROVIDER_TIMEOUT", "PROVIDER_DOWNLOAD_FAILED", "PROVIDER_INVALID_DOWNLOAD", "PROVIDER_DOWNLOAD_TOO_LARGE", "PROVIDER_INVALID_ASSET", "PROVIDER_ASSET_MISSING"}
                if not recoverable or edited_scenes is not None:
                    raise
                local_options = [] if FOOTAGE_PROVIDER is LOCAL_FOOTAGE_PROVIDER else LOCAL_FOOTAGE_PROVIDER.visual_options_for_ratio(scene_text, index, job_id, 0, ratio, selected_asset_ids)
                if local_options:
                    visual = SceneVisual(**{**local_options[0].__dict__, "warnings": local_options[0].warnings + (f"Local library fallback used after {exc.code} from the configured footage provider.",)})
                    options = [visual] + local_options[1:]
                else:
                    visual_catalog[scene_id] = []
                    scene_analysis = analyze_scene(scene_text)
                    detail = {"scene_id": scene_id, "index": index + 1, "narration_text": scene_text, "code": exc.code, "message": str(exc), "retryable": exc.retryable, "provider_details": _safe_provider_error_details(exc.details), "recovery": "Import a rights-cleared local clip with a descriptive filename, then choose Find alternatives for this scene."}
                    unavailable_scenes.append(detail)
                    public_scenes.append({"scene_id": scene_id, "narration_text": scene_text, "caption_text": caption_text, "start": round(start, 6), "end": round(end, 6), "duration": round(end - start, 6), "selected_visual_id": None, "selected_visual": None, "visual_options": [], "visual_decision": "rejected", "scene_analysis": scene_analysis, "error": detail})
                    with jobs_lock:
                        prog = 10 + int(45 * (index + 1) / len(scenes))
                        jobs[job_id]["progress"] = prog
                        _sync_parent_progress(prog)
                    continue
            visual_catalog[scene_id] = options
            selected_asset_ids.add(visual.asset_id)
            if visual.asset_path is not None:
                if visual.media_type == "image":
                    zoom = f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},zoompan=z='min(zoom+0.0004,1.04)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s={width}x{height}:fps={FRAME_RATE},trim=end_frame={scene_frames},setpts=PTS-STARTPTS"
                    _run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-loop", "1", "-framerate", str(FRAME_RATE), "-i", str(visual.asset_path.resolve()), "-vf", zoom, "-an", "-frames:v", str(scene_frames), "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-r", str(FRAME_RATE), str(scene_path)], stage=f"render scene {index + 1}")
                else:
                    _run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-stream_loop", "-1", "-i", str(visual.asset_path.resolve()), "-vf", f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},fps={FRAME_RATE},trim=end_frame={scene_frames},setpts=PTS-STARTPTS", "-an", "-frames:v", str(scene_frames), "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-r", str(FRAME_RATE), str(scene_path)], stage=f"render scene {index + 1}")
            else:
                motion = f"drawbox=x='mod(t*70+{index * 97},iw-160)':y='(ih-160)/2':w=160:h=160:color=white@0.08:t=fill"
                _run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", f"color=c={visual.color}:s={width}x{height}:r={FRAME_RATE}", "-vf", motion, "-an", "-frames:v", str(scene_frames), "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-r", str(FRAME_RATE), str(scene_path)], stage=f"render scene {index + 1}")
            _require_nonempty_file(scene_path, f"render scene {index + 1}")
            scene_paths.append(scene_path)
            warnings = list(visual.warnings)
            if voice_source == "user_upload":
                warnings.append("Uploaded audio was not transcribed; scene timing covers the audio duration only.")
            if visual.rights_basis == "user_attestation":
                warnings.append("Local clip rights are declared by the operator and were not independently verified by SceneCraft.")
            scene_cues = _caption_cues(caption_text, start, end)
            caption_cues.extend(scene_cues)
            scene_analysis = analyze_scene(scene_text)
            visual_decision = (edited_scenes[index].get("visual_decision", "suggested") if draft_preview and edited_scenes is not None else "accepted" if edited_scenes is not None else "suggested")
            timeline.append({"scene_id": scene_id, "index": index + 1, "narration_text": scene_text if voice_source != "user_upload" or edited_scenes is not None else None, "caption_text": caption_text, "caption_segments": scene_cues, "scene_analysis": scene_analysis, "start": round(start, 6), "end": round(end, 6), "start_frame": scene_timing.start_frame, "end_frame": scene_timing.end_frame, "clip": visual.asset_id, "provider": visual.provider, "media_type": visual.media_type, "source": visual.source, "license": visual.license, "license_verified": visual.license_verified, "rights_basis": visual.rights_basis, "attribution": visual.attribution, "source_url": visual.source_url, "ai_generated": visual.ai_generated, "selection_reason": visual.selection_reason, "relevance_score": visual.relevance_score, "keyword_overlap_score": visual.keyword_overlap_score, "visual_decision": visual_decision, "visual_options": [_visual_details(item) for item in options], "warnings": warnings})
            public_scenes.append({"scene_id": scene_id, "narration_text": scene_text, "caption_text": caption_text, "caption_segments": scene_cues, "scene_analysis": scene_analysis, "start": round(start, 6), "end": round(end, 6), "duration": round(end - start, 6), "selected_visual_id": visual.asset_id, "selected_visual": _visual_details(visual), "visual_options": [_visual_details(item) for item in options], "visual_decision": visual_decision})
            with jobs_lock:
                prog = 10 + int(45 * (index + 1) / len(scenes))
                jobs[job_id]["progress"] = prog
                _sync_parent_progress(prog)

        if unavailable_scenes:
            names = ", ".join(f"Scene {item['index']}" for item in unavailable_scenes)
            error_code = unavailable_scenes[0]["code"] if len(unavailable_scenes) == 1 else "SCENE_FOOTAGE_UNAVAILABLE"
            if len(unavailable_scenes) == 1:
                unresolved = unavailable_scenes[0]
                message = f"{unresolved['message']} Scene {unresolved['index']} remains unresolved. {unresolved['recovery']}"
                error_details = {**unresolved["provider_details"], "affected_scenes": unavailable_scenes}
            else:
                message = f"Required real footage is unavailable for {names}. Review the affected storyboard cards and import or select eligible clips before rendering."
                error_details = {"affected_scenes": unavailable_scenes}
            raise ProviderError(error_code, message, retryable=any(item["retryable"] for item in unavailable_scenes), details=error_details)

        concat_file.write_text("".join(f"file '{path.name}'\n" for path in scene_paths), encoding="utf-8")
        joined = render_dir / "joined.mp4"
        _run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(concat_file), "-c", "copy", str(joined)], cwd=render_dir, stage="join scenes")
        _require_nonempty_file(joined, "join scenes")
        output = render_dir / f"video_{ratio.replace(':', 'x')}.mp4"
        muxed_output = render_dir / "muxed.mp4"
        _run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(joined), "-i", str(audio_path), "-map", "0:v:0", "-map", "1:a:0", "-af", "apad", "-t", f"{render_duration:.9f}", "-c:v", "copy", "-c:a", "aac", "-movflags", "+faststart", str(muxed_output)], cwd=render_dir, stage="mux audio and video")
        _require_nonempty_file(muxed_output, "mux audio and video")
        mux_probe = _probe_render(muxed_output)
        actual_duration = mux_probe["duration"]
        try:
            rendered_rate = float(Fraction(mux_probe["video"]["avg_frame_rate"]))
        except (KeyError, ValueError, ZeroDivisionError) as exc:
            raise RuntimeError("FFprobe could not verify the rendered frame rate.") from exc
        if abs(rendered_rate - FRAME_RATE) > 0.001:
            raise RuntimeError(f"The rendered frame rate is {rendered_rate:g} fps; expected {FRAME_RATE} fps.")
        if abs(actual_duration - render_duration) > (1 / FRAME_RATE):
            raise RuntimeError("The rendered audio/video duration differs from the narration timeline by more than one frame.")
        # The muxer may pad an AAC packet by up to one audio frame. Extend only
        # the final scene so metadata and captions finish at the playable end.
        final_end = round(actual_duration, 6)
        if final_end < timeline[-1]["start"]:
            raise RuntimeError("The rendered output is shorter than its scene timeline.")
        timeline[-1]["end"] = final_end
        public_scenes[-1]["end"] = final_end
        public_scenes[-1]["duration"] = round(final_end - public_scenes[-1]["start"], 6)
        if caption_cues:
            caption_cues[-1]["end"] = actual_duration
            timeline[-1]["caption_segments"][-1]["end"] = final_end
            public_scenes[-1]["caption_segments"][-1]["end"] = final_end
        captions_path = render_dir / "captions.srt"
        captions_path.write_text(_format_srt(caption_cues), encoding="utf-8")
        # Draft MP4s retain a separate native preview track. Only the final
        # downloadable export is burned in, so the preview never paints the
        # same generated cue twice.
        if edited_scenes is not None and not draft_preview:
            _burn_captions_into_video(ffmpeg, muxed_output, captions_path, output, height)
        else:
            os.replace(muxed_output, output)
        probe = _probe_render(output)
        if probe["video"].get("codec_name") != "h264" or probe["audio"].get("codec_name") != "aac":
            raise RuntimeError("The rendered file did not contain the required H.264 video and AAC audio streams.")
        if (int(probe["video"].get("width", 0)), int(probe["video"].get("height", 0))) != (width, height):
            raise RuntimeError("The rendered video dimensions did not match the selected aspect ratio.")
        burned_duration = probe["duration"]
        try:
            burned_rate = float(Fraction(probe["video"]["avg_frame_rate"]))
        except (KeyError, ValueError, ZeroDivisionError) as exc:
            raise RuntimeError("FFprobe could not verify the caption-burned frame rate.") from exc
        if abs(burned_rate - FRAME_RATE) > 0.001:
            raise RuntimeError(f"Burning captions changed the final frame rate to {burned_rate:g} fps.")
        if abs(burned_duration - actual_duration) > 0.001:
            raise RuntimeError("Burning captions changed the final video duration.")
        (render_dir / "timeline.json").write_text(json.dumps({"duration_seconds": final_end, "frame_rate": FRAME_RATE, "scenes": timeline}, indent=2), encoding="utf-8")
        real_footage_used = any(scene["provider"] != MOCK_PROVIDER.name for scene in timeline)
        mock_footage_used = any(scene["provider"] == MOCK_PROVIDER.name for scene in timeline)
        user_declared_assets = any(scene["rights_basis"] == "user_attestation" for scene in timeline)
        manifest = {
            "job_id": job_id,
            "provider": {"speech": voice_source, "speech_details": speech_metadata or ({"name": MOCK_PROVIDER.name, "version": MOCK_PROVIDER.version} if voice_source.startswith("mock") else {"name": "user-upload", "version": None}), "footage": {"name": FOOTAGE_PROVIDER.name, "version": FOOTAGE_PROVIDER.version}},
            "settings": {"aspect_ratio": ratio, "resolution": f"{width}x{height}", "frame_rate": FRAME_RATE, "video_codec": "H.264", "audio_codec": "AAC", "captions": "burned_into_final_mp4" if edited_scenes is not None and not draft_preview else "external_preview_track", "seed": None, "selection": "Deterministic order; no random seed used"},
            "timings": {"source_audio_duration_seconds": round(source_audio_duration, 6), "rendered_duration_seconds": final_end, "av_duration_drift_seconds": round(final_end - source_audio_duration, 6), "processing_seconds": round(time.monotonic() - started_at, 3)},
            "cost": {"status": "not_measured_provider_usage_only" if speech_metadata else "unavailable_no_paid_provider", "amount": None, "currency": None},
            "warnings": (["Generated color visuals are mock graphics, not genuine footage or stock media."] if mock_footage_used else []) + (["Pexels API-ranked media is not independently relevance-scored; review third-party model, property, and trademark rights."] if any(scene["provider"] == "pexels" for scene in timeline) else []) + (["Wikimedia Commons search ranking is not an independent relevance score; review uploader licensing data and third-party rights."] if any(scene["provider"] == "wikimedia-commons" for scene in timeline) else []) + (["Local clip rights are operator-declared and were not independently verified."] if user_declared_assets else []) + (["Mock narration is a 440 Hz test tone, not speech or real TTS."] if voice_source.startswith("mock") else (["Uploaded audio was not transcribed; captions report transcript unavailability."] if voice_source == "user_upload" else [])),
            "rights": {"status": "user_declared_local_media_review_required" if user_declared_assets else "third_party_license_metadata_review_required" if real_footage_used else "generated_mock_only", "third_party_media_used": any(scene["provider"] not in {MOCK_PROVIDER.name, "local-library"} for scene in timeline), "unverified_assets_used": user_declared_assets, "policy": "User-supplied clip license and attribution fields are an operator declaration and have not been independently verified." if user_declared_assets else "Commons/Pexels file metadata is preserved, but source license claims and model, property, trademark, and other third-party rights must be reviewed." if real_footage_used else "Generated mock visuals only; no third-party media used."},
            "assets": [{"asset_id": scene["clip"], "media_type": scene["media_type"], "source": scene["source"], "source_url": scene["source_url"], "license": scene["license"], "license_verified": scene["license_verified"], "rights_basis": scene["rights_basis"], "attribution": scene["attribution"], "ai_generated": scene["ai_generated"], "selection_reason": scene["selection_reason"], "relevance_score": scene["relevance_score"], "keyword_overlap_score": scene["keyword_overlap_score"]} for scene in timeline],
            "warnings_detail": [warning for scene in timeline for warning in scene["warnings"]],
            "measured_output": {"duration_seconds": burned_duration, "video": probe["video"], "audio": probe["audio"]},
        }
        (render_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        _verify_required_artifacts(job_id, render_dir, ratio, final_end)
        artifact_names = [output.name, "captions.srt", "timeline.json", "manifest.json"]
        for artifact_name in artifact_names:
            os.replace(render_dir / artifact_name, job_dir / artifact_name)
        with jobs_lock:
            result_payload = {"video": f"/api/v1/jobs/{job_id}/artifacts/{output.name}", "captions": f"/api/v1/jobs/{job_id}/artifacts/captions.srt", "timeline": f"/api/v1/jobs/{job_id}/artifacts/timeline.json", "manifest": f"/api/v1/jobs/{job_id}/artifacts/manifest.json"}
            jobs[job_id].update(status="succeeded", progress=100, render_kind="draft" if draft_preview or edited_scenes is None else "final", scenes=public_scenes, _visual_catalog=visual_catalog, _ratio=ratio, dirty=False, result=result_payload)
            if parent_job_id and parent_job_id in jobs and "draft_preview" in jobs[parent_job_id]:
                jobs[parent_job_id]["draft_preview"].update({
                    "status": "succeeded",
                    "progress": 100,
                    "result": result_payload,
                    "error": None,
                })
        succeeded = True
        logger.info("job succeeded")
    except BaseException as exc:
        with jobs_lock:
            if job_id in jobs:
                if isinstance(exc, ProviderError):
                    failure_error = {"code": exc.code, "message": str(exc), "retryable": exc.retryable, "details": exc.details}
                else:
                    error_code = "INVALID_AUDIO" if isinstance(exc, ValueError) and uploaded_audio else "PROCESSING_FAILED"
                    message = str(exc) if isinstance(exc, Exception) else "The background render was interrupted."
                    failure_error = {"code": error_code, "message": message or "The background render failed.", "retryable": False, "details": None}
                if public_scenes and not draft_preview:
                    jobs[job_id].update(scenes=public_scenes, _visual_catalog=visual_catalog, _ratio=ratio, dirty=True)
        logger.error("job failed error_type=%s", type(exc).__name__)
    finally:
        shutil.rmtree(render_dir, ignore_errors=True)
        if not succeeded and edited_scenes is None and not draft_preview:
            # This initial attempt owns its generated narration cache. Edited
            # rerenders and child draft previews can reuse a valid parent cache.
            shutil.rmtree(job_dir / "voice", ignore_errors=True)
        if not succeeded and not edited_scenes and not draft_preview:
            if not public_scenes:
                shutil.rmtree(job_dir / ".provider-assets", ignore_errors=True)
                for original_audio in job_dir.glob("source_audio.*"):
                    original_audio.unlink(missing_ok=True)
        if not succeeded:
            if edited_scenes is None or draft_preview:
                for name in ("video_16x9.mp4", "video_9x16.mp4", "video_1x1.mp4", "captions.srt", "timeline.json", "manifest.json"):
                    (job_dir / name).unlink(missing_ok=True)
            # Empty scratch subdirectories are safe to remove, but preserve any
            # non-empty assets belonging to this failed job for scene review.
            for directory, _, _ in os.walk(job_dir, topdown=False, followlinks=False):
                path = Path(directory)
                if path != job_dir:
                    try:
                        path.rmdir()
                    except OSError:
                        pass
        if not succeeded and failure_error is not None:
            # Do not expose a terminal status until cleanup has completed.
            # Callers use it as the signal that the job directory is stable.
            with jobs_lock:
                if job_id in jobs:
                    jobs[job_id].update(status="failed", progress=100, error=failure_error, result=None)
                    if parent_job_id and parent_job_id in jobs and "draft_preview" in jobs[parent_job_id]:
                        jobs[parent_job_id]["draft_preview"].update({
                            "status": "failed",
                            "progress": 100,
                            "error": failure_error,
                            "result": None,
                        })
        job_context.reset(job_token)
        correlation_context.reset(correlation_token)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/ready")
def ready() -> JSONResponse:
    ffmpeg_ready = _ffmpeg_path() is not None
    ffprobe_ready = _ffprobe_path() is not None
    writable_dir = DATA_DIR if DATA_DIR.exists() else next((parent for parent in DATA_DIR.parents if parent.exists()), Path.cwd())
    storage_ready = writable_dir.is_dir() and os.access(writable_dir, os.W_OK)
    checks = {"ffmpeg": ffmpeg_ready, "ffprobe": ffprobe_ready, "job_storage": storage_ready, "provider_configuration": True}
    if all(checks.values()):
        return JSONResponse(status_code=200, content={"status": "ready", "checks": checks})
    return JSONResponse(status_code=503, content=_error_body("NOT_READY", "One or more required services are unavailable.", retryable=True, details=checks))


@app.get("/api/v1/providers")
def provider_information() -> dict[str, Any]:
    footage = FOOTAGE_PROVIDER.name
    speech = "user-upload" if settings.speech_provider == "elevenlabs" else "mock-sine-test-tone"
    if settings.speech_provider == "elevenlabs":
        speech = SPEECH_PROVIDER.name if SPEECH_PROVIDER else "elevenlabs-unconfigured"
    mock_footage = footage == "mock-local"
    footage_configured = not footage.endswith("-unconfigured")
    mock_speech = speech == "mock-sine-test-tone"
    speech_configured = settings.speech_provider == "mock" or SPEECH_PROVIDER is not None
    mode = "unconfigured" if not footage_configured or not speech_configured else "demo" if mock_footage and mock_speech else "real" if not mock_footage and not mock_speech else "mixed"
    links = {}
    if "pexels" in footage:
        links["pexels"] = "https://www.pexels.com/"
    if "wikimedia-commons" in footage:
        links["wikimedia_commons"] = "https://commons.wikimedia.org/"
    return {
        "mode": mode,
        "footage_provider": footage,
        "footage_configured": footage_configured,
        "speech_provider": speech,
        "speech_configured": speech_configured,
        "speech_voice_status": "DEMO - generated 440 Hz test tone, not speech" if speech == "mock-sine-test-tone" else "ElevenLabs configured" if SPEECH_PROVIDER else "ElevenLabs is selected but needs ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID",
        "external_processing": "pexels" in footage or "wikimedia-commons" in footage or speech == "elevenlabs",
        "disclosure": "Demo mode uses generated color cards and a 440 Hz test tone; it is not stock footage or spoken narration." if mode == "demo" else "Wikimedia Commons footage is selected but needs WIKIMEDIA_CONTACT (a public contact URL or email for the API User-Agent) configured on the server. No still images or demo graphics will be substituted." if footage == "wikimedia-commons-unconfigured" else "Pexels footage is selected but needs PEXELS_API_KEY configured on the server. No mock footage will be substituted." if not footage_configured else "ElevenLabs narration needs ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID configured on the server." if not speech_configured else "When configured, Commons/Pexels receive scene search phrases and ElevenLabs receives narration text. Local imports remain on this server. Review each clip's license and third-party rights before use.",
        "configuration": {"footage": "BROLL_PROVIDER=wikimedia and WIKIMEDIA_CONTACT (no API key); BROLL_PROVIDER=local uses imported clips; BROLL_PROVIDER=pexels requires PEXELS_API_KEY", "speech": "TTS_PROVIDER=elevenlabs, ELEVENLABS_API_KEY, and ELEVENLABS_VOICE_ID"},
        "provider_links": links,
    }


@app.post("/api/v1/footage/imports", status_code=201)
async def import_local_footage(
    video: UploadFile = File(...),
    license: str = Form(...),
    attribution: str = Form(...),
    rights_confirmed: bool = Form(...),
    source_url: str = Form(default=""),
) -> dict[str, Any]:
    """Import a local clip with an explicit operator rights declaration."""
    original_name = Path(video.filename or "").name
    suffix = Path(original_name).suffix.lower()
    if suffix not in ALLOWED_FOOTAGE_EXTENSIONS:
        raise _error("INVALID_FOOTAGE_TYPE", "Import an MP4, MOV, M4V, WebM, OGV, or MKV video file.", 422)
    license = license.strip()
    attribution = attribution.strip()
    source_url = source_url.strip()
    if len(license) < 2 or len(license) > 300 or len(attribution) < 2 or len(attribution) > 500:
        raise _error("INVALID_FOOTAGE_METADATA", "Provide the clip's license and attribution (up to 300 and 500 characters).", 422)
    if not rights_confirmed:
        raise _error("FOOTAGE_RIGHTS_NOT_CONFIRMED", "Confirm that you have permission to use this clip and comply with its license before importing.", 422)
    if source_url and (urlparse(source_url).scheme != "https" or not urlparse(source_url).hostname):
        raise _error("INVALID_FOOTAGE_SOURCE", "Source URL must be an HTTPS URL.", 422)
    content = await video.read(settings.max_footage_bytes + 1)
    if len(content) < 1024 or len(content) > settings.max_footage_bytes:
        raise _error("INVALID_FOOTAGE_SIZE", f"Footage file must be at least 1 KiB and no larger than {settings.max_footage_bytes // (1024 * 1024)} MiB.", 422)
    safe_stem = re.sub(r"[^A-Za-z0-9_-]+", "-", Path(original_name).stem).strip("-_")[:60] or "clip"
    LOCAL_FOOTAGE_PROVIDER.library_dir.mkdir(parents=True, exist_ok=True)
    target = LOCAL_FOOTAGE_PROVIDER.library_dir / f"{uuid.uuid4().hex[:10]}-{safe_stem}{suffix}"
    temporary = target.with_name(f".{target.stem}.upload{target.suffix}")
    sidecar = LOCAL_FOOTAGE_PROVIDER.sidecar_path(target)
    try:
        temporary.write_bytes(content)
        try:
            _probe_footage(temporary, temporary.name)
        except ProviderError as exc:
            raise _error("INVALID_FOOTAGE_FILE", "The uploaded file could not be validated as readable video by ffprobe.", 422, {"provider_code": exc.code}) from exc
        temporary.replace(target)
        metadata = {"license": license, "attribution": attribution, "source_url": source_url or None, "rights_confirmed": True, "rights_basis": "user_attestation"}
        sidecar.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    except HTTPException:
        raise
    except OSError as exc:
        target.unlink(missing_ok=True)
        sidecar.unlink(missing_ok=True)
        raise _error("FOOTAGE_IMPORT_FAILED", "The clip could not be saved into the local footage library.", 500, {"exception_type": type(exc).__name__}) from exc
    finally:
        temporary.unlink(missing_ok=True)
    asset_id = "local-" + hashlib.sha256(target.name.encode()).hexdigest()[:12]
    return {"asset_id": asset_id, "filename": target.name, "provider": LOCAL_FOOTAGE_PROVIDER.name, "license": license, "attribution": attribution, "source_url": source_url or None, "rights_basis": "user_attestation", "message": "Imported locally. The rights declaration is your responsibility and was not independently verified by SceneCraft."}


@app.get("/")
def home() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.post("/api/v1/broll/jobs", status_code=202)
async def create_job(
    script: str = Form(default=""),
    aspect_ratio: str = Form(default="16:9"),
    audio: UploadFile | None = File(default=None),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, str]:
    script = script.strip()
    if len(script) > MAX_SCRIPT_CHARACTERS:
        raise _error("SCRIPT_TOO_LONG", f"Script must be no longer than {MAX_SCRIPT_CHARACTERS} characters.", 422, {"max_characters": MAX_SCRIPT_CHARACTERS})
    if not script and audio is None:
        raise _error("INPUT_REQUIRED", "Enter a script or upload an audio file.", 422)
    if script and audio is not None:
        raise _error("INPUT_CONFLICT", "Provide either a script or an audio file, not both.", 422)
    if aspect_ratio not in RATIOS:
        raise _error("INVALID_ASPECT_RATIO", "Choose 16:9, 9:16, or 1:1.", 422)
    audio_path: Path | None = None
    if audio is not None:
        suffix = Path(audio.filename or "").suffix.lower()
        if suffix not in ALLOWED_EXTENSIONS:
            raise _error("INVALID_AUDIO_TYPE", "Upload an MP3, WAV, or M4A audio file.", 422)
        contents = await audio.read(MAX_UPLOAD_BYTES + 1)
        if not contents or len(contents) > MAX_UPLOAD_BYTES:
            max_megabytes = MAX_UPLOAD_BYTES / (1024 * 1024)
            raise _error("INVALID_AUDIO_SIZE", f"Audio must be non-empty and no larger than {max_megabytes:g} MB.", 422)
    else:
        contents = b""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    audio_extension = Path(audio.filename or "").suffix.lower() if audio is not None else ""
    fingerprint = hashlib.sha256(json.dumps({"script": script, "aspect_ratio": aspect_ratio, "audio_sha256": hashlib.sha256(contents).hexdigest() if audio is not None else None, "audio_extension": audio_extension}, sort_keys=True).encode("utf-8")).hexdigest()
    if idempotency_key is not None and (not idempotency_key.strip() or len(idempotency_key) > 128):
        raise _error("INVALID_IDEMPOTENCY_KEY", "Idempotency-Key must contain 1 to 128 characters.", 422)
    if idempotency_key is not None:
        idempotency_key = idempotency_key.strip()
    if idempotency_key is not None:
        with jobs_lock:
            previous = idempotency_records.get(idempotency_key)
            if previous:
                if previous[0] != fingerprint:
                    raise _error("IDEMPOTENCY_KEY_REUSED", "This Idempotency-Key was already used with a different request.", 409)
                return dict(previous[1])

    job_id = str(uuid.uuid4())
    job_dir = DATA_DIR / job_id
    job_dir.mkdir()
    if audio is not None:
        audio_path = job_dir / f"source_audio{audio_extension}"
        audio_path.write_bytes(contents)
    response_body = {"job_id": job_id, "status": "queued", "status_url": f"/api/v1/jobs/{job_id}"}
    with jobs_lock:
        if idempotency_key is not None:
            previous = idempotency_records.get(idempotency_key)
            if previous:
                shutil.rmtree(job_dir, ignore_errors=True)
                if previous[0] != fingerprint:
                    raise _error("IDEMPOTENCY_KEY_REUSED", "This Idempotency-Key was already used with a different request.", 409)
                return dict(previous[1])
        jobs[job_id] = {"job_id": job_id, "status": "queued", "progress": 0, "error": None, "result": None, "voice_input_type": "user_upload" if audio_path else "script", "created_at": datetime.now(timezone.utc).isoformat(), "_script": script, "_source_audio_path": str(audio_path) if audio_path else None, "_ratio": aspect_ratio}
        if idempotency_key is not None:
            idempotency_records[idempotency_key] = (fingerprint, response_body)
    correlation_id = correlation_context.get()
    executor.submit(_render, job_id, script, aspect_ratio, audio_path, correlation_id)
    logger.info("job accepted", extra={"job_id": job_id})
    return response_body


@app.get("/api/v1/jobs/{job_id}")
def get_job(job_id: str) -> dict[str, Any]:
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            raise _error("JOB_NOT_FOUND", "No job exists with that ID.", 404)
        if "draft_preview" in job and isinstance(job["draft_preview"], dict):
            preview_id = job["draft_preview"].get("job_id")
            preview_job = jobs.get(preview_id) if preview_id else None
            if preview_job:
                job["draft_preview"].update({
                    "status": preview_job.get("status"),
                    "progress": preview_job.get("progress", 0),
                    "result": preview_job.get("result"),
                    "error": preview_job.get("error"),
                })
        return {key: value for key, value in job.items() if not key.startswith("_")}


@app.post("/api/v1/jobs/{job_id}/voice")
def generate_job_voice(job_id: str) -> JSONResponse:
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            raise _error("JOB_NOT_FOUND", "No job exists with that ID.", 404)
        if job["status"] in {"queued", "running"}:
            raise _error("JOB_BUSY", "Wait for the current video job to finish before preparing voice audio.", 409)
        preview = job.get("draft_preview")
        preview_job = jobs.get(preview.get("job_id")) if isinstance(preview, dict) else None
        if preview_job and preview_job.get("status") in {"queued", "running"}:
            raise _error("JOB_BUSY", "Wait for the current draft preview to finish before preparing voice audio.", 409)
        if job.get("voice_input_type") == "user_upload":
            return JSONResponse(status_code=200, content=_voice_status(job_id, job))
        scenes = job.get("scenes") or []
        if not scenes:
            raise _error("JOB_NOT_EDITABLE", "A completed script storyboard is required before generating narration.", 409)
        if settings.speech_provider == "elevenlabs" and SPEECH_PROVIDER is None:
            raise _error("TTS_NOT_CONFIGURED", "ElevenLabs is selected but not configured. Set ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID in the server environment or untracked .env file, then restart the server.", 503, {"required_environment": ["ELEVENLABS_API_KEY", "ELEVENLABS_VOICE_ID"], "retryable": False})
        existing = _voice_status(job_id, job)
        if existing.get("status") == "ready":
            return JSONResponse(status_code=200, content=existing)
        if existing.get("status") in {"queued", "running"}:
            return JSONResponse(status_code=202, content={"job_id": job_id, **existing, "status_url": f"/api/v1/jobs/{job_id}/voice"})
        texts = [scene["narration_text"] for scene in scenes]
        signature = _voice_cache_paths(job_id, texts)[1]
        queued = {"status": "queued", "progress": 0, "signature": signature, "provider": "elevenlabs" if SPEECH_PROVIDER else MOCK_PROVIDER.name}
        job["voice_generation"] = queued
    executor.submit(_generate_voice_task, job_id, signature)
    return JSONResponse(status_code=202, content={"job_id": job_id, **queued, "status_url": f"/api/v1/jobs/{job_id}/voice"})


@app.get("/api/v1/jobs/{job_id}/voice")
def get_job_voice(job_id: str) -> dict[str, Any]:
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            raise _error("JOB_NOT_FOUND", "No job exists with that ID.", 404)
        return _voice_status(job_id, job)


@app.get("/api/v1/jobs/{job_id}/voice/audio")
def get_job_voice_audio(job_id: str, signature: str = "") -> FileResponse:
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            raise _error("JOB_NOT_FOUND", "No job exists with that ID.", 404)
        if job.get("voice_input_type") == "user_upload":
            raise _error("VOICE_AUDIO_UNAVAILABLE", "This job uses uploaded voice-over audio, which is not exposed as a generated voice artifact.", 404)
        texts = [scene.get("narration_text", "") for scene in job.get("scenes", [])]
        if not texts:
            raise _error("VOICE_AUDIO_UNAVAILABLE", "Narration audio is not ready.", 404)
        voice_dir, expected_signature = _voice_cache_paths(job_id, texts)
        if not signature or signature != expected_signature:
            raise _error("VOICE_AUDIO_STALE", "Narration changed. Generate voice again before playing this audio.", 404)
        audio_path = (voice_dir / f"narration-{expected_signature}.wav").resolve()
        if audio_path.parent != voice_dir or not audio_path.is_file() or audio_path.stat().st_size == 0:
            raise _error("VOICE_AUDIO_UNAVAILABLE", "Narration audio is not ready. Generate voice again.", 404)
    return FileResponse(audio_path, media_type="audio/wav", filename="scenecraft-narration.wav")


@app.patch("/api/v1/jobs/{job_id}/scenes/{scene_id}")
def edit_scene(job_id: str, scene_id: str, changes: SceneEditRequest = Body(...)) -> dict[str, Any]:
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            raise _error("JOB_NOT_FOUND", "No job exists with that ID.", 404)
        if job["status"] not in {"succeeded", "failed"} or not job.get("scenes"):
            raise _error("JOB_NOT_EDITABLE", "Scenes can be edited after a draft render has completed.", 409)
        scene = next((item for item in job["scenes"] if item["scene_id"] == scene_id), None)
        if scene is None:
            raise _error("SCENE_NOT_FOUND", "No scene exists with that ID in this job.", 404)
        values = changes.model_dump(exclude_unset=True)
        if not values:
            raise _error("EMPTY_SCENE_EDIT", "Provide at least one scene field to change.", 422)
        for field in ("narration_text", "caption_text"):
            if field in values:
                value = values[field]
                if value is None or not value.strip():
                    raise _error("INVALID_SCENE_TEXT", f"{field} cannot be empty.", 422)
        decision = values.get("visual_decision")
        if decision is not None and decision not in {"accepted", "rejected", "suggested"}:
            raise _error("INVALID_VISUAL_DECISION", "Visual decision must be accepted, pending review (suggested), or rejected.", 422)
        selected = None
        if "visual_id" in values:
            visual_id = values["visual_id"]
            if visual_id is None and decision != "rejected":
                raise _error("VISUAL_NOT_AVAILABLE", "Choose a visual option belonging to this scene.", 422)
            selected = next((candidate for candidate in scene["visual_options"] if candidate["id"] == visual_id), None) if visual_id is not None else None
            if visual_id is not None and selected is None:
                raise _error("VISUAL_NOT_AVAILABLE", "Choose a visual option belonging to this scene.", 422)
        if "narration_text" in values:
            scene["narration_text"] = values["narration_text"].strip()
        if "caption_text" in values:
            scene["caption_text"] = values["caption_text"].strip()
        if selected is not None:
            scene["selected_visual_id"] = selected["id"]
            scene["selected_visual"] = selected
            scene["visual_decision"] = decision or "accepted"
        if decision is not None:
            scene["visual_decision"] = decision
            if decision == "rejected":
                scene["selected_visual_id"] = None
                scene["selected_visual"] = None
        _retime_saved_scenes(job["scenes"])
        _mark_storyboard_changed(job, narration_changed="narration_text" in values)
        return {"scene": dict(scene), "dirty": True, "render_required": True}


def _editable_job(job_id: str) -> dict[str, Any]:
    job = jobs.get(job_id)
    if not job:
        raise _error("JOB_NOT_FOUND", "No job exists with that ID.", 404)
    if job["status"] not in {"succeeded", "failed"} or not job.get("scenes"):
        raise _error("JOB_NOT_EDITABLE", "Scenes can be edited after a draft render has completed.", 409)
    return job


@app.post("/api/v1/jobs/{job_id}/scenes", status_code=201)
def add_scene(job_id: str, request: SceneCreateRequest) -> dict[str, Any]:
    narration = request.narration_text.strip()
    caption = (request.caption_text if request.caption_text is not None else narration).strip()
    if not narration or not caption:
        raise _error("INVALID_SCENE_TEXT", "Narration and caption text cannot be empty.", 422)
    with jobs_lock:
        job = _editable_job(job_id)
        scenes = job["scenes"]
        position = len(scenes) if request.insert_at is None else request.insert_at
        if position > len(scenes):
            raise _error("INVALID_SCENE_POSITION", "Choose an insertion position within the current storyboard.", 422)
        scene_id = f"scene-{uuid.uuid4().hex[:12]}"
        scene = {
            "scene_id": scene_id, "narration_text": narration, "caption_text": caption,
            "duration": round(request.duration, 6), "start": 0.0, "end": 0.0,
            "caption_segments": [], "scene_analysis": analyze_scene(narration),
            "generation": 0, "selected_visual_id": None, "selected_visual": None,
            "visual_options": [], "visual_decision": "suggested",
            "error": {"code": "VISUAL_NEEDED", "message": "Find or upload a visual, then explicitly accept it before rendering.", "retryable": False},
        }
        scenes.insert(position, scene)
        job.setdefault("_visual_catalog", {})[scene_id] = []
        _retime_saved_scenes(scenes)
        _mark_storyboard_changed(job, narration_changed=True)
        return {"scene": dict(scene), "position": position, "dirty": True, "render_required": True}


@app.post("/api/v1/jobs/{job_id}/scenes/{scene_id}/split")
def split_scene(job_id: str, scene_id: str, request: SceneSplitRequest) -> dict[str, Any]:
    with jobs_lock:
        job = _editable_job(job_id)
        scenes = job["scenes"]
        index = next((i for i, item in enumerate(scenes) if item["scene_id"] == scene_id), None)
        if index is None:
            raise _error("SCENE_NOT_FOUND", "No scene exists with that ID in this job.", 404)
        original = scenes[index]
        start, end = float(original["start"]), float(original["end"])
        minimum = 0.1
        if request.split_at <= start + minimum or request.split_at >= end - minimum:
            raise _error("INVALID_SPLIT_POINT", "Choose a split point at least 0.1 seconds from each scene boundary.", 422)
        ratio = (request.split_at - start) / (end - start)
        try:
            narration_a, narration_b = _split_scene_text(original["narration_text"], ratio)
            caption_a, caption_b = _split_scene_text(original["caption_text"], ratio)
        except ValueError as exc:
            raise _error("INVALID_SCENE_SPLIT", str(exc), 422) from exc
        duration_a = round(request.split_at - start, 6)
        duration_b = round(end - request.split_at, 6)
        first_id, second_id = f"scene-{uuid.uuid4().hex[:12]}", f"scene-{uuid.uuid4().hex[:12]}"
        def new_scene(identifier: str, narration: str, caption: str, duration: float) -> dict[str, Any]:
            return {
                "scene_id": identifier, "narration_text": narration, "caption_text": caption,
                "duration": duration, "start": 0.0, "end": 0.0, "caption_segments": [],
                "scene_analysis": analyze_scene(narration), "generation": 0,
                "selected_visual_id": None, "selected_visual": None, "visual_options": [],
                "visual_decision": "suggested", "error": {"code": "VISUAL_NEEDED", "message": "This split scene needs its own accepted visual before rendering.", "retryable": False},
            }
        replacements = [new_scene(first_id, narration_a, caption_a, duration_a), new_scene(second_id, narration_b, caption_b, duration_b)]
        updated = scenes[:index] + replacements + scenes[index + 1:]
        _retime_saved_scenes(updated)
        job["scenes"] = updated
        catalog = job.setdefault("_visual_catalog", {})
        catalog.pop(scene_id, None)
        catalog[first_id], catalog[second_id] = [], []
        _mark_storyboard_changed(job, narration_changed=True)
        return {"scenes": [dict(item) for item in replacements], "dirty": True, "render_required": True}


@app.delete("/api/v1/jobs/{job_id}/scenes/{scene_id}")
def remove_scene(job_id: str, scene_id: str, request: SceneRemoveRequest = Body(...)) -> dict[str, Any]:
    if not request.confirm:
        raise _error("SCENE_DELETE_CONFIRMATION_REQUIRED", "Confirm deletion because this discards this scene's saved edits and visual choice.", 422)
    with jobs_lock:
        job = _editable_job(job_id)
        scenes = job["scenes"]
        if len(scenes) <= 1:
            raise _error("SCENE_DELETE_NOT_ALLOWED", "Keep at least one scene in the storyboard.", 422)
        index = next((i for i, item in enumerate(scenes) if item["scene_id"] == scene_id), None)
        if index is None:
            raise _error("SCENE_NOT_FOUND", "No scene exists with that ID in this job.", 404)
        removed = scenes.pop(index)
        job.setdefault("_visual_catalog", {}).pop(scene_id, None)
        _retime_saved_scenes(scenes)
        _mark_storyboard_changed(job, narration_changed=True)
        return {"removed_scene_id": removed["scene_id"], "dirty": True, "render_required": True}


@app.post("/api/v1/jobs/{job_id}/scenes/{scene_id}/local-video", status_code=201)
async def upload_scene_video(
    job_id: str,
    scene_id: str,
    video: UploadFile = File(...),
    license: str = Form(...),
    attribution: str = Form(...),
    rights_confirmed: bool = Form(...),
) -> dict[str, Any]:
    """Store a validated, rights-declared clip in this job only."""
    suffix = Path(video.filename or "").suffix.lower()
    if suffix not in ALLOWED_FOOTAGE_EXTENSIONS:
        raise _error("INVALID_FOOTAGE_TYPE", "Upload an MP4, MOV, M4V, WebM, OGV, or MKV video file.", 422)
    license, attribution = license.strip(), attribution.strip()
    if len(license) < 2 or len(license) > 300 or len(attribution) < 2 or len(attribution) > 500:
        raise _error("INVALID_FOOTAGE_METADATA", "Provide the clip's license and attribution.", 422)
    if not rights_confirmed:
        raise _error("FOOTAGE_RIGHTS_NOT_CONFIRMED", "Confirm that you have permission to use this clip before uploading.", 422)
    content = await video.read(settings.max_footage_bytes + 1)
    if len(content) < 1024 or len(content) > settings.max_footage_bytes:
        raise _error("INVALID_FOOTAGE_SIZE", f"Footage file must be at least 1 KiB and no larger than {settings.max_footage_bytes // (1024 * 1024)} MiB.", 422)
    with jobs_lock:
        job = _editable_job(job_id)
        scene = next((item for item in job["scenes"] if item["scene_id"] == scene_id), None)
        if scene is None:
            raise _error("SCENE_NOT_FOUND", "No scene exists with that ID in this job.", 404)
    folder = (DATA_DIR / job_id / ".scene-uploads").resolve()
    job_root = (DATA_DIR / job_id).resolve()
    if folder.parent != job_root:
        raise _error("INVALID_UPLOAD_PATH", "The scene upload path is invalid.", 500)
    folder.mkdir(parents=True, exist_ok=True)
    asset_id = f"scene-upload-{uuid.uuid4().hex}"
    destination = folder / f"{asset_id}{suffix}"
    temporary = destination.with_suffix(destination.suffix + ".part")
    try:
        temporary.write_bytes(content)
        _validate_scene_upload(temporary, asset_id)
        temporary.replace(destination)
    except ProviderError as exc:
        raise _error(exc.code, str(exc), 503 if exc.retryable else 422, _safe_provider_error_details(exc.details), retryable=exc.retryable) from exc
    finally:
        temporary.unlink(missing_ok=True)
    visual = SceneVisual(
        asset_id=asset_id, color="black", asset_path=destination, provider="scene-local-upload",
        source="Local video uploaded for this scene", source_url=None, license=license,
        attribution=attribution, ai_generated=False,
        selection_reason="Uploaded locally for this scene; rights are declared by the operator.",
        relevance_score=None, warnings=("Local clip rights are declared by the operator and were not independently verified by SceneCraft.",),
        license_verified=False, rights_basis="user_attestation", media_type="video",
    )
    with jobs_lock:
        job = _editable_job(job_id)
        scene = next((item for item in job["scenes"] if item["scene_id"] == scene_id), None)
        if scene is None:
            destination.unlink(missing_ok=True)
            raise _error("SCENE_NOT_FOUND", "The scene was removed before the upload completed.", 404)
        catalog = job.setdefault("_visual_catalog", {}).setdefault(scene_id, [])
        catalog.append(visual)
        details = _visual_details(visual)
        scene["visual_options"] = [*scene.get("visual_options", []), details]
        scene["selected_visual_id"] = asset_id
        scene["selected_visual"] = details
        scene["visual_decision"] = "accepted"
        scene.pop("error", None)
        _mark_storyboard_changed(job, narration_changed=False)
        return {"scene": dict(scene), "dirty": True, "render_required": True}


@app.post("/api/v1/jobs/{job_id}/scenes/{scene_id}/regenerate", status_code=202)
def regenerate_scene_visual(job_id: str, scene_id: str) -> dict[str, Any]:
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            raise _error("JOB_NOT_FOUND", "No job exists with that ID.", 404)
        if job["status"] not in {"succeeded", "failed"} or not job.get("scenes"):
            raise _error("JOB_NOT_EDITABLE", "Visuals can be regenerated after a draft render has completed.", 409)
        scene = next((item for item in job["scenes"] if item["scene_id"] == scene_id), None)
        if scene is None:
            raise _error("SCENE_NOT_FOUND", "No scene exists with that ID in this job.", 404)
        generation = int(scene.get("generation", 0)) + 1
        index = int(scene_id.rsplit("-", 1)[-1]) - 1
        narration = scene["narration_text"]
        ratio = job.get("_ratio", "16:9")
        excluded_visual_ids = {item.get("selected_visual_id") for item in job.get("scenes", []) if item["scene_id"] != scene_id and item.get("selected_visual_id")}
        # A regeneration must move beyond the current card's existing choices;
        # excluding only other scenes lets deterministic providers return the
        # exact same suggestion list, making the button appear to do nothing.
        excluded_visual_ids.update(option.get("id") for option in scene.get("visual_options", []) if option.get("id"))
    try:
        options = _visual_options(narration, index, job_id, generation, ratio, excluded_visual_ids)
    except ProviderError as exc:
        raise _error(exc.code, str(exc), 503 if exc.retryable else 422, exc.details, retryable=exc.retryable) from exc
    with jobs_lock:
        if job["status"] not in {"succeeded", "failed"}:
            raise _error("JOB_BUSY", "This job started rendering while visual suggestions were being prepared.", 409)
        job["_visual_catalog"][scene_id] = options
        scene["generation"] = generation
        scene["visual_options"] = [_visual_details(option) for option in options]
        scene["selected_visual_id"] = None
        scene["selected_visual"] = None
        scene["visual_decision"] = "suggested"
        scene.pop("error", None)
        job["dirty"] = True
        result = dict(scene)
    return {"job_id": job_id, "scene": result, "dirty": True}


@app.post("/api/v1/jobs/{job_id}/render", status_code=202)
def render_edited_job(job_id: str, request: RenderRequest | None = Body(default=None)) -> dict[str, Any]:
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            raise _error("JOB_NOT_FOUND", "No job exists with that ID.", 404)
        if job["status"] in {"queued", "running"}:
            raise _error("JOB_BUSY", "This job is already rendering.", 409)
        preview = job.get("draft_preview")
        if preview:
            prev_job = jobs.get(preview.get("job_id"))
            if prev_job and prev_job.get("status") in {"queued", "running"}:
                raise _error("JOB_BUSY", "Wait for the draft preview render to finish before rendering the full video.", 409)
        scenes = job.get("scenes") or []
        if not scenes:
            raise _error("JOB_NOT_EDITABLE", "A completed draft is required before scene edits can be rendered.", 409)
        rejected = [scene["scene_id"] for scene in scenes if scene.get("visual_decision") != "accepted" or not scene.get("selected_visual_id")]
        if rejected:
            raise _error("VISUALS_NEED_REVIEW", "Accept or replace every scene visual before rendering.", 422, {"scene_ids": rejected})
        edited_scenes = [{"scene_id": scene["scene_id"], "narration_text": scene["narration_text"], "caption_text": scene["caption_text"], "selected_visual_id": scene["selected_visual_id"], "visual_decision": scene["visual_decision"]} for scene in scenes]
        correlation_id = correlation_context.get()
        ratio = request.aspect_ratio if request and request.aspect_ratio else job["_ratio"]
        if ratio not in RATIOS:
            raise _error("INVALID_ASPECT_RATIO", "Choose 16:9, 9:16, or 1:1.", 422)
        script = job["_script"]
        audio_path = Path(job["_source_audio_path"]) if job.get("_source_audio_path") else None
        job["_ratio"] = ratio
        job.update(status="queued", progress=0, error=None, result=None)
    executor.submit(_render, job_id, script, ratio, audio_path, correlation_id, edited_scenes)
    return {"job_id": job_id, "status": "queued", "full_render": True, "status_url": f"/api/v1/jobs/{job_id}"}


@app.post("/api/v1/jobs/{job_id}/draft-preview", status_code=202)
def create_draft_preview(job_id: str, request: RenderRequest | None = Body(default=None)) -> dict[str, Any]:
    """Render a disposable full-length preview from saved selections, without accepting them."""
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            raise _error("JOB_NOT_FOUND", "No job exists with that ID.", 404)
        if job["status"] in {"queued", "running"}:
            raise _error("JOB_BUSY", "Wait for the current render to finish before creating a draft preview.", 409)
        preview = job.get("draft_preview")
        if preview:
            prev_job = jobs.get(preview.get("job_id"))
            if prev_job and prev_job.get("status") in {"queued", "running"}:
                raise _error("DRAFT_PREVIEW_BUSY", "Wait for the current draft preview to finish before creating another.", 409)
        scenes = job.get("scenes") or []
        if not scenes:
            raise _error("JOB_NOT_EDITABLE", "Create a storyboard before previewing the full draft.", 409)
        missing = [scene["scene_id"] for scene in scenes if not scene.get("selected_visual_id") or scene.get("visual_decision") == "rejected"]
        if missing:
            raise _error("DRAFT_VISUALS_MISSING", "Choose a visual for each scene before creating a draft preview.", 422, {"scene_ids": missing})
        ratio = request.aspect_ratio if request and request.aspect_ratio else job.get("_ratio", "16:9")
        if ratio not in RATIOS:
            raise _error("INVALID_ASPECT_RATIO", "Choose 16:9, 9:16, or 1:1.", 422)
        preview_id = str(uuid.uuid4())
        edited_scenes = [{"scene_id": scene["scene_id"], "narration_text": scene["narration_text"], "caption_text": scene["caption_text"], "selected_visual_id": scene["selected_visual_id"], "visual_decision": scene.get("visual_decision", "suggested")} for scene in scenes]
        source_audio = job.get("_source_audio_path")
        preview_job = {
            "job_id": preview_id, "status": "queued", "progress": 0, "error": None, "result": None,
            "created_at": datetime.now(timezone.utc).isoformat(), "scenes": [dict(scene) for scene in scenes],
            "_visual_catalog": {scene_id: list(options) for scene_id, options in job.get("_visual_catalog", {}).items()},
            "_script": job.get("_script", ""), "_source_audio_path": source_audio, "_ratio": ratio,
        }
        jobs[preview_id] = preview_job
        job["draft_preview"] = {"job_id": preview_id, "status": "queued", "progress": 0, "ratio": ratio, "result": None, "error": None}
        correlation_id = correlation_context.get()
    executor.submit(_render, preview_id, job.get("_script", ""), ratio, Path(source_audio) if source_audio else None, correlation_id, edited_scenes, True, job_id)
    return {"job_id": job_id, "preview_id": preview_id, "status": "queued", "status_url": f"/api/v1/jobs/{job_id}/draft-preview"}


@app.get("/api/v1/jobs/{job_id}/draft-preview")
def get_draft_preview(job_id: str) -> dict[str, Any]:
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            raise _error("JOB_NOT_FOUND", "No job exists with that ID.", 404)
        preview = job.get("draft_preview")
        if not preview:
            return {"job_id": job_id, "status": "not_created", "progress": 0, "result": None, "error": None}
        preview_job = jobs.get(preview["job_id"])
        if not preview_job:
            return {**preview, "status": "failed", "error": {"code": "DRAFT_PREVIEW_LOST", "message": "The draft preview worker state is unavailable. Create the preview again."}}
        return {"job_id": job_id, "preview_id": preview["job_id"], "status": preview_job["status"], "progress": preview_job["progress"], "ratio": preview["ratio"], "result": preview_job.get("result"), "error": preview_job.get("error")}


@app.get("/api/v1/jobs/{job_id}/scenes/{scene_id}/visuals/{visual_id}/preview")
def get_scene_visual_preview(job_id: str, scene_id: str, visual_id: str) -> FileResponse:
    """Serve the selected real/local source media for an explicit scene preview."""
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            raise _error("JOB_NOT_FOUND", "No job exists with that ID.", 404)
        scene = next((item for item in job.get("scenes", []) if item["scene_id"] == scene_id), None)
        if scene is None:
            raise _error("SCENE_NOT_FOUND", "No scene exists with that ID in this job.", 404)
        visual = next((item for item in job.get("_visual_catalog", {}).get(scene_id, []) if item.asset_id == visual_id), None)
        if visual is None:
            raise _error("VISUAL_NOT_AVAILABLE", "Choose a visual candidate belonging to this scene.", 404)
    if visual.asset_path is None:
        if not visual.download_url:
            raise _error("PREVIEW_UNAVAILABLE", "This generated mock graphic has no source video or image file. It will be represented as a labelled graphic in the draft.", 422)
        try:
            visual = _materialize_visual(visual, job_id)
        except ProviderError as exc:
            raise HTTPException(
                status_code=503 if exc.retryable else 422,
                detail={"error": {"code": "PREVIEW_UNAVAILABLE", "message": f"The candidate media could not be fetched for preview: {exc}", "retryable": exc.retryable, "details": _safe_provider_error_details(exc.details)}},
            ) from exc
        with jobs_lock:
            catalog = job.get("_visual_catalog", {}).get(scene_id, [])
            job["_visual_catalog"][scene_id] = [visual if item.asset_id == visual.asset_id else item for item in catalog]
    if visual.asset_path is None or not visual.asset_path.is_file():
        raise _error("PREVIEW_UNAVAILABLE", "The selected source media could not be prepared for preview. Try another candidate or import a local clip.", 422)
    guessed_type, _ = mimetypes.guess_type(str(visual.asset_path))
    media_type = guessed_type or ("image/jpeg" if visual.media_type == "image" else "video/mp4")
    return FileResponse(visual.asset_path, media_type=media_type, filename=visual.asset_path.name)


@app.get("/api/v1/jobs/{job_id}/scenes/{scene_id}")
def get_scene(job_id: str, scene_id: str) -> dict[str, Any]:
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            raise _error("JOB_NOT_FOUND", "No job exists with that ID.", 404)
        scene = next((item for item in job.get("scenes", []) if item["scene_id"] == scene_id), None)
        if scene is None:
            raise _error("SCENE_NOT_FOUND", "No scene exists with that ID in this job.", 404)
        return dict(scene)


@app.get("/api/v1/jobs/{job_id}/captions.srt")
def download_current_scene_captions(job_id: str) -> Response:
    """Download the latest saved scene captions against the project's timeline."""
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            raise _error("JOB_NOT_FOUND", "No job exists with that ID.", 404)
        captions = _merged_scene_captions_srt(job.get("scenes") or [])
    if not captions:
        raise _error("CAPTIONS_UNAVAILABLE", "There are no saved captions to download.", 404)
    return Response(
        content=captions,
        media_type="application/x-subrip; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="scenecraft-captions.srt"'},
    )


@app.get("/api/v1/jobs/{job_id}/artifacts/{filename}")
def get_artifact(job_id: str, filename: str) -> FileResponse:
    allowed_names = {"captions.srt", "timeline.json", "manifest.json", "video_16x9.mp4", "video_9x16.mp4", "video_1x1.mp4"}
    if Path(filename).name != filename or filename not in allowed_names:
        raise _error("ARTIFACT_NOT_FOUND", "Artifact not found.", 404)
    with jobs_lock:
        if job_id not in jobs or jobs[job_id]["status"] != "succeeded":
            raise _error("ARTIFACT_NOT_READY", "The artifact is not ready.", 404)
    artifact = DATA_DIR / job_id / filename
    if not artifact.is_file():
        raise _error("ARTIFACT_NOT_FOUND", "Artifact not found.", 404)
    return FileResponse(artifact)

