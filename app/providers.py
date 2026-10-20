from __future__ import annotations

import colorsys
import hashlib
import html
import json
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request, urlopen
from app.pipeline import scene_search_phrases, score_keyword_overlap, score_scene_relevance


class ProviderError(RuntimeError):
    """A provider failure with a safe, stable code and retry policy."""

    def __init__(self, code: str, message: str, *, retryable: bool, details: dict[str, object] | None = None):
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.details = details


def _retry_after_details(headers: object) -> dict[str, object]:
    """Preserve a server retry hint and expose a parsed delay when it is valid."""
    value = headers.get("Retry-After") if hasattr(headers, "get") else None
    if not isinstance(value, str) or not value.strip():
        return {}
    value = value.strip()
    details: dict[str, object] = {"retry_after": value[:128]}
    seconds: int | None = None
    if re.fullmatch(r"\d+", value):
        seconds = int(value)
    else:
        try:
            retry_at = parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            seconds = max(0, int((retry_at - datetime.now(timezone.utc)).total_seconds()))
        except (TypeError, ValueError, OverflowError):
            pass
    if seconds is not None:
        details["retry_after_seconds"] = seconds
    return details


@dataclass(frozen=True)
class SceneVisual:
    """A selected visual and the provenance that must travel with it."""

    asset_id: str
    color: str
    asset_path: Path | None
    provider: str
    source: str
    source_url: str | None
    license: str
    attribution: str
    ai_generated: bool | None
    selection_reason: str
    relevance_score: float | None
    keyword_overlap_score: float | None = None
    warnings: tuple[str, ...] = ()
    license_verified: bool = False
    thumbnail_url: str | None = None
    download_url: str | None = None
    rights_basis: str = "provider_metadata"
    media_type: str = "video"
    selection_requires_review: bool = False


class FootageProvider(Protocol):
    """Small interface that lets a verified footage source replace the mock."""

    name: str
    version: str

    def select_visual(self, scene_text: str, index: int, job_id: str) -> SceneVisual: ...

    def visual_options(self, scene_text: str, index: int, job_id: str, generation: int = 0) -> list[SceneVisual]: ...


@dataclass(frozen=True)
class UnconfiguredFootageProvider:
    """Expose a selected-but-incomplete real provider without using mock media."""

    name: str
    version: str
    configuration_message: str

    def select_visual(self, scene_text: str, index: int, job_id: str) -> SceneVisual:
        raise ProviderError("BROLL_NOT_CONFIGURED", self.configuration_message, retryable=False)

    def visual_options(self, scene_text: str, index: int, job_id: str, generation: int = 0) -> list[SceneVisual]:
        raise ProviderError("BROLL_NOT_CONFIGURED", self.configuration_message, retryable=False)

    def visual_options_for_ratio(self, scene_text: str, index: int, job_id: str, generation: int, ratio: str, exclude_asset_ids: set[str]) -> list[SceneVisual]:
        raise ProviderError("BROLL_NOT_CONFIGURED", self.configuration_message, retryable=False)


class SpeechProvider(Protocol):
    """Server-side speech generator. Implementations write audio to a private file."""

    name: str
    version: str

    def synthesize(self, text: str, destination: Path) -> dict[str, str | None]: ...


