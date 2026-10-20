from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from dotenv import load_dotenv


PROJECT_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


def load_project_environment(env_file: Path | None = None) -> bool:
    """Load the ignored project-root .env without overriding process settings."""
    if os.environ.get("SCENECRAFT_DISABLE_DOTENV", "").strip() == "1":
        return False
    return load_dotenv(dotenv_path=env_file or PROJECT_ENV_FILE, override=False)


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    ffmpeg_bin: str
    ffprobe_bin: str
    provider: str
    local_footage_dir: Path
    wikimedia_contact: str | None
    max_upload_bytes: int
    job_workers: int
    provider_timeout_seconds: float
    speech_provider: str
    pexels_api_key: str | None
    elevenlabs_api_key: str | None
    elevenlabs_voice_id: str | None
    elevenlabs_model_id: str
    max_footage_bytes: int
    access_token: str | None


def load_settings(environ: dict[str, str] | None = None) -> Settings:
    values = os.environ if environ is None else environ
    try:
        max_upload_bytes = int(values.get("MAX_UPLOAD_BYTES", str(25 * 1024 * 1024)))
        job_workers = int(values.get("JOB_WORKERS", "2"))
        provider_timeout_seconds = float(values.get("PROVIDER_TIMEOUT_SECONDS", "10"))
    except ValueError as exc:
        raise ValueError("MAX_UPLOAD_BYTES and JOB_WORKERS must be integers; PROVIDER_TIMEOUT_SECONDS must be numeric.") from exc

    provider = values.get("BROLL_PROVIDER", "mock").strip().lower()
    speech_provider = values.get("TTS_PROVIDER", "mock").strip().lower()
    if provider not in {"mock", "pexels", "wikimedia", "local"}:
        raise ValueError("BROLL_PROVIDER must be 'mock', 'pexels', 'wikimedia', or 'local'.")
    if speech_provider not in {"mock", "elevenlabs"}:
        raise ValueError("TTS_PROVIDER must be 'mock' or 'elevenlabs'.")
    pexels_api_key = values.get("PEXELS_API_KEY", "").strip() or None
    wikimedia_contact = values.get("WIKIMEDIA_CONTACT", "").strip() or None
    elevenlabs_api_key = values.get("ELEVENLABS_API_KEY", "").strip() or None
    elevenlabs_voice_id = values.get("ELEVENLABS_VOICE_ID", "").strip() or None
    elevenlabs_model_id = values.get("ELEVENLABS_MODEL_ID", "eleven_multilingual_v2").strip()
    access_token = values.get("SCENECRAFT_ACCESS_TOKEN", "").strip() or None
    if not elevenlabs_model_id:
        raise ValueError("ELEVENLABS_MODEL_ID must not be empty.")
    try:
        max_footage_bytes = int(values.get("MAX_FOOTAGE_BYTES", str(25 * 1024 * 1024)))
    except ValueError as exc:
        raise ValueError("MAX_FOOTAGE_BYTES must be an integer.") from exc
    if max_footage_bytes < 1024:
        raise ValueError("MAX_FOOTAGE_BYTES must be at least 1024 bytes.")
    if max_upload_bytes <= 0:
        raise ValueError("MAX_UPLOAD_BYTES must be greater than zero.")
    if not 1 <= job_workers <= 8:
        raise ValueError("JOB_WORKERS must be between 1 and 8.")
    if not 0.05 <= provider_timeout_seconds <= 120:
        raise ValueError("PROVIDER_TIMEOUT_SECONDS must be between 0.05 and 120.")

    data_dir_value = values.get("BROLL_DATA_DIR", str(Path(__file__).resolve().parent.parent / "data" / "jobs")).strip()
    local_footage_value = values.get("BROLL_LOCAL_LIBRARY", str(Path(__file__).resolve().parent.parent / "data" / "footage-library")).strip()
    ffmpeg_bin = values.get("FFMPEG_BIN", "ffmpeg").strip()
    ffprobe_bin = values.get("FFPROBE_BIN", "ffprobe").strip()
    if not data_dir_value or not local_footage_value or not ffmpeg_bin or not ffprobe_bin:
        raise ValueError("BROLL_DATA_DIR, BROLL_LOCAL_LIBRARY, FFMPEG_BIN, and FFPROBE_BIN must not be empty.")

    return Settings(
        data_dir=Path(data_dir_value),
        ffmpeg_bin=ffmpeg_bin,
        ffprobe_bin=ffprobe_bin,
        provider=provider,
        local_footage_dir=Path(local_footage_value),
        wikimedia_contact=wikimedia_contact,
        max_upload_bytes=max_upload_bytes,
        job_workers=job_workers,
        provider_timeout_seconds=provider_timeout_seconds,
        speech_provider=speech_provider,
        pexels_api_key=pexels_api_key,
        elevenlabs_api_key=elevenlabs_api_key,
        elevenlabs_voice_id=elevenlabs_voice_id,
        elevenlabs_model_id=elevenlabs_model_id,
        max_footage_bytes=max_footage_bytes,
        access_token=access_token,
    )


load_project_environment()
settings = load_settings()