class CommonsRequestLimiter:
    """Share request spacing and active cooldown across Commons adapters."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.last_request_at = 0.0
        self.cooldown_until = 0.0


class PexelsFootageProvider:
    """Pexels video search adapter; stores bounded candidates in the job folder."""

    name = "pexels"
    version = "videos-api-v1"
    api_url = "https://api.pexels.com/v1/videos/search"
    license_text = "Pexels License; commercial use subject to Pexels restrictions and third-party rights review"
    stop_words = {"a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "in", "into", "is", "it", "of", "on", "or", "that", "the", "their", "this", "to", "was", "were", "with"}

    def __init__(self, api_key: str, data_dir: Path, timeout: float = 10, max_download_bytes: int = 25 * 1024 * 1024):
        self._api_key = api_key
        self._data_dir = data_dir
        self._timeout = timeout
        self._max_download_bytes = max_download_bytes

    def _query(self, scene_text: str) -> str:
        phrases = scene_search_phrases(scene_text)
        return phrases[0] if phrases else ""

    def select_visual(self, scene_text: str, index: int, job_id: str) -> SceneVisual:
        options = self.visual_options(scene_text, index, job_id)
        if not options:
            raise ProviderError("PROVIDER_NO_MEDIA", "Pexels returned no usable videos for this scene.", retryable=False)
        return options[0]

    def visual_options(self, scene_text: str, index: int, job_id: str, generation: int = 0) -> list[SceneVisual]:
        return self.visual_options_for_ratio(scene_text, index, job_id, generation, "16:9", set())

    def materialize(self, visual: SceneVisual, job_id: str) -> SceneVisual:
        if not visual.download_url or not visual.asset_id.startswith("pexels-video-"):
            raise ProviderError("PROVIDER_INVALID_RESULT", "The selected Pexels video has no safe download reference.", retryable=False)
        video_id = visual.asset_id.removeprefix("pexels-video-")
        return SceneVisual(**{**visual.__dict__, "asset_path": self._download(visual.download_url, job_id, video_id)})

    def visual_options_for_ratio(self, scene_text: str, index: int, job_id: str, generation: int, ratio: str, exclude_asset_ids: set[str]) -> list[SceneVisual]:
        query = self._query(scene_text)
        if not query:
            raise ProviderError("PROVIDER_NO_MEDIA", "The scene has no searchable words for Pexels.", retryable=False)
        orientation = {"16:9": "landscape", "9:16": "portrait", "1:1": "square"}.get(ratio, "landscape")
        url = self.api_url + "?" + urlencode({"query": query, "orientation": orientation, "size": "small", "per_page": 15})
        try:
            with urlopen(Request(url, headers={"Authorization": self._api_key, "Accept": "application/json", "User-Agent": "SceneCraft-Studio/1.0"}), timeout=self._timeout) as response:
                payload_bytes = response.read(2 * 1024 * 1024 + 1)
        except HTTPError as exc:
            retryable = exc.code == 429 or exc.code >= 500
            code = "PROVIDER_RATE_LIMITED" if exc.code == 429 else "PROVIDER_AUTH_FAILED" if exc.code in {401, 403} else "PROVIDER_UNAVAILABLE"
            raise ProviderError(
                code,
                f"Pexels API request failed with HTTP {exc.code}.",
                retryable=retryable,
                details={"status_code": exc.code, **_retry_after_details(exc.headers)},
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise ProviderError("PROVIDER_UNAVAILABLE", "Pexels API could not be reached.", retryable=True, details={"exception_type": type(exc).__name__}) from exc
        if len(payload_bytes) > 2 * 1024 * 1024:
            raise ProviderError("PROVIDER_INVALID_RESULT", "Pexels returned an unexpectedly large search response.", retryable=False)
        try:
            videos = json.loads(payload_bytes).get("videos", [])
        except (UnicodeDecodeError, json.JSONDecodeError, AttributeError) as exc:
            raise ProviderError("PROVIDER_INVALID_RESULT", "Pexels returned invalid search data.", retryable=False) from exc
        options: list[SceneVisual] = []
        seen: set[str] = set(exclude_asset_ids)
        for video in videos:
            if not isinstance(video, dict):
                continue
            video_id = str(video.get("id", "")).strip()
            asset_id = f"pexels-video-{video_id}"
            if not video_id.isdigit() or asset_id in seen:
                continue
            files = video.get("video_files")
            if not isinstance(files, list):
                continue
            eligible = [item for item in files if isinstance(item, dict) and item.get("file_type") == "video/mp4" and item.get("link")]
            eligible.sort(key=lambda item: int(item.get("width", 0)) * int(item.get("height", 0)) if str(item.get("width", "0")).isdigit() and str(item.get("height", "0")).isdigit() else 10**12)
            chosen_file = next((item for item in eligible if int(item.get("width", 0) or 0) >= 640), eligible[-1] if eligible else None)
            if not chosen_file:
                continue
            download_url = str(chosen_file["link"])
            if not self._is_allowed_video_url(download_url):
                continue
            user = video.get("user") or {}
            photographer = str(user.get("name") or "Pexels contributor")[:120]
            source_url = str(video.get("url") or "")
            if urlparse(source_url).scheme != "https" or urlparse(source_url).hostname not in {"www.pexels.com", "pexels.com"}:
                continue
            thumbnail_url = None
            pictures = video.get("video_pictures")
            if isinstance(pictures, list) and pictures and isinstance(pictures[0], dict):
                candidate_thumb = str(pictures[0].get("picture") or "")
                if urlparse(candidate_thumb).scheme == "https" and urlparse(candidate_thumb).hostname == "images.pexels.com":
                    thumbnail_url = candidate_thumb
            options.append(SceneVisual(
                asset_id=asset_id,
                color="black",
                asset_path=None,
                provider=self.name,
                source=f"Pexels video by {photographer}",
                source_url=source_url,
                license=self.license_text,
                attribution=f"Video by {photographer} via Pexels",
                ai_generated=False,
                selection_reason=f"Pexels API-ranked result {len(options) + 1} for scene-specific query '{query}'; title/description relevance was not independently scored, so review before export",
                relevance_score=None,
                warnings=("Pexels API ranking is not an independently calculated relevance score.", "Review model, property, trademark, and other third-party rights for your intended use."),
                license_verified=True,
                thumbnail_url=thumbnail_url,
                download_url=download_url,
                selection_requires_review=True,
            ))
            seen.add(asset_id)
            if len(options) == 3:
                break
        if not options:
            raise ProviderError("PROVIDER_NO_MEDIA", "Pexels returned no usable, non-repeated video results for this scene.", retryable=False, details={"orientation": orientation})
        return options

    def _download(self, url: str, job_id: str, video_id: str) -> Path:
        if not self._is_allowed_video_url(url):
            raise ProviderError("PROVIDER_INVALID_DOWNLOAD", "Pexels returned a video URL outside its documented video hosts.", retryable=False)
        job_dir = self._data_dir / job_id
        asset_dir = job_dir / ".provider-assets"
        asset_dir.mkdir(parents=True, exist_ok=True)
        destination = asset_dir / f"pexels-{video_id}.mp4"
        temporary = destination.with_suffix(".part")
        try:
            with urlopen(Request(url, headers={"User-Agent": "SceneCraft-Studio/1.0"}), timeout=self._timeout) as response, temporary.open("wb") as output:
                if not self._is_allowed_video_url(response.geturl()):
                    raise ProviderError("PROVIDER_INVALID_DOWNLOAD", "Pexels video download redirected outside its documented video hosts.", retryable=False)
                content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                if content_type and content_type not in {"video/mp4", "application/octet-stream"}:
                    raise ProviderError("PROVIDER_INVALID_DOWNLOAD", "Pexels returned a download with an unexpected media type.", retryable=False)
                total = 0
                while True:
                    chunk = response.read(64 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > self._max_download_bytes:
                        raise ProviderError("PROVIDER_DOWNLOAD_TOO_LARGE", "Pexels video exceeded the configured download limit.", retryable=False, details={"max_bytes": self._max_download_bytes})
                    output.write(chunk)
            if total < 1024:
                raise ProviderError("PROVIDER_INVALID_DOWNLOAD", "Pexels returned an empty or incomplete video file.", retryable=False)
            temporary.replace(destination)
            return destination
        except ProviderError:
            raise
        except HTTPError as exc:
            code = "PROVIDER_RATE_LIMITED" if exc.code == 429 else "PROVIDER_DOWNLOAD_FAILED"
            raise ProviderError(
                code,
                f"Pexels video download failed with HTTP {exc.code}.",
                retryable=exc.code == 429 or exc.code >= 500,
                details={"status_code": exc.code, **_retry_after_details(exc.headers)},
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise ProviderError("PROVIDER_DOWNLOAD_FAILED", "Pexels video download failed.", retryable=True, details={"exception_type": type(exc).__name__}) from exc
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _is_allowed_video_url(url: str) -> bool:
        parsed = urlparse(url)
        if parsed.scheme != "https":
            return False
        if parsed.hostname == "videos.pexels.com":
            return parsed.path.startswith("/video-files/")
        # The official Pexels video API examples currently return Vimeo-hosted
        # MP4 download URLs. Keep this tightly scoped to their /external/ path.
        if parsed.hostname == "player.vimeo.com":
            return parsed.path.startswith("/external/")
        return False


class WikimediaCommonsFootageProvider:
    """Read-only Commons search; no API key, but a contact-bearing User-Agent is required."""

    name = "wikimedia-commons"
    version = "mediawiki-action-api"
    api_url = "https://commons.wikimedia.org/w/api.php"
    stop_words = PexelsFootageProvider.stop_words
    license_pattern = re.compile(r"^(?:CC0(?:\s+1\.0)?|Public domain|CC BY(?:-SA)?(?:\s+(?:2(?:\.0|\.5)?|3(?:\.0)?|4(?:\.0)?))?)$", re.IGNORECASE)
    max_search_bytes = 2 * 1024 * 1024
    supported_video_extensions = {".mp4", ".webm", ".ogv", ".mkv", ".mov"}
    max_search_phrases = 4

    def __init__(self, data_dir: Path, contact: str, timeout: float = 10, max_download_bytes: int = 25 * 1024 * 1024, min_request_interval: float = 0.35, max_retry_after_seconds: int = 3, request_limiter: CommonsRequestLimiter | None = None):
        self._data_dir = data_dir
        self._contact = contact
        self._timeout = timeout
        self._max_download_bytes = max_download_bytes
        self._request_limiter = request_limiter or CommonsRequestLimiter()
        self._request_lock = self._request_limiter.lock
        self._min_request_interval = min_request_interval
        self._max_retry_after_seconds = max_retry_after_seconds
        self._last_rejection_counts: dict[str, int] = {}

    @property
    def operation_timeout_seconds(self) -> float:
        """Bound the complete, serial alternative-search plan, not one request."""
        per_search_budget = self._timeout + self._max_retry_after_seconds + self._timeout
        return self.max_search_phrases * per_search_budget + max(0, self.max_search_phrases - 1) * self._min_request_interval

    def _query(self, scene_text: str) -> str:
        phrases = self._search_phrases(scene_text)
        return phrases[0] if phrases else ""

    def _search_phrases(self, scene_text: str) -> list[str]:
        return scene_search_phrases(scene_text, self.max_search_phrases)

    def select_visual(self, scene_text: str, index: int, job_id: str) -> SceneVisual:
        options = self.visual_options(scene_text, index, job_id)
        return options[0]

    def visual_options(self, scene_text: str, index: int, job_id: str, generation: int = 0) -> list[SceneVisual]:
        return self.visual_options_for_ratio(scene_text, index, job_id, generation, "16:9", set())

    def visual_options_for_ratio(self, scene_text: str, index: int, job_id: str, generation: int, ratio: str, exclude_asset_ids: set[str]) -> list[SceneVisual]:
        queries = self._search_phrases(scene_text)
        if not queries:
            raise ProviderError("PROVIDER_NO_MEDIA", "The scene has no searchable words for Wikimedia Commons.", retryable=False)
        attempted_queries: list[str] = []
        collected: dict[str, SceneVisual] = {}
        rejection_counts: dict[str, int] = {}
        for query in queries:
            attempted_queries.append(query)
            raw = self._search(query, filetype="video")
            try:
                pages = json.loads(raw).get("query", {}).get("pages", [])
            except (UnicodeDecodeError, json.JSONDecodeError, AttributeError) as exc:
                raise ProviderError("PROVIDER_INVALID_RESULT", "Wikimedia Commons returned invalid search data.", retryable=False) from exc
            if isinstance(pages, dict):
                pages = list(pages.values())
            options = self._parse_video_options(pages, query, exclude_asset_ids, scene_text)
            for reason, count in self._last_rejection_counts.items():
                rejection_counts[reason] = rejection_counts.get(reason, 0) + count
            for option in options:
                current = collected.get(option.asset_id)
                if current is None or (option.relevance_score or 0) > (current.relevance_score or 0):
                    collected[option.asset_id] = option
            if options and max(option.relevance_score or 0 for option in options) >= 0.6:
                break
        if collected:
            return sorted(collected.values(), key=lambda option: (-(option.relevance_score or 0), option.asset_id))[:3]
        raise ProviderError(
            "PROVIDER_NO_MEDIA",
            "Wikimedia Commons returned no video candidates with supported commercial-use license metadata after trying alternative scene searches.",
            retryable=False,
            details={"queries_tried": attempted_queries, "rejected_candidates": rejection_counts},
        )

    def _search(self, query: str, *, filetype: str = "video") -> bytes:
        started_at = time.monotonic()
        params = {
            "action": "query", "format": "json", "formatversion": "2", "generator": "search",
            "gsrnamespace": "6", "gsrlimit": "12", "gsrsearch": f"filetype:{filetype} {query}",
            "prop": "imageinfo", "iiprop": "url|size|mime|mediatype|extmetadata", "iiurlwidth": "480",
            "iiextmetadatafilter": "Artist|Credit|LicenseShortName|LicenseUrl|UsageTerms|Attribution|ImageDescription|ObjectName",
        }
        request = Request(self.api_url + "?" + urlencode(params), headers={
            "Accept": "application/json", "User-Agent": f"SceneCraftStudio/1.0 ({self._contact})",
        })
        for attempt in range(2):
            try:
                with self._request_lock:
                    now = time.monotonic()
                    if self._request_limiter.cooldown_until > now:
                        remaining = max(0, int(self._request_limiter.cooldown_until - now + 0.999))
                        raise ProviderError(
                            "PROVIDER_RATE_LIMITED", "Wikimedia Commons rate limit is still active; wait before searching again.",
                            retryable=True, details={"retry_after_seconds": remaining},
                        )
                    pause = max(0.0, self._min_request_interval - (now - self._request_limiter.last_request_at))
                    if pause:
                        time.sleep(pause)
                    self._request_limiter.last_request_at = time.monotonic()
                    try:
                        with urlopen(request, timeout=self._timeout) as response:
                            raw = response.read(self.max_search_bytes + 1)
                    except HTTPError as exc:
                        retry_details = _retry_after_details(exc.headers)
                        delay = retry_details.get("retry_after_seconds")
                        if exc.code == 429 and isinstance(delay, int):
                            self._request_limiter.cooldown_until = time.monotonic() + delay
                            if attempt == 0 and delay <= self._max_retry_after_seconds:
                                if delay:
                                    time.sleep(delay)
                                self._request_limiter.cooldown_until = 0.0
                                continue
                        retryable = exc.code == 429 or exc.code >= 500
                        code = "PROVIDER_RATE_LIMITED" if exc.code == 429 else "PROVIDER_UNAVAILABLE"
                        details = {"stage": "commons_search", "elapsed_ms": round((time.monotonic() - started_at) * 1000), "status_code": exc.code, **retry_details}
                        raise ProviderError(code, f"Wikimedia Commons search failed with HTTP {exc.code}.", retryable=retryable, details=details) from exc
                if len(raw) > self.max_search_bytes:
                    raise ProviderError("PROVIDER_INVALID_RESULT", "Wikimedia Commons returned an unexpectedly large search response.", retryable=False)
                return raw
            except ProviderError:
                raise
            except (URLError, TimeoutError, OSError) as exc:
                raise ProviderError("PROVIDER_UNAVAILABLE", "Wikimedia Commons could not be reached.", retryable=True, details={"stage": "commons_search", "elapsed_ms": round((time.monotonic() - started_at) * 1000), "exception_type": type(exc).__name__}) from exc
        raise ProviderError("PROVIDER_RATE_LIMITED", "Wikimedia Commons rate limit persisted after one bounded retry.", retryable=True)

    def _parse_video_options(self, pages: object, query: str, exclude_asset_ids: set[str], scene_context: str | None = None) -> list[SceneVisual]:
        options: list[SceneVisual] = []
        rejected: dict[str, int] = {}

        def reject(reason: str) -> None:
            rejected[reason] = rejected.get(reason, 0) + 1

        for page in pages if isinstance(pages, list) else []:
            if not isinstance(page, dict):
                reject("invalid_page")
                continue
            infos = page.get("imageinfo")
            if not isinstance(infos, list) or not infos or not isinstance(infos[0], dict):
                reject("missing_file_metadata")
                continue
            info = infos[0]
            # Commons identifies media type separately from the HTTP/MIME
            # container value. In particular, valid Ogg video files can be
            # reported as application/ogg rather than video/ogg. Require the
            # explicit VIDEO media type before allowing that container, so an
            # audio .ogg file can never become a footage candidate.
            media_type = str(info.get("mediatype", "")).strip().upper()
            mime_type = str(info.get("mime", "")).strip().lower()
            if media_type != "VIDEO" or int(info.get("size", 0) or 0) > self._max_download_bytes:
                reject("not_video_or_too_large")
                continue
            download_url = str(info.get("url", ""))
            if not self._allowed_media_url(download_url):
                reject("unsafe_download_url")
                continue
            extension = Path(urlparse(download_url).path).suffix.lower()
            if extension not in self.supported_video_extensions:
                reject("unsupported_video_extension")
                continue
            if not mime_type.startswith("video/") and not (mime_type == "application/ogg" and extension == ".ogv"):
                reject("unsupported_video_mime")
                continue
            ext = info.get("extmetadata") or {}
            license_name = self._plain_meta(ext, "LicenseShortName") or self._plain_meta(ext, "UsageTerms")
            license_url = self._plain_meta(ext, "LicenseUrl")
            if not self.license_pattern.fullmatch(license_name):
                reject("unsupported_or_missing_license")
                continue
            if license_name.lower().startswith("cc") and (urlparse(license_url).scheme != "https" or urlparse(license_url).hostname != "creativecommons.org"):
                reject("unverified_license_url")
                continue
            artist = self._plain_meta(ext, "Artist") or self._plain_meta(ext, "Credit")
            if license_name.lower().startswith("cc by") and not artist:
                # Attribution is a condition of these licenses; don't fabricate a creator credit.
                reject("missing_required_attribution")
                continue
            artist = artist or "Creator not identified on Commons file page"
            title = str(page.get("title", ""))
            description = self._plain_meta(ext, "ImageDescription") or self._plain_meta(ext, "ObjectName")
            relevance_score = score_scene_relevance(query, title.removeprefix("File:"), description, scene_context)
            keyword_overlap = score_keyword_overlap(query, title.removeprefix("File:"), description)
            source_url = "https://commons.wikimedia.org/wiki/" + quote(title.replace(" ", "_"), safe="_:()")
            asset_id = f"commons-{page.get('pageid', '')}"
            if not asset_id.removeprefix("commons-").isdigit() or asset_id in exclude_asset_ids:
                reject("invalid_or_repeated_asset")
                continue
            thumbnail_url = str(info.get("thumburl", "")) or None
            if thumbnail_url and not self._allowed_media_url(thumbnail_url):
                thumbnail_url = None
            license_text = f"{license_name}; see {license_url}" if license_url else license_name
            review_required = relevance_score < 0.6
            review_note = "weak metadata match; human review required" if review_required else "inspect source and rights before export"
            options.append(SceneVisual(
                asset_id=asset_id, color="black", asset_path=None, provider=self.name,
                source=title.removeprefix("File:"), source_url=source_url, license=license_text,
                attribution=artist, ai_generated=None,
                selection_reason=f"Commons scene-relevance evidence {relevance_score:.3f} (query keyword overlap {keyword_overlap:.3f}) for '{query}'; {review_note}",
                relevance_score=relevance_score,
                keyword_overlap_score=keyword_overlap,
                warnings=("Commons license fields are uploader-provided and are not guaranteed by Wikimedia; verify the file page and any person, property, or trademark rights.",),
                license_verified=False, thumbnail_url=thumbnail_url, download_url=download_url,
                rights_basis="commons_file_metadata",
                media_type="video", selection_requires_review=review_required,
            ))
            if len(options) == 3:
                break
        self._last_rejection_counts = rejected
        return options

    @staticmethod
    def _plain_meta(metadata: dict[str, object], key: str) -> str:
        item = metadata.get(key)
        value = item.get("value") if isinstance(item, dict) else item
        return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]*>", " ", str(value or "")))).strip()

    @staticmethod
    def _allowed_media_url(value: str) -> bool:
        parsed = urlparse(value)
        return parsed.scheme == "https" and parsed.hostname == "upload.wikimedia.org" and parsed.path.startswith("/wikipedia/commons/")

    def materialize(self, visual: SceneVisual, job_id: str) -> SceneVisual:
        if visual.provider != self.name or not visual.download_url or not visual.asset_id.startswith("commons-"):
            raise ProviderError("PROVIDER_INVALID_RESULT", "The selected Commons clip has no safe media reference.", retryable=False)
        if not self._allowed_media_url(visual.download_url):
            raise ProviderError("PROVIDER_INVALID_DOWNLOAD", "Commons returned a file outside the upload host.", retryable=False)
        suffix = Path(urlparse(visual.download_url).path).suffix.lower()
        if suffix not in self.supported_video_extensions:
            raise ProviderError("PROVIDER_INVALID_DOWNLOAD", "Commons returned an unsupported video format.", retryable=False)
        folder = self._data_dir / job_id / ".provider-assets"
        folder.mkdir(parents=True, exist_ok=True)
        destination = folder / f"{visual.asset_id}{suffix}"
        temporary = destination.with_suffix(destination.suffix + ".part")
        started_at = time.monotonic()
        try:
            with urlopen(Request(visual.download_url, headers={"User-Agent": f"SceneCraftStudio/1.0 ({self._contact})"}), timeout=self._timeout) as response, temporary.open("wb") as output:
                if not self._allowed_media_url(response.geturl()):
                    raise ProviderError("PROVIDER_INVALID_DOWNLOAD", "Commons file download redirected outside the upload host.", retryable=False)
                content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                if content_type and not content_type.startswith("video/") and content_type not in {"application/octet-stream", "application/ogg"}:
                    raise ProviderError("PROVIDER_INVALID_DOWNLOAD", "Commons returned a download with an unexpected media type.", retryable=False)
                total = 0
                while True:
                    chunk = response.read(64 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > self._max_download_bytes:
                        raise ProviderError("PROVIDER_DOWNLOAD_TOO_LARGE", "Commons video exceeded the configured download limit.", retryable=False)
                    output.write(chunk)
            if total < 1024:
                raise ProviderError("PROVIDER_INVALID_DOWNLOAD", "Commons returned an empty or incomplete media file.", retryable=False)
            temporary.replace(destination)
            return SceneVisual(**{**visual.__dict__, "asset_path": destination})
        except ProviderError:
            raise
        except HTTPError as exc:
            retryable = exc.code == 429 or exc.code >= 500
            code = "PROVIDER_RATE_LIMITED" if exc.code == 429 else "PROVIDER_DOWNLOAD_FAILED"
            details = {"stage": "commons_download", "elapsed_ms": round((time.monotonic() - started_at) * 1000), "status_code": exc.code, **_retry_after_details(exc.headers)}
            raise ProviderError(code, f"Commons file download failed with HTTP {exc.code}.", retryable=retryable, details=details) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise ProviderError("PROVIDER_DOWNLOAD_FAILED", "Commons file download failed.", retryable=True, details={"stage": "commons_download", "elapsed_ms": round((time.monotonic() - started_at) * 1000), "exception_type": type(exc).__name__}) from exc
        finally:
            temporary.unlink(missing_ok=True)


class WikimediaCommonsImageProvider(WikimediaCommonsFootageProvider):
    """Commons still-image adapter restricted to licensed raster files."""

    name = "wikimedia-commons-images"
    version = "mediawiki-action-api-raster-images-v1"
    supported_image_extensions = {".jpg", ".jpeg", ".png", ".webp"}
    allowed_image_types = {"image/jpeg", "image/png", "image/webp"}

    def visual_options(self, scene_text: str, index: int, job_id: str, generation: int = 0) -> list[SceneVisual]:
        return self.visual_options_for_ratio(scene_text, index, job_id, generation, "16:9", set())

    def visual_options_for_ratio(self, scene_text: str, index: int, job_id: str, generation: int, ratio: str, exclude_asset_ids: set[str]) -> list[SceneVisual]:
        queries = self._search_phrases(scene_text)
        if not queries:
            raise ProviderError("PROVIDER_NO_MEDIA", "The scene has no searchable words for Commons images.", retryable=False)
        attempted: list[str] = []
        collected: dict[str, SceneVisual] = {}
        for query in queries:
            attempted.append(query)
            raw = self._search(query, filetype="bitmap")
            try:
                pages = json.loads(raw).get("query", {}).get("pages", [])
            except (UnicodeDecodeError, json.JSONDecodeError, AttributeError) as exc:
                raise ProviderError("PROVIDER_INVALID_RESULT", "Wikimedia Commons returned invalid image search data.", retryable=False) from exc
            if isinstance(pages, dict):
                pages = list(pages.values())
            options = self._parse_image_options(pages, query, exclude_asset_ids, scene_text)
            for option in options:
                current = collected.get(option.asset_id)
                if current is None or (option.relevance_score or 0) > (current.relevance_score or 0):
                    collected[option.asset_id] = option
            if options and max(option.relevance_score or 0 for option in options) >= 0.6:
                break
        if collected:
            return sorted(collected.values(), key=lambda option: (-(option.relevance_score or 0), option.asset_id))[:3]
        raise ProviderError("PROVIDER_NO_MEDIA", "Commons returned no raster images with supported license metadata for the scene.", retryable=False, details={"queries_tried_count": len(attempted)})

    def _parse_image_options(self, pages: object, query: str, exclude_asset_ids: set[str], scene_context: str | None = None) -> list[SceneVisual]:
        options: list[SceneVisual] = []
        for page in pages if isinstance(pages, list) else []:
            if not isinstance(page, dict):
                continue
            infos = page.get("imageinfo")
            if not isinstance(infos, list) or not infos or not isinstance(infos[0], dict):
                continue
            info = infos[0]
            mime = str(info.get("mime", "")).lower()
            url = str(info.get("url", ""))
            suffix = Path(urlparse(url).path).suffix.lower()
            if mime not in self.allowed_image_types or suffix not in self.supported_image_extensions:
                continue
            if not self._allowed_media_url(url) or int(info.get("size", 0) or 0) > self._max_download_bytes:
                continue
            metadata = info.get("extmetadata") or {}
            license_name = self._plain_meta(metadata, "LicenseShortName") or self._plain_meta(metadata, "UsageTerms")
            license_url = self._plain_meta(metadata, "LicenseUrl")
            if not self.license_pattern.fullmatch(license_name):
                continue
            if license_name.lower().startswith("cc") and (urlparse(license_url).scheme != "https" or urlparse(license_url).hostname != "creativecommons.org"):
                continue
            artist = self._plain_meta(metadata, "Artist") or self._plain_meta(metadata, "Credit")
            if license_name.lower().startswith("cc by") and not artist:
                continue
            artist = artist or "Creator not identified on Commons file page"
            title = str(page.get("title", ""))
            description = self._plain_meta(metadata, "ImageDescription") or self._plain_meta(metadata, "ObjectName")
            score = score_scene_relevance(query, title.removeprefix("File:"), description, scene_context)
            keyword_overlap = score_keyword_overlap(query, title.removeprefix("File:"), description)
            asset_id = f"commons-image-{page.get('pageid', '')}"
            if not asset_id.removeprefix("commons-image-").isdigit() or asset_id in exclude_asset_ids:
                continue
            source_url = "https://commons.wikimedia.org/wiki/" + quote(title.replace(" ", "_"), safe="_:()")
            thumbnail = str(info.get("thumburl", "")) or None
            if thumbnail and not self._allowed_media_url(thumbnail):
                thumbnail = None
            license_text = f"{license_name}; see {license_url}" if license_url else license_name
            review_required = score < 0.6
            review_note = "weak metadata match; human review required" if review_required else "inspect source and rights before export"
            options.append(SceneVisual(
                asset_id=asset_id, color="black", asset_path=None, provider=self.name,
                source=title.removeprefix("File:"), source_url=source_url, license=license_text,
                attribution=artist, ai_generated=None,
                selection_reason=f"Commons image scene-relevance evidence {score:.3f} (query keyword overlap {keyword_overlap:.3f}) for '{query}'; {review_note}",
                relevance_score=score,
                keyword_overlap_score=keyword_overlap,
                warnings=("Commons license fields are uploader-provided and are not guaranteed by Wikimedia; verify the file page and any person, property, or trademark rights.",),
                license_verified=False, thumbnail_url=thumbnail, download_url=url,
                rights_basis="commons_file_metadata", media_type="image", selection_requires_review=review_required,
            ))
            if len(options) == 3:
                break
        return options

    def materialize(self, visual: SceneVisual, job_id: str) -> SceneVisual:
        if visual.provider != self.name or visual.media_type != "image" or not visual.download_url or not visual.asset_id.startswith("commons-image-"):
            raise ProviderError("PROVIDER_INVALID_RESULT", "The selected Commons still image has no safe media reference.", retryable=False)
        if not self._allowed_media_url(visual.download_url):
            raise ProviderError("PROVIDER_INVALID_DOWNLOAD", "Commons returned a still image outside the upload host.", retryable=False)
        suffix = Path(urlparse(visual.download_url).path).suffix.lower()
        if suffix not in self.supported_image_extensions:
            raise ProviderError("PROVIDER_INVALID_DOWNLOAD", "Commons returned an unsupported still-image format.", retryable=False)
        folder = self._data_dir / job_id / ".provider-assets"
        folder.mkdir(parents=True, exist_ok=True)
        destination = folder / f"{visual.asset_id}{suffix}"
        temporary = destination.with_suffix(destination.suffix + ".part")
        try:
            with urlopen(Request(visual.download_url, headers={"User-Agent": f"SceneCraftStudio/1.0 ({self._contact})"}), timeout=self._timeout) as response, temporary.open("wb") as output:
                if not self._allowed_media_url(response.geturl()):
                    raise ProviderError("PROVIDER_INVALID_DOWNLOAD", "Commons image download redirected outside the upload host.", retryable=False)
                content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                if content_type and content_type not in self.allowed_image_types:
                    raise ProviderError("PROVIDER_INVALID_DOWNLOAD", "Commons returned an unexpected still-image content type.", retryable=False)
                total = 0
                while True:
                    chunk = response.read(64 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > self._max_download_bytes:
                        raise ProviderError("PROVIDER_DOWNLOAD_TOO_LARGE", "Commons still image exceeded the configured download limit.", retryable=False)
                    output.write(chunk)
            if total < 64 or not self._valid_image_header(temporary):
                raise ProviderError("PROVIDER_INVALID_DOWNLOAD", "Commons returned an empty or invalid raster image.", retryable=False)
            temporary.replace(destination)
            return SceneVisual(**{**visual.__dict__, "asset_path": destination})
        except ProviderError:
            raise
        except HTTPError as exc:
            retryable = exc.code == 429 or exc.code >= 500
            code = "PROVIDER_RATE_LIMITED" if exc.code == 429 else "PROVIDER_DOWNLOAD_FAILED"
            raise ProviderError(code, f"Commons image download failed with HTTP {exc.code}.", retryable=retryable, details={"status_code": exc.code, **_retry_after_details(exc.headers)}) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise ProviderError("PROVIDER_DOWNLOAD_FAILED", "Commons image download failed.", retryable=True, details={"exception_type": type(exc).__name__}) from exc
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _valid_image_header(path: Path) -> bool:
        header = path.read_bytes()[:16]
        return header.startswith(b"\xff\xd8\xff") or header.startswith(b"\x89PNG\r\n\x1a\n") or (header.startswith(b"RIFF") and header[8:12] == b"WEBP")


class LocalFootageLibraryProvider:
    """Search operator-imported clips by filename; sidecars record user rights declarations."""

    name = "local-library"
    version = "filename-match-v1"
    extensions = {".mp4", ".mov", ".m4v", ".webm", ".ogv", ".mkv"}
    stop_words = PexelsFootageProvider.stop_words | {"about", "after", "before", "during", "how", "more", "than", "when", "where", "which", "who", "why"}

    def __init__(self, library_dir: Path, max_download_bytes: int = 25 * 1024 * 1024):
        self.library_dir = library_dir
        self.max_download_bytes = max_download_bytes

    @staticmethod
    def sidecar_path(video_path: Path) -> Path:
        return video_path.with_suffix(video_path.suffix + ".json")

    def visual_options(self, scene_text: str, index: int, job_id: str, generation: int = 0) -> list[SceneVisual]:
        phrases = scene_search_phrases(scene_text)
        if not phrases or not self.library_dir.is_dir():
            return []
        ranked: list[tuple[float, float, str, Path, dict[str, object], set[str], str]] = []
        root = self.library_dir.resolve()
        for path in self.library_dir.rglob("*"):
            if path.is_symlink() or not path.is_file() or path.suffix.lower() not in self.extensions:
                continue
            try:
                path.resolve().relative_to(root)
                if path.stat().st_size > self.max_download_bytes:
                    continue
            except (OSError, ValueError):
                continue
            sidecar = self.sidecar_path(path)
            if sidecar.is_symlink():
                continue
            try:
                meta = json.loads(sidecar.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(meta, dict) or meta.get("rights_confirmed") is not True or not meta.get("license") or not meta.get("attribution"):
                continue
            name_terms = {word.lower() for word in re.findall(r"[A-Za-z0-9'-]+", path.stem.replace("_", " ").replace("-", " ")) if len(word) > 2}
            best: tuple[float, float, set[str], str] = (0.0, 0.0, set(), "")
            for phrase in phrases:
                phrase_terms = {word.lower() for word in re.findall(r"[A-Za-z0-9'-]+", phrase) if word.lower() not in self.stop_words and len(word) > 2}
                matched = phrase_terms & name_terms
                filename_text = path.stem.replace("_", " ").replace("-", " ")
                lexical_score = score_keyword_overlap(phrase, filename_text)
                score = score_scene_relevance(phrase, filename_text, scene_context=scene_text)
                if score > best[0]:
                    best = (score, lexical_score, matched, phrase)
            if best[0] > 0:
                ranked.append((best[0], best[1], path.name.lower(), path, meta, best[2], best[3]))
        ranked.sort(key=lambda row: (-row[0], row[2]))
        options: list[SceneVisual] = []
        for score, lexical_score, _sort_name, path, meta, matched, phrase in ranked[:3]:
            if meta.get("source_url") and (urlparse(str(meta["source_url"])).scheme != "https"):
                continue
            reason = f"Filename matched scene keywords: {', '.join(sorted(matched))}" if matched else f"Filename matched the scene concept '{phrase}' using related device/action terms"
            options.append(SceneVisual(
                asset_id=f"local-{hashlib.sha256(path.relative_to(self.library_dir).as_posix().encode()).hexdigest()[:12]}", color="black", asset_path=path, provider=self.name,
                source=f"Local footage: {path.name}", source_url=str(meta.get("source_url") or "") or None,
                license=f"User-declared: {meta['license']}", attribution=str(meta["attribution"]), ai_generated=None,
                selection_reason=f"{reason}; scene-relevance evidence {score:.3f}, filename keyword overlap {lexical_score:.3f}", relevance_score=score, keyword_overlap_score=lexical_score, selection_requires_review=score < 0.6,
                warnings=("Rights information was supplied and confirmed by the operator; SceneCraft did not independently verify the source or license.",),
                license_verified=False, rights_basis="user_attestation",
            ))
        return options

    def visual_options_for_ratio(self, scene_text: str, index: int, job_id: str, generation: int, ratio: str, exclude_asset_ids: set[str]) -> list[SceneVisual]:
        return [item for item in self.visual_options(scene_text, index, job_id, generation) if item.asset_id not in exclude_asset_ids]

    def select_visual(self, scene_text: str, index: int, job_id: str) -> SceneVisual:
        options = self.visual_options(scene_text, index, job_id)
        if not options:
            raise ProviderError("PROVIDER_NO_MEDIA", "No licensed local footage filename matches this scene.", retryable=False)
        return options[0]


class FallbackFootageProvider:
    """Combine remote video, local video, and (optionally) remote still images."""

    def __init__(self, primary: FootageProvider, local: LocalFootageLibraryProvider, images: WikimediaCommonsImageProvider | None = None):
        self.primary = primary
        self.local = local
        self.images = images
        self.name = f"{primary.name}+{local.name}"
        self.version = f"{primary.version}+{local.version}"

    @property
    def operation_timeout_seconds(self) -> float | None:
        return getattr(self.primary, "operation_timeout_seconds", None)

    def visual_options(self, scene_text: str, index: int, job_id: str, generation: int = 0) -> list[SceneVisual]:
        return self.visual_options_for_ratio(scene_text, index, job_id, generation, "16:9", set())

    def visual_options_for_ratio(self, scene_text: str, index: int, job_id: str, generation: int, ratio: str, exclude_asset_ids: set[str]) -> list[SceneVisual]:
        search = getattr(self.primary, "visual_options_for_ratio", None)
        try:
            remote = search(scene_text, index, job_id, generation, ratio, exclude_asset_ids) if callable(search) else self.primary.visual_options(scene_text, index, job_id, generation)
            remote_error = None
        except ProviderError as exc:
            remote, remote_error = [], exc
        local = self.local.visual_options_for_ratio(scene_text, index, job_id, generation, ratio, exclude_asset_ids)
        if local and not remote:
            reason = f"Local library fallback used after {remote_error.code} from the configured footage provider." if remote_error else "Local library supplied this scene because the configured provider returned no video candidates."
            local = [SceneVisual(**{**item.__dict__, "warnings": item.warnings + (reason,)}) for item in local]
        uncertain_remote = not remote or all(item.selection_requires_review for item in remote)
        images: list[SceneVisual] = []
        image_error: ProviderError | None = None
        if uncertain_remote and self.images is not None:
            try:
                images = self.images.visual_options_for_ratio(scene_text, index, job_id, generation, ratio, exclude_asset_ids)
            except ProviderError as exc:
                image_error = exc
        if remote or local or images:
            combined = remote + local + images
            unique: dict[str, SceneVisual] = {}
            for item in combined:
                unique.setdefault(item.asset_id, item)
            return sorted(unique.values(), key=lambda item: (item.selection_requires_review, -(item.relevance_score if item.relevance_score is not None else -1), item.media_type != "video", item.asset_id))[:3]
        if image_error:
            raise image_error
        if remote_error:
            raise remote_error
        raise ProviderError("PROVIDER_NO_MEDIA", "The selected provider and local library returned no usable scene footage.", retryable=False)

    def materialize(self, visual: SceneVisual, job_id: str) -> SceneVisual:
        if visual.provider == "local-library":
            return visual
        if visual.provider == "wikimedia-commons-images" and self.images is not None:
            return self.images.materialize(visual, job_id)
        materialize = getattr(self.primary, "materialize", None)
        if not callable(materialize):
            raise ProviderError("PROVIDER_INVALID_RESULT", "The selected provider cannot download its video result.", retryable=False)
        return materialize(visual, job_id)


class ElevenLabsSpeechProvider:
    """Natural speech adapter using the documented ElevenLabs HTTP endpoint."""

    name = "elevenlabs"
    version = "text-to-speech-v1"

    def __init__(self, api_key: str, voice_id: str, model_id: str = "eleven_multilingual_v2", timeout: float = 10, max_audio_bytes: int = 20 * 1024 * 1024):
        self._api_key = api_key
        self._voice_id = voice_id
        self._model_id = model_id
        self._timeout = timeout
        self._max_audio_bytes = max_audio_bytes

    def _http_error(self, exc: HTTPError) -> ProviderError:
        """Extract only documented, bounded error fields; never surface raw bodies."""
        raw = b""
        try:
            raw = exc.read(8193)
        except (OSError, AttributeError, TypeError, ValueError):
            pass
        finally:
            try:
                exc.close()
            except (OSError, AttributeError):
                pass
        if len(raw) > 8192:
            raw = raw[:8192]

        detail: dict[str, object] = {}
        try:
            payload = json.loads(raw.decode("utf-8"))
            value = payload.get("detail") if isinstance(payload, dict) else None
            if isinstance(value, dict):
                detail = value
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
            pass

        def safe_text(value: object, limit: int = 240) -> str | None:
            if not isinstance(value, str):
                return None
            text = " ".join(value.split())
            if self._api_key:
                text = text.replace(self._api_key, "[redacted]")
            text = re.sub(r"(?i)(xi-api-key|api[_ -]?key|authorization|bearer)\s*[:=]\s*[^\s,;]+", r"\1=[redacted]", text)
            text = re.sub(r"(?i)\b(?:sk|xi|token)[-_][A-Za-z0-9_-]{12,}\b", "[redacted-token]", text)
            text = re.sub(r"https?://[^\s]+", "[link omitted]", text)
            text = re.sub(r"[\x00-\x1f\x7f]", " ", text)
            text = " ".join(text.split())
            return text[:limit] or None

        provider_message = safe_text(detail.get("message"))
        provider_code = safe_text(detail.get("code"), 80)
        provider_type = safe_text(detail.get("type"), 80)
        request_id = safe_text(detail.get("request_id"), 100)
        retryable = exc.code == 429 or exc.code >= 500
        free_library_voice_restriction = (
            exc.code == 402
            and provider_message is not None
            and "free" in provider_message.casefold()
            and "library voice" in provider_message.casefold()
            and "api" in provider_message.casefold()
        )
        code = (
            "PROVIDER_RATE_LIMITED" if exc.code == 429
            else "PROVIDER_AUTH_FAILED" if exc.code in {401, 403}
            else "PROVIDER_CONFLICT" if exc.code == 409
            else "PROVIDER_VOICE_PLAN_RESTRICTED" if free_library_voice_restriction
            else "PROVIDER_PAYMENT_REQUIRED" if exc.code == 402
            else "PROVIDER_UNAVAILABLE"
        )
        if free_library_voice_restriction:
            message = (
                f"ElevenLabs rejected the configured voice (HTTP 402): {provider_message} "
                "Choose a voice that your account permits for API use, update ELEVENLABS_VOICE_ID in the project .env, "
                "and restart SceneCraft Studio. The configured voice was not changed."
            )
        elif exc.code == 402 and provider_message:
            message = (
                f"ElevenLabs requires account or plan access for this request (HTTP 402): {provider_message} "
                "Review voice eligibility and API credits in your ElevenLabs account."
            )
        elif provider_message:
            message = f"ElevenLabs speech request failed with HTTP {exc.code}: {provider_message}"
        elif exc.code == 409:
            message = "ElevenLabs reported a request conflict (HTTP 409), but its response did not include a readable explanation. Check the provider status details and try again."
        else:
            message = f"ElevenLabs speech request failed with HTTP {exc.code}."
        details: dict[str, object] = {"status_code": exc.code}
        if provider_code:
            details["provider_error_code"] = provider_code
        if provider_type:
            details["provider_error_type"] = provider_type
        if request_id:
            details["provider_request_id"] = request_id
        return ProviderError(code, message, retryable=retryable, details=details)

    def synthesize(self, text: str, destination: Path) -> dict[str, str | None]:
        from urllib.parse import quote

        url = f"https://api.elevenlabs.io/v1/text-to-speech/{quote(self._voice_id, safe='')}?output_format=mp3_44100_128"
        body = json.dumps({"text": text, "model_id": self._model_id}).encode("utf-8")
        request = Request(url, data=body, headers={"xi-api-key": self._api_key, "Content-Type": "application/json", "Accept": "audio/mpeg", "User-Agent": "SceneCraft-Studio/1.0"}, method="POST")
        try:
            with urlopen(request, timeout=self._timeout) as response:
                audio = response.read(self._max_audio_bytes + 1)
                headers = getattr(response, "headers", None)
                header_get = getattr(headers, "get", None)
                usage = header_get("character-cost") if callable(header_get) else None
        except HTTPError as exc:
            raise self._http_error(exc) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise ProviderError("PROVIDER_UNAVAILABLE", "ElevenLabs speech service could not be reached.", retryable=True, details={"exception_type": type(exc).__name__}) from exc
        except (AttributeError, TypeError, ValueError) as exc:
            raise ProviderError("PROVIDER_INVALID_RESULT", "ElevenLabs returned a malformed audio response.", retryable=False) from exc
        if not isinstance(audio, (bytes, bytearray)):
            raise ProviderError("PROVIDER_INVALID_RESULT", "ElevenLabs returned a malformed audio response.", retryable=False)
        audio = bytes(audio)
        if len(audio) > self._max_audio_bytes:
            raise ProviderError("PROVIDER_INVALID_RESULT", "ElevenLabs audio response exceeded the configured size limit.", retryable=False)
        has_mpeg_frame = len(audio) > 1 and audio[0] == 0xFF and audio[1] & 0xE0 == 0xE0
        if not audio.startswith(b"ID3") and not has_mpeg_frame:
            raise ProviderError("PROVIDER_INVALID_RESULT", "ElevenLabs returned an invalid MP3 response.", retryable=False)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(audio)
        return {"provider": self.name, "model": self._model_id, "voice_id": self._voice_id, "provider_character_cost": usage}


@dataclass(frozen=True)
class MockMediaProvider:
    """Deterministic offline test audio and generated color cards, never stock."""

    name: str = "mock-local"
    version: str = "2"

    def narration_duration(self, script: str) -> float:
        word_count = max(1, len(script.split()))
        return max(2.0, word_count / 2.5)

    def scene_color(self, index: int) -> str:
        # Golden-angle hues make each scene visually distinct without a fixed
        # five-color cycle (which would repeat on longer videos).
        hue = (index * 0.61803398875) % 1
        red, green, blue = colorsys.hsv_to_rgb(hue, 0.58, 0.43)
        return "0x{:02x}{:02x}{:02x}".format(round(red * 255), round(green * 255), round(blue * 255))

    def select_visual(self, scene_text: str, index: int, job_id: str) -> SceneVisual:
        return self.visual_options(scene_text, index, job_id)[0]

    def visual_options(self, scene_text: str, index: int, job_id: str, generation: int = 0) -> list[SceneVisual]:
        """Offer distinct offline cards without claiming semantic relevance."""
        options: list[SceneVisual] = []
        for option_index in range(3):
            color_index = index + (option_index + generation * 3) * 7919
            color = self.scene_color(color_index)
            options.append(SceneVisual(
                asset_id=f"mock-color-{job_id}-{index + 1}-option-{option_index + 1}-g{generation}",
                color=color,
                asset_path=None,
                provider=self.name,
                source="FFmpeg lavfi color generator",
                source_url=None,
                license="Generated locally; no third-party media licence applies; not stock footage",
                attribution="Generated locally by the SceneCraft Studio mock provider",
                ai_generated=False,
                selection_reason="Offline generated-color fallback option; scene text is not semantically matched",
                relevance_score=None,
                warnings=("Mock generated visual; not genuine footage or stock media",),
            ))
        return options

    @property
    def speech_label(self) -> str:
        return "mock_sine_test_tone"


MOCK_PROVIDER = MockMediaProvider()
