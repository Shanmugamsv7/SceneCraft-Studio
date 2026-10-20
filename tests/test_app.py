from __future__ import annotations

import time
import json
import subprocess
import asyncio
import io
import logging
import uuid
import threading
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import pytest
import yaml

from fastapi.testclient import TestClient

from app import main


client = TestClient(main.app)


def wait_for_job(job_id: str) -> dict:
    for _ in range(100):
        response = client.get(f"/api/v1/jobs/{job_id}")
        assert response.status_code == 200
        job = response.json()
        if job["status"] in {"succeeded", "failed"}:
            return job
        time.sleep(0.1)
    raise AssertionError("job did not finish in time")


def test_health_and_home() -> None:
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/").status_code == 200
    assert client.get("/api/v1/docs").status_code == 200
    assert client.get("/api/v1/openapi.json").json()["openapi"] == "3.1.0"


def test_deployment_access_token_protects_interactive_routes_not_health(monkeypatch) -> None:
    from dataclasses import replace
    import base64

    monkeypatch.setattr(main, "settings", replace(main.settings, access_token="test-deployment-token"))
    assert client.get("/").status_code == 401
    assert client.get("/static/app.js").status_code == 401
    assert client.get("/health").status_code == 200
    assert client.get("/ready").status_code != 401
    header = base64.b64encode(b"scenecraft:test-deployment-token").decode("ascii")
    assert client.get("/", headers={"Authorization": f"Basic {header}"}).status_code == 200
    assert client.get("/", headers={"Authorization": "Bearer test-deployment-token"}).status_code == 200


def test_editing_workspace_and_assets_are_served() -> None:
    page = client.get("/")
    assert page.status_code == 200
    markup = page.text
    for control in ("script", "audio", "scene-list", "preview-video", "render-button", "export video"):
        assert control in markup.lower()
    assert 'id="draft-preview-button"' in markup
    assert 'id="preview-mode-bar"' in markup
    assert client.get("/static/styles.css").status_code == 200
    script = client.get("/static/app.js")
    assert script.status_code == 200
    assert "/api/v1/jobs/${currentJobId}/scenes/${scene.scene_id}" in script.text
    assert 'status.mode === "demo_test_tone"' in script.text
    assert "narration ready" in script.text
    assert 'api("/api/v1/providers")' in script.text
    assert "const baseUrl = new URL(result.video, window.location.href);" in script.text
    assert "video.src = nextVideoUrl" in script.text
    assert "_sc_export" in script.text
    assert 'id="preview-load-error"' in markup
    styles = client.get("/static/styles.css").text
    assert "[hidden]{display:none!important}" in styles
    assert ".preview-empty{aspect-ratio:auto;min-height:86px;padding:14px}" in styles
    assert "addEventListener(\"loadeddata\"" in script.text
    assert "addEventListener(\"error\"" in script.text
    assert "const saved = await saveEdits();" in script.text
    assert "if (!saved) return;" in script.text
    assert "if (state.kind === \"stale\")" in script.text
    assert "Searching alternatives…" in script.text
    assert "No new eligible alternatives were found." in script.text
    assert "Alternative search failed" in script.text
    assert "setCaptionTrack(captions, expectedVideoUrl)" in script.text
    assert '<track id="caption-track"' in markup
    assert markup.count("<video ") == 2
    assert "find-alternatives-button[data-scene-id]" in script.text
    assert 'regenerate.dataset.sceneId = scene.scene_id' in script.text
    assert "button.disabled = !scene || searching || working" in script.text
    assert "SceneCraftPreview.selectMainPreview" in script.text
    assert "Rendered video" in markup
    assert 'id="source-preview-image"' in markup
    assert '"SOURCE IMAGE PREVIEW - NOT THE RENDERED VIDEO"' in script.text
    assert '"SOURCE CLIP PREVIEW - NOT THE RENDERED VIDEO"' in script.text
    assert 'id="source-preview-video"' in markup
    assert 'id="add-scene-button"' in markup
    assert '"Previous export - edits need rendering"' in script.text
    assert "source-preview.append(sourceLabel, sourceImage)" not in script.text
    assert 'src="/static/preview-state.js"' in markup
    assert "Accept selected ${selectedForAccept?.media_type === \"image\" ? \"image\" : \"video\"}" in script.text
    assert 'href="/api/v1/docs"' not in markup
    assert "/api/v1/jobs/${currentJobId}/draft-preview" in script.text
    assert 'id="download-all-captions"' in markup
    assert "/captions.srt" in script.text
    assert "/visuals/${encodeURIComponent(previewVisual.id)}/preview" in script.text


def test_job_creation_status_and_artifacts(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(main, "DATA_DIR", tmp_path)
    response = client.post("/api/v1/broll/jobs", data={"script": "Hello world. This is a test.", "aspect_ratio": "16:9"})
    assert response.status_code == 202
    initial = response.json()
    assert initial["status"] == "queued"
    finished = wait_for_job(initial["job_id"])
    assert finished["status"] == "succeeded", finished["error"]
    assert finished["progress"] == 100
    for name, url in finished["result"].items():
        artifact = client.get(url)
        assert artifact.status_code == 200, name
        if name == "video":
            video_path = tmp_path / "rendered.mp4"
            video_path.write_bytes(artifact.content)
            probe = subprocess.run(
                    [main._ffprobe_path(), "-v", "error", "-show_entries", "format=duration:stream=codec_type,codec_name,width,height,avg_frame_rate,duration", "-of", "json", str(video_path)],
                capture_output=True,
                text=True,
                check=True,
            )
            streams = json.loads(probe.stdout)["streams"]
            assert {stream["codec_type"]: stream["codec_name"] for stream in streams} == {"video": "h264", "audio": "aac"}
            video_stream = next(stream for stream in streams if stream["codec_type"] == "video")
            audio_stream = next(stream for stream in streams if stream["codec_type"] == "audio")
            assert (video_stream["width"], video_stream["height"]) == (1920, 1080)
            assert video_stream["avg_frame_rate"] == "30/1"
            assert abs(float(video_stream["duration"]) - float(audio_stream["duration"])) <= (1 / main.FRAME_RATE)
    manifest = client.get(finished["result"]["manifest"]).json()
    assert "not genuine footage" in manifest["warnings"][0]


def test_audio_upload_job_renders_with_uploaded_audio(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(main, "DATA_DIR", tmp_path)
    audio_path = tmp_path / "tone.wav"
    subprocess.run(
        [main._ffmpeg_path(), "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=520:duration=2", str(audio_path)],
        check=True,
    )
    response = client.post(
        "/api/v1/broll/jobs",
        data={"aspect_ratio": "1:1"},
        files={"audio": ("voice.wav", audio_path.read_bytes(), "audio/wav")},
    )
    assert response.status_code == 202
    job = wait_for_job(response.json()["job_id"])
    assert job["status"] == "succeeded", job["error"]
    assert client.get(job["result"]["video"]).status_code == 200
    assert client.get(f"/api/v1/jobs/{job['job_id']}/artifacts/input.wav").status_code == 404
    assert not list((tmp_path / job["job_id"]).glob("input.*"))
    assert not (tmp_path / job["job_id"] / "mock_narration.wav").exists()
    manifest = client.get(job["result"]["manifest"]).json()
    assert manifest["provider"]["speech"] == "user_upload"


@pytest.mark.parametrize(("ratio", "expected_width", "expected_height"), [("9:16", 1080, 1920), ("1:1", 1080, 1080)])
def test_aspect_ratio_dimensions(tmp_path, monkeypatch, ratio, expected_width, expected_height) -> None:
    monkeypatch.setattr(main, "DATA_DIR", tmp_path)
    response = client.post("/api/v1/broll/jobs", data={"script": "One short scene.", "aspect_ratio": ratio})
    job = wait_for_job(response.json()["job_id"])
    assert job["status"] == "succeeded", job["error"]
    video = client.get(job["result"]["video"])
    video_path = tmp_path / "ratio.mp4"
    video_path.write_bytes(video.content)
    probe = subprocess.run(
        [main._ffprobe_path(), "-v", "error", "-show_entries", "format=duration:stream=codec_type,codec_name,width,height,avg_frame_rate,duration", "-of", "json", str(video_path)],
        capture_output=True,
        text=True,
        check=True,
    )
    result = json.loads(probe.stdout)
    stream = next(stream for stream in result["streams"] if stream["codec_type"] == "video")
    assert (stream["width"], stream["height"]) == (expected_width, expected_height)
    assert stream["avg_frame_rate"] == "30/1"
    audio = next(stream for stream in result["streams"] if stream["codec_type"] == "audio")
    assert stream["codec_name"] == "h264"
    assert audio["codec_name"] == "aac"
    assert abs(float(stream["duration"]) - float(audio["duration"])) <= (1 / main.FRAME_RATE)
    timeline = client.get(job["result"]["timeline"]).json()
    manifest = client.get(job["result"]["manifest"]).json()
    assert abs(timeline["duration_seconds"] - float(result["format"]["duration"])) <= 0.001
    assert manifest["settings"]["aspect_ratio"] == ratio


def test_scene_timing_provenance_and_captions_match_render(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(main, "DATA_DIR", tmp_path)
    script = "First sentence has a clear boundary. Here is another useful phrase, with a pause. The final short line ends it."
    response = client.post("/api/v1/broll/jobs", data={"script": script, "aspect_ratio": "16:9"})
    job = wait_for_job(response.json()["job_id"])
    assert job["status"] == "succeeded", job["error"]
    timeline = client.get(job["result"]["timeline"]).json()
    manifest = client.get(job["result"]["manifest"]).json()
    captions = client.get(job["result"]["captions"]).text
    scenes = timeline["scenes"]
    assert len(scenes) == 3
    assert scenes[0]["start"] == 0
    assert all(left["end"] == right["start"] for left, right in zip(scenes, scenes[1:]))
    assert scenes[-1]["end"] == timeline["duration_seconds"]
    assert scenes[-1]["end_frame"] > scenes[-1]["start_frame"]
    assert len({scene["clip"] for scene in scenes}) == len(scenes)
    for scene in scenes:
        assert scene["source_url"] is None
        assert "not stock footage" in scene["license"]
        assert scene["ai_generated"] is False
        assert scene["selection_reason"]
        assert scene["relevance_score"] is None
    assert "not genuine footage" in " ".join(manifest["warnings"]).lower()
    assert manifest["rights"]["unverified_assets_used"] is False
    assert "00:00:00,000 -->" in captions
    final_caption_time = captions.strip().split("\n")[-2].split(" --> ")[1].strip()
    assert final_caption_time == main._srt_timestamp(timeline["duration_seconds"])
    video_path = tmp_path / "duration-check.mp4"
    video_path.write_bytes(client.get(job["result"]["video"]).content)
    measured = main._probe_render(video_path)["duration"]
    assert abs(measured - timeline["duration_seconds"]) <= 0.001
    assert abs(manifest["timings"]["rendered_duration_seconds"] - measured) <= 0.001


def test_render_has_no_black_frames_and_is_not_a_single_frozen_frame(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(main, "DATA_DIR", tmp_path)
    accepted = client.post("/api/v1/broll/jobs", data={"script": "Moving shapes keep the mock preview alive."}).json()
    job = wait_for_job(accepted["job_id"])
    assert job["status"] == "succeeded", job["error"]
    output = tmp_path / job["job_id"] / "checked.mp4"
    output.write_bytes(client.get(job["result"]["video"]).content)
    black_check = subprocess.run(
        [main._ffmpeg_path(), "-hide_banner", "-i", str(output), "-vf", "blackdetect=d=0.02:pix_th=0.05", "-an", "-f", "null", "-"],
        capture_output=True, text=True, check=True,
    )
    assert "black_start:" not in black_check.stderr
    frame_hashes = subprocess.run(
        [main._ffmpeg_path(), "-v", "error", "-i", str(output), "-vf", "fps=2", "-frames:v", "4", "-f", "framemd5", "-"],
        capture_output=True, text=True, check=True,
    ).stdout
    hashes = [line.rsplit(",", 1)[-1].strip() for line in frame_hashes.splitlines() if line and not line.startswith("#")]
    assert len(hashes) >= 3
    assert len(set(hashes)) > 1


def test_very_long_sentence_is_split_at_phrase_boundaries() -> None:
    from app.pipeline import MAX_SCENE_WORDS, allocate_scene_timings, segment_script

    script = " ".join(f"word{i}," for i in range(75)) + " and then the ending."
    scenes = segment_script(script)
    assert len(scenes) > 1
    assert max(len(scene.split()) for scene in scenes) <= MAX_SCENE_WORDS
    timings = allocate_scene_timings(scenes, 900)
    assert timings[0].start_frame == 0
    assert timings[-1].end_frame == 900
    assert all(left.end_frame == right.start_frame for left, right in zip(timings, timings[1:]))


def test_measured_speech_segment_durations_drive_contiguous_caption_frames() -> None:
    from app.pipeline import allocate_scene_timings_by_duration

    timings = allocate_scene_timings_by_duration(["A short line.", "A longer second line."], [0.4, 1.1])
    assert timings[0].start_frame == 0
    assert timings[0].end_frame == timings[1].start_frame
    assert timings[0].end == pytest.approx(0.4)
    assert timings[-1].end_frame == 45
    assert timings[-1].end == pytest.approx(1.5)


@pytest.mark.parametrize("script", [
    "What does a promise mean when nobody remembers making it?",
    "I saw her duck near the river bank, then the duck disappeared.",
    "Fictional host Maya Rao visits the fictional Acme workshop.",
    "This fictional story discusses grief, recovery, and asking for help.",
])
def test_abstract_ambiguous_named_and_sensitive_scripts_are_safe_inputs(script) -> None:
    from app.pipeline import segment_script

    scenes = segment_script(script)
    assert scenes
    assert " ".join(scenes).replace("  ", " ") == script
    for index, scene in enumerate(scenes):
        visual = main.MOCK_PROVIDER.select_visual(scene, index, "test-job")
        assert visual.selection_reason.startswith("Offline generated-color fallback")
        assert visual.relevance_score is None
        assert "not stock footage" in visual.license


def test_smartphone_script_gets_action_specific_visual_queries_and_rejects_topic_only_match() -> None:
    from app.pipeline import analyze_scene, scene_search_phrases, score_keyword_overlap, score_scene_relevance, segment_script

    script = (
        "Every morning, millions of people begin their day by checking their phones. "
        "Technology has become an essential part of our daily lives, changing how we communicate, learn, work, and make decisions."
    )
    scenes = segment_script(script)
    assert len(scenes) == 2
    assert scenes[0] == "Every morning, millions of people begin their day by checking their phones."
    assert scenes[1].startswith("Technology has become an essential part")
    first_queries = scene_search_phrases(scenes[0])
    assert first_queries[0] == "people checking smartphones"
    assert "scientist" not in " ".join(first_queries)
    assert analyze_scene(scenes[0])["action"] == "checking or browsing on the phone"
    second_queries = scene_search_phrases(scenes[1], limit=4)
    assert any("communicating" in query for query in second_queries)
    assert any("learning" in query for query in second_queries)
    assert any("working" in query for query in second_queries)
    assert any("decision" in query for query in second_queries)
    relevant = score_scene_relevance(first_queries[0], "People checking smartphones on a morning commute")
    unrelated = score_scene_relevance(first_queries[0], "Older scientist giving an interview about technology")
    building = score_scene_relevance(first_queries[0], "Modern office building with glass facade")
    conference_overlap = score_keyword_overlap(first_queries[0], "Conference attendees checking schedules at a courthouse")
    conference_relevance = score_scene_relevance(first_queries[0], "Conference attendees checking schedules at a courthouse", scene_context=scenes[0])
    assert relevant > unrelated
    assert unrelated < 0.45
    assert relevant > building
    assert building <= 0.24
    assert conference_overlap > conference_relevance
    assert conference_relevance <= 0.24
    work_query = next(query for query in second_queries if "working" in query)
    assert score_scene_relevance(work_query, "Modern office building with glass facade") < 0.45


def test_smartphone_ranking_keeps_generic_building_weak_for_commons_video_and_image() -> None:
    from app.providers import WikimediaCommonsFootageProvider, WikimediaCommonsImageProvider

    phone = {
        "pageid": 201, "title": "File:Person checking a smartphone.jpg",
        "imageinfo": [{"url": "https://upload.wikimedia.org/wikipedia/commons/a/a1/person.jpg",
            "thumburl": "https://upload.wikimedia.org/wikipedia/commons/thumb/a/a1/person.jpg/480px-person.jpg",
            "size": 1024, "mime": "image/jpeg", "mediatype": "BITMAP",
            "extmetadata": {"LicenseShortName": {"value": "CC BY 4.0"}, "LicenseUrl": {"value": "https://creativecommons.org/licenses/by/4.0/"}, "Artist": {"value": "Test Creator"}, "ImageDescription": {"value": "A person checking a smartphone."}}}],
    }
    building = {
        "pageid": 202, "title": "File:Modern office building.jpg",
        "imageinfo": [{"url": "https://upload.wikimedia.org/wikipedia/commons/b/b2/building.jpg",
            "thumburl": "https://upload.wikimedia.org/wikipedia/commons/thumb/b/b2/building.jpg/480px-building.jpg",
            "size": 1024, "mime": "image/jpeg", "mediatype": "BITMAP",
            "extmetadata": {"LicenseShortName": {"value": "CC BY 4.0"}, "LicenseUrl": {"value": "https://creativecommons.org/licenses/by/4.0/"}, "Artist": {"value": "Test Creator"}, "ImageDescription": {"value": "A modern office building with a glass facade."}}}],
    }
    conference = json.loads(json.dumps(building))
    conference["pageid"] = 203
    conference["title"] = "File:Conference courthouse checking schedules.jpg"
    conference["imageinfo"][0]["url"] = "https://upload.wikimedia.org/wikipedia/commons/c/c3/conference.jpg"
    conference["imageinfo"][0]["thumburl"] = "https://upload.wikimedia.org/wikipedia/commons/thumb/c/c3/conference.jpg/480px-conference.jpg"
    conference["imageinfo"][0]["extmetadata"]["ImageDescription"]["value"] = "Conference attendees checking schedules outside a courthouse."
    image_provider = WikimediaCommonsImageProvider(Path("unused"), "test@example.org")
    scene = "Every morning, millions of people begin their day by checking their phones."
    image_options = image_provider._parse_image_options([building, conference, phone], "people checking smartphones", set(), scene)
    image_by_id = {option.asset_id: option for option in image_options}
    assert image_by_id["commons-image-201"].relevance_score > image_by_id["commons-image-202"].relevance_score
    assert sorted(image_options, key=lambda option: -(option.relevance_score or 0))[0].asset_id == "commons-image-201"
    assert image_by_id["commons-image-201"].selection_requires_review is False
    assert image_by_id["commons-image-202"].selection_requires_review is True
    assert image_by_id["commons-image-203"].keyword_overlap_score > image_by_id["commons-image-203"].relevance_score
    assert image_by_id["commons-image-203"].relevance_score <= 0.24
    assert image_by_id["commons-image-203"].selection_requires_review is True

    video_pages = []
    for page in (building, conference, phone):
        video_page = json.loads(json.dumps(page))
        video_page["title"] = video_page["title"].removesuffix(".jpg") + ".webm"
        info = video_page["imageinfo"][0]
        info["url"] = info["url"].removesuffix(".jpg") + ".webm"
        info["mime"] = "video/webm"
        info["mediatype"] = "VIDEO"
        video_pages.append(video_page)
    video_provider = WikimediaCommonsFootageProvider(Path("unused"), "test@example.org")
    video_options = video_provider._parse_video_options(video_pages, "people checking smartphones", set(), scene)
    video_by_id = {option.asset_id: option for option in video_options}
    assert video_by_id["commons-202"].relevance_score < video_by_id["commons-201"].relevance_score
    assert sorted(video_options, key=lambda option: -(option.relevance_score or 0))[0].asset_id == "commons-201"
    assert video_by_id["commons-202"].selection_requires_review is True
    assert video_by_id["commons-203"].relevance_score <= 0.24
    assert video_by_id["commons-203"].selection_requires_review is True


def test_courthouse_metadata_cannot_be_strong_for_phone_or_automated_review_scenes() -> None:
    from app.pipeline import scene_search_phrases, score_scene_relevance
    from app.providers import WikimediaCommonsFootageProvider, WikimediaCommonsImageProvider

    phone_scene = "Every morning, millions of people begin their day by checking their phones."
    review_scene = "People must review important decisions, question incorrect results, and understand the limitations of automated systems."
    assert "people reviewing automated system results" in scene_search_phrases(review_scene)
    courthouse_image = {
        "pageid": 902,
        "title": "File:La Cour D'Appel du Québec.jpg",
        "imageinfo": [{
            "url": "https://upload.wikimedia.org/wikipedia/commons/a/a1/courthouse.jpg",
            "thumburl": "https://upload.wikimedia.org/wikipedia/commons/thumb/a/a1/courthouse.jpg/480px-courthouse.jpg",
            "size": 1024, "mime": "image/jpeg", "mediatype": "BITMAP",
            "extmetadata": {
                "LicenseShortName": {"value": "CC BY 4.0"},
                "LicenseUrl": {"value": "https://creativecommons.org/licenses/by/4.0/"},
                "Artist": {"value": "Test Creator"},
                "ImageDescription": {"value": "Courthouse exterior where people review important decisions, question incorrect results, and check legal decisions."},
            },
        }],
    }
    image_provider = WikimediaCommonsImageProvider(Path("unused"), "test@example.org")
    video_provider = WikimediaCommonsFootageProvider(Path("unused"), "test@example.org")

    # Query-only overlap is misleading for both the old broad phone fallback
    # and the abstract decision scene; complete scene context must constrain it.
    phone_fallback = "check"
    assert score_scene_relevance(phone_fallback, "La Cour D'Appel du Québec", "Courthouse exterior where people review important decisions, question incorrect results, and check legal decisions") >= 0.6
    assert score_scene_relevance(phone_fallback, "La Cour D'Appel du Québec", "Courthouse exterior where people review important decisions, question incorrect results, and check legal decisions", phone_scene) <= 0.24
    legacy_decision_query = "must review important decisions question incorrect"
    assert score_scene_relevance(legacy_decision_query, "La Cour D'Appel du Québec", "Courthouse exterior where people review important decisions, question incorrect results, and check legal decisions") >= 0.6
    assert score_scene_relevance(legacy_decision_query, "La Cour D'Appel du Québec", "Courthouse exterior where people review important decisions, question incorrect results, and check legal decisions", review_scene) <= 0.24

    for scene in (phone_scene, review_scene):
        for query in scene_search_phrases(scene):
            image_options = image_provider._parse_image_options([courthouse_image], query, set(), scene)
            assert len(image_options) == 1
            assert image_options[0].relevance_score <= 0.24
            assert image_options[0].selection_requires_review is True
            assert "human review required" in image_options[0].selection_reason

            video_page = json.loads(json.dumps(courthouse_image))
            video_page["title"] = video_page["title"].removesuffix(".jpg") + ".webm"
            info = video_page["imageinfo"][0]
            info["url"] = info["url"].removesuffix(".jpg") + ".webm"
            info["mime"] = "video/webm"
            info["mediatype"] = "VIDEO"
            video_options = video_provider._parse_video_options([video_page], query, set(), scene)
            assert len(video_options) == 1
            assert video_options[0].relevance_score <= 0.24
            assert video_options[0].selection_requires_review is True
            assert "human review required" in video_options[0].selection_reason

    video_page = json.loads(json.dumps(courthouse_image))
    video_page["title"] = video_page["title"].removesuffix(".jpg") + ".webm"
    video_info = video_page["imageinfo"][0]
    video_info["url"] = video_info["url"].removesuffix(".jpg") + ".webm"
    video_info["mime"] = "video/webm"
    video_info["mediatype"] = "VIDEO"
    for provider, page, filetype in (
        (image_provider, courthouse_image, "bitmap"),
        (video_provider, video_page, "video"),
    ):
        queries = scene_search_phrases(phone_scene)
        searched = []
        payload = json.dumps({"query": {"pages": [page]}}).encode()
        provider._search = lambda query, *, filetype=filetype: (searched.append(query), payload)[1]
        options = provider.visual_options_for_ratio(phone_scene, 0, "test-job", 0, "16:9", set())
        assert searched == queries
        assert options[0].relevance_score <= 0.24
        assert options[0].selection_requires_review is True


def test_weak_courthouse_candidate_is_not_accepted_or_rendered_for_phone_scene(tmp_path, monkeypatch):
    from app.pipeline import scene_search_phrases
    from app.providers import LocalFootageLibraryProvider, WikimediaCommonsImageProvider

    scene = "Every morning, millions of people begin their day by checking their phones."
    page = {
        "pageid": 903,
        "title": "File:La Cour D'Appel du Québec.jpg",
        "imageinfo": [{
            "url": "https://upload.wikimedia.org/wikipedia/commons/a/a1/courthouse.jpg",
            "thumburl": "https://upload.wikimedia.org/wikipedia/commons/thumb/a/a1/courthouse.jpg/480px-courthouse.jpg",
            "size": 1024, "mime": "image/jpeg", "mediatype": "BITMAP",
            "extmetadata": {
                "LicenseShortName": {"value": "CC BY 4.0"},
                "LicenseUrl": {"value": "https://creativecommons.org/licenses/by/4.0/"},
                "Artist": {"value": "Test Creator"},
                "ImageDescription": {"value": "Exterior view of a courthouse building, the Court of Appeal of Quebec."},
            },
        }],
    }
    image_provider = WikimediaCommonsImageProvider(Path("unused"), "test@example.org")

    class WeakCourtProvider:
        name = "wikimedia-commons-images"
        version = "test-fixture"

        def visual_options_for_ratio(self, scene_text, index, job_id, generation, ratio, exclude_asset_ids):
            query = scene_search_phrases(scene_text)[0]
            return image_provider._parse_image_options([page], query, exclude_asset_ids, scene_text)

    monkeypatch.setattr(main, "DATA_DIR", tmp_path / "jobs")
    monkeypatch.setattr(main, "LOCAL_FOOTAGE_PROVIDER", LocalFootageLibraryProvider(tmp_path / "empty-library"))
    monkeypatch.setattr(main, "FOOTAGE_PROVIDER", WeakCourtProvider())
    monkeypatch.setattr(type(main.MOCK_PROVIDER), "narration_duration", lambda self, _script: 1.2)

    accepted = client.post("/api/v1/broll/jobs", data={"script": scene})
    assert accepted.status_code == 202
    job = wait_for_job(accepted.json()["job_id"])
    assert job["status"] == "failed"
    assert job["error"]["code"] == "SCENE_RELEVANCE_REVIEW_REQUIRED"
    assert job["result"] is None
    assert job["scenes"][0]["selected_visual_id"] is None
    assert job["scenes"][0]["visual_decision"] == "rejected"
    assert job["scenes"][0]["visual_options"][0]["selection_requires_review"] is True
    assert not (tmp_path / "jobs" / job["job_id"] / "video_16x9.mp4").exists()


def test_preview_state_switches_from_source_to_export_and_marks_stale_edits() -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is needed to exercise the browser preview-state helper")
    script = r'''const assert = require("assert");
const { resolvePreviewState } = require("./app/static/preview-state.js");
const { selectMainPreview, selectCaptionResult } = require("./app/static/preview-state.js");
const scenes = [{scene_id:"scene-1", visual_options:[{id:"image-1", media_type:"image", source:"phone photo", thumbnail_url:"/phone.jpg"}], selected_visual_id:"image-1", visual_decision:"accepted"}];
assert.equal(resolvePreviewState({scenes}).kind, "source");
assert.equal(resolvePreviewState({scenes}).visual.media_type, "image");
assert.equal(resolvePreviewState({scenes, result:{video:"/job/video_16-9.mp4"}}).kind, "rendered");
assert.equal(resolvePreviewState({scenes, result:{video:"/job/video_16-9.mp4"}, stale:true}).kind, "stale");
const draft = {video:"/job/draft.mp4"};
const final = {video:"/job/final.mp4"};
assert.equal(selectMainPreview({draftResult:draft, finalResult:final, activeView:"draft"}).result, draft);
assert.equal(selectMainPreview({draftResult:draft, finalResult:final, activeView:"final"}).result, final);
assert.equal(selectMainPreview({draftResult:null, finalResult:final, activeView:"draft"}).result, final);
assert.equal(selectMainPreview({draftResult:null, finalResult:null, activeView:"draft"}).result, null);
assert.equal(selectMainPreview({draftResult:null, finalResult:null, activeView:"draft"}).kind, null);
const olderDraft = {video:"/job/draft-old.mp4", captions:"/job/draft-old.srt"};
const refreshedDraft = {video:"/job/draft-new.mp4", captions:"/job/draft-new.srt"};
assert.equal(selectCaptionResult({draftResult:refreshedDraft, finalResult:final, activeView:"draft"}), refreshedDraft);
assert.equal(selectCaptionResult({draftResult:olderDraft, finalResult:final, activeView:"final"}), final);
'''
    subprocess.run([node, "-e", script], check=True, capture_output=True, text=True)


def test_refreshed_draft_caption_track_replaces_old_cues() -> None:
    script = (Path(__file__).resolve().parent.parent / "app" / "static" / "app.js").read_text(encoding="utf-8")
    assert "function previewCaptionResult(job)" in script
    assert "selectCaptionResult({ draftResult, finalResult, activeView: activePreviewView })" in script
    assert "function clearCaptionTracks()" in script
    assert 'Array.from(video.textTracks).forEach((textTrack) => { textTrack.mode = "disabled"; });' in script
    assert 'video.querySelectorAll("track").forEach((track) => track.remove());' in script
    assert "clearCaptionTracks();" in script
    assert 'track.addEventListener("load", () => {' in script
    assert 'track.track.mode = "showing";' in script
    assert 'track.track.mode = "hidden";' in script
    assert "const playbackTime = video.currentTime;" in script
    assert "video.currentTime = playbackTime;" in script
    assert "video.dataset.exportUrl !== expectedVideoUrl" in script
    assert "!track.isConnected" in script
    assert "video.load();" not in script[script.index("function setCaptionTrack"):script.index("async function loadCaptionsForPreview")]
    assert "await loadCaptionsForPreview(previewCaptionResult(job));" in script
    assert "if (requestId !== captionRequestId) return;" in script
    assert "captionRequestId += 1;" in script[script.index("function clearCaptionTracks"):script.index("function setCaptionTrack")]


def test_local_matching_does_not_select_a_scientist_interview_for_phone_scene(tmp_path) -> None:
    from app.providers import LocalFootageLibraryProvider

    library = tmp_path / "library"
    library.mkdir()
    rights = {"license": "Test fixture only; not licensed stock footage", "attribution": "Synthetic test", "rights_confirmed": True}
    for filename in ("older_scientist_interview.mp4", "person_checking_smartphones.mp4"):
        clip = library / filename
        clip.write_bytes(b"test-fixture")
        LocalFootageLibraryProvider.sidecar_path(clip).write_text(json.dumps(rights), encoding="utf-8")
    provider = LocalFootageLibraryProvider(library)
    options = provider.visual_options("Every morning people begin by checking their phones.", 0, "job-phone")
    assert [item.source.rsplit(" ", 1)[-1] for item in options] == ["person_checking_smartphones.mp4"]
    assert options[0].relevance_score == 1.0


def test_caption_chunks_wrap_long_text_without_losing_words() -> None:
    from app.pipeline import caption_chunks

    text = "People check their smartphones before starting work and use technology to communicate, learn, and make decisions throughout the day."
    chunks = caption_chunks(text)
    assert len(chunks) >= 2
    assert all(len(line) <= 42 for cue in chunks for line in cue.splitlines())
    assert all(1 <= len(cue.splitlines()) <= 2 for cue in chunks)
    assert " ".join(" ".join(chunks).split()) == text


def test_burn_captions_uses_libass_filter_and_keeps_audio(tmp_path, monkeypatch) -> None:
    source = tmp_path / "muxed.mp4"
    captions = tmp_path / "captions.srt"
    output = tmp_path / "video.mp4"
    source.write_bytes(b"source")
    captions.write_text("1\n00:00:00,000 --> 00:00:01,000\nCurrent final caption.\n", encoding="utf-8")
    commands = []

    def fake_run(command, **kwargs):
        commands.append((command, kwargs))
        if "-filters" not in command:
            Path(command[-1]).write_bytes(b"rendered")
        return subprocess.CompletedProcess(command, 0, stdout=" .. subtitles         V->V       Render text subtitles onto input video using the libass library.\n", stderr="")

    monkeypatch.setattr(main, "_run", fake_run)
    main._burn_captions_into_video("ffmpeg", source, captions, output, 1080)

    command, kwargs = commands[-1]
    assert "-vf" in command
    assert "subtitles=filename='captions.srt':charenc=UTF-8" in command[command.index("-vf") + 1]
    assert "Alignment=2" in command[command.index("-vf") + 1]
    assert command[command.index("-map") + 1] == "0:v:0"
    assert "0:a:0" in command
    assert command[command.index("-c:a") + 1] == "copy"
    assert kwargs["cwd"] == tmp_path
    assert kwargs["stage"] == "burn captions"


def test_burn_captions_resolves_source_before_changing_working_directory(tmp_path, monkeypatch) -> None:
    render_dir = tmp_path / "job" / ".rendering"
    render_dir.mkdir(parents=True)
    source = render_dir / "muxed.mp4"
    captions = render_dir / "captions.srt"
    output = render_dir / "video.mp4"
    source.write_bytes(b"source")
    captions.write_text("1\n00:00:00,000 --> 00:00:01,000\nCaption\n", encoding="utf-8")
    commands = []

    def fake_run(command, **kwargs):
        commands.append((command, kwargs))
        if "-filters" not in command:
            Path(command[-1]).write_bytes(b"rendered")
        return subprocess.CompletedProcess(command, 0, stdout=" .. subtitles         V->V\n", stderr="")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(main, "_run", fake_run)
    main._burn_captions_into_video("ffmpeg", Path("job/.rendering/muxed.mp4"), Path("job/.rendering/captions.srt"), Path("job/.rendering/video.mp4"), 720)

    command, kwargs = commands[-1]
    assert command[command.index("-i") + 1] == str(source.resolve())
    assert command[-1] == str(output.resolve())
    assert kwargs["cwd"] == render_dir.resolve()


def test_missing_ffmpeg_intermediate_is_reported_explicitly(tmp_path) -> None:
    with pytest.raises(RuntimeError, match="mux audio and video.*non-empty file"):
        main._require_nonempty_file(tmp_path / "muxed.mp4", "mux audio and video")


def test_ffmpeg_failure_includes_stage_and_stderr(monkeypatch) -> None:
    def fail_run(*args, **kwargs):
        raise subprocess.CalledProcessError(1, args[0], stderr="Error opening input file muxed.mp4.")

    monkeypatch.setattr(main.subprocess, "run", fail_run)
    with pytest.raises(RuntimeError, match=r"during mux audio and video.*Error opening input file muxed\.mp4"):
        main._run(["ffmpeg", "-i", "muxed.mp4"], stage="mux audio and video")


def test_burn_captions_explains_missing_subtitles_filter(tmp_path, monkeypatch) -> None:
    captions = tmp_path / "captions.srt"
    captions.write_text("1\n00:00:00,000 --> 00:00:01,000\nCaption\n", encoding="utf-8")
    monkeypatch.setattr(main, "_ffmpeg_supports_subtitles", lambda _ffmpeg: False)
    with pytest.raises(RuntimeError, match="enable-libass"):
        main._burn_captions_into_video("ffmpeg", tmp_path / "muxed.mp4", captions, tmp_path / "video.mp4", 720)


def test_merged_scene_captions_srt_uses_absolute_times_and_current_text() -> None:
    from app.main import _merged_scene_captions_srt

    captions = _merged_scene_captions_srt([
        {"start": 5.25, "end": 7.0, "caption_text": "Repeated sentence."},
        {"start": 0.0, "end": 2.5, "caption_text": "First line has enough words to wrap cleanly onto another caption line."},
        {"start": 7.0, "end": 8.75, "caption_text": "Repeated sentence."},
        {"start": 2.5, "end": 5.25, "caption_text": "Latest saved caption."},
    ])

    blocks = captions.strip().split("\n\n")
    assert [block.splitlines()[0] for block in blocks] == ["1", "2", "3", "4"]
    assert blocks[0].splitlines()[1] == "00:00:00,000 --> 00:00:02,500"
    assert len(blocks[0].splitlines()) == 4
    assert " ".join(blocks[0].splitlines()[2:]) == "First line has enough words to wrap cleanly onto another caption line."
    assert blocks[1].splitlines()[1] == "00:00:02,500 --> 00:00:05,250"
    assert "Latest saved caption." in blocks[1]
    assert captions.count("Repeated sentence.") == 2
    assert "00:00:05,250 --> 00:00:07,000" in blocks[2]
    assert "00:00:07,000 --> 00:00:08,750" in blocks[3]


def test_merged_scene_captions_srt_skips_only_exact_duplicate_cues_and_empty_captions() -> None:
    from app.main import _merged_scene_captions_srt

    assert _merged_scene_captions_srt([]) == ""
    assert _merged_scene_captions_srt([{"start": 0, "end": 1, "caption_text": "   "}]) == ""
    captions = _merged_scene_captions_srt([
        {"start": 0, "end": 1, "caption_text": "Same cue."},
        {"start": 0, "end": 1, "caption_text": "Same cue."},
        {"start": 1, "end": 2, "caption_text": "Same cue."},
    ])
    assert captions.count("Same cue.") == 2
    assert [block.splitlines()[0] for block in captions.strip().split("\n\n")] == ["1", "2"]


def test_current_scene_caption_download_uses_saved_edit_not_previous_artifact(tmp_path, monkeypatch) -> None:
    job = _new_draft(tmp_path, monkeypatch, script="Original caption text.")
    scene = job["scenes"][0]
    updated = client.patch(
        f"/api/v1/jobs/{job['job_id']}/scenes/{scene['scene_id']}",
        json={"caption_text": "Latest saved caption text."},
    )
    assert updated.status_code == 200

    response = client.get(f"/api/v1/jobs/{job['job_id']}/captions.srt")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/x-subrip")
    assert 'filename="scenecraft-captions.srt"' in response.headers["content-disposition"]
    assert "Latest saved caption text." in response.text
    assert "Original caption text." not in response.text


def test_current_scene_caption_download_reports_when_no_captions_exist() -> None:
    job_id = str(uuid.uuid4())
    with main.jobs_lock:
        main.jobs[job_id] = {"job_id": job_id, "status": "succeeded", "scenes": []}
    try:
        response = client.get(f"/api/v1/jobs/{job_id}/captions.srt")
        assert response.status_code == 404
        assert response.json()["detail"]["error"]["code"] == "CAPTIONS_UNAVAILABLE"
    finally:
        with main.jobs_lock:
            main.jobs.pop(job_id, None)


def test_wikimedia_image_options_preserve_creator_license_and_review_weak_matches(tmp_path) -> None:
    from app.providers import FallbackFootageProvider, LocalFootageLibraryProvider, WikimediaCommonsImageProvider

    provider = WikimediaCommonsImageProvider(tmp_path, "contact@example.org", min_request_interval=0)
    pages = [{"pageid": 744, "title": "File:Person checking a smartphone.jpg", "imageinfo": [{
        "url": "https://upload.wikimedia.org/wikipedia/commons/a/a1/Person_checking_a_smartphone.jpg",
        "thumburl": "https://upload.wikimedia.org/wikipedia/commons/thumb/a/a1/Person_checking_a_smartphone.jpg/480px-Person_checking_a_smartphone.jpg",
        "size": 4096, "mime": "image/jpeg", "mediatype": "BITMAP",
        "extmetadata": {
            "LicenseShortName": {"value": "CC BY 4.0"},
            "LicenseUrl": {"value": "https://creativecommons.org/licenses/by/4.0/"},
            "Artist": {"value": "<a>Alex Creator</a>"},
            "ImageDescription": {"value": "A person checking a smartphone."},
        },
    }]}]
    options = provider._parse_image_options(pages, "people checking smartphones", set())
    assert len(options) == 1
    visual = options[0]
    assert visual.media_type == "image"
    assert visual.provider == "wikimedia-commons-images"
    assert visual.license.startswith("CC BY 4.0")
    assert visual.attribution == "Alex Creator"
    assert visual.source_url == "https://commons.wikimedia.org/wiki/File:Person_checking_a_smartphone.jpg"
    assert visual.rights_basis == "commons_file_metadata"
    assert visual.license_verified is False
    assert visual.selection_requires_review is False
    pages[0]["imageinfo"][0]["extmetadata"].pop("Artist")
    assert provider._parse_image_options(pages, "people checking smartphones", set()) == []

    class NoVideoResults:
        name = "no-video"
        version = "test"

        def visual_options_for_ratio(self, *args):
            return []

    class OneImageResult:
        def visual_options_for_ratio(self, *args):
            return [visual]

    fallback = FallbackFootageProvider(NoVideoResults(), LocalFootageLibraryProvider(tmp_path / "empty"), OneImageResult())
    options = fallback.visual_options_for_ratio("people checking smartphones", 0, "job-2", 0, "16:9", set())
    assert options[0].media_type == "image"
    assert options[0].source_url == visual.source_url


def test_commons_image_and_video_adapters_share_rate_limit_cooldown(tmp_path, monkeypatch) -> None:
    from app.providers import CommonsRequestLimiter, ProviderError, WikimediaCommonsFootageProvider, WikimediaCommonsImageProvider

    limiter = CommonsRequestLimiter()
    limiter.cooldown_until = time.monotonic() + 5
    video = WikimediaCommonsFootageProvider(tmp_path, "contact@example.org", request_limiter=limiter)
    images = WikimediaCommonsImageProvider(tmp_path, "contact@example.org", request_limiter=limiter)
    calls = []
    monkeypatch.setattr("app.providers.urlopen", lambda *args, **kwargs: calls.append(args))
    with pytest.raises(ProviderError) as error:
        images._search("smartphone", filetype="bitmap")
    assert error.value.code == "PROVIDER_RATE_LIMITED"
    assert error.value.retryable is True
    assert calls == []
    assert video._request_limiter is images._request_limiter


def test_image_scene_uses_pan_zoom_and_keeps_image_provenance(tmp_path, monkeypatch) -> None:
    from app.providers import SceneVisual

    image_path = tmp_path / "synthetic-test-image.png"
    main._run([main._ffmpeg_path(), "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=teal:s=640x360", "-frames:v", "1", str(image_path)])
    visual = SceneVisual(
        asset_id="synthetic-image-1", color="black", asset_path=None, provider="synthetic-image-test",
        source="Locally generated test image", source_url="https://example.invalid/test-image",
        license="Test fixture only; not licensed stock footage", attribution="Generated locally for this test",
        ai_generated=True, selection_reason="Deterministic renderer test fixture", relevance_score=None,
        download_url="https://example.invalid/test-image.png", rights_basis="user_attestation", media_type="image",
    )

    class ImageFixtureProvider:
        name = "synthetic-image-test"
        version = "test"

        def visual_options_for_ratio(self, scene_text, index, job_id, generation, ratio, exclude_asset_ids):
            return [visual]

        def materialize(self, selected, job_id):
            return SceneVisual(**{**selected.__dict__, "asset_path": image_path})

    monkeypatch.setattr(main, "DATA_DIR", tmp_path / "jobs")
    monkeypatch.setattr(main, "FOOTAGE_PROVIDER", ImageFixtureProvider())
    monkeypatch.setattr(type(main.MOCK_PROVIDER), "narration_duration", lambda self, _script: 0.6)
    accepted = client.post("/api/v1/broll/jobs", data={"script": "A quiet test image gains subtle motion."}).json()
    job = wait_for_job(accepted["job_id"])
    assert job["status"] == "succeeded", job["error"]
    timeline = client.get(job["result"]["timeline"]).json()
    manifest = client.get(job["result"]["manifest"]).json()
    assert timeline["scenes"][0]["media_type"] == "image"
    assert timeline["scenes"][0]["source_url"] == visual.source_url
    assert timeline["scenes"][0]["license"] == visual.license
    assert timeline["scenes"][0]["attribution"] == visual.attribution
    assert manifest["assets"][0]["media_type"] == "image"
    mp4 = tmp_path / "image-scene.mp4"
    mp4.write_bytes(client.get(job["result"]["video"]).content)
    probe = main._probe_render(mp4)
    assert probe["video"]["codec_name"] == "h264"
    assert probe["audio"]["codec_name"] == "aac"
    assert probe["video"]["width"] == 1920
    assert probe["video"]["height"] == 1080
    assert probe["duration"] == pytest.approx(timeline["duration_seconds"], abs=1 / 30)


def test_uploaded_audio_does_not_claim_transcription(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(main, "DATA_DIR", tmp_path)
    audio_path = tmp_path / "short.wav"
    subprocess.run([main._ffmpeg_path(), "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=500:duration=1", str(audio_path)], check=True)
    response = client.post("/api/v1/broll/jobs", data={"aspect_ratio": "16:9"}, files={"audio": ("voice.wav", audio_path.read_bytes(), "audio/wav")})
    job = wait_for_job(response.json()["job_id"])
    assert job["status"] == "succeeded", job["error"]
    timeline = client.get(job["result"]["timeline"]).json()
    manifest = client.get(job["result"]["manifest"]).json()
    assert timeline["scenes"][0]["narration_text"] is None
    assert "Transcript unavailable" in timeline["scenes"][0]["caption_text"]
    assert any("not transcribed" in warning.lower() for warning in manifest["warnings"])


def test_render_command_failure_marks_job_failed_without_success_artifacts(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(main, "DATA_DIR", tmp_path)
    original_run = main._run

    def fail_render(command, **kwargs):
        if "color=c=" in " ".join(command):
            raise RuntimeError("simulated FFmpeg render failure")
        return original_run(command, **kwargs)

    monkeypatch.setattr(main, "_run", fail_render)
    response = client.post("/api/v1/broll/jobs", data={"script": "A render failure is reported."})
    job = wait_for_job(response.json()["job_id"])
    assert job["status"] == "failed"
    assert job["error"]["code"] == "PROCESSING_FAILED"
    assert "simulated FFmpeg render failure" in job["error"]["message"]
    assert job["result"] is None


def test_missing_selected_footage_asset_is_rejected(tmp_path, monkeypatch) -> None:
    from app.providers import SceneVisual

    monkeypatch.setattr(main, "DATA_DIR", tmp_path)

    class MissingFootage:
        name = "test-file-provider"
        version = "test"

        def select_visual(self, scene_text, index, job_id):
            return SceneVisual(
                asset_id=f"missing-{index}", color="0x112233", asset_path=tmp_path / "missing.mp4",
                provider=self.name, source="test asset", source_url="https://media.example.invalid/source",
                license="Test-only licence metadata", attribution="Test-only attribution", ai_generated=False,
                selection_reason="test missing-file path", relevance_score=None, license_verified=True,
            )

    monkeypatch.setattr(main, "FOOTAGE_PROVIDER", MissingFootage())
    response = client.post("/api/v1/broll/jobs", data={"script": "Missing asset test."})
    job = wait_for_job(response.json()["job_id"])
    assert job["status"] == "failed"
    assert "asset is missing" in job["error"]["message"]
    assert job["error"]["details"]["affected_scenes"][0]["message"] == "Selected footage asset is missing: missing-0"
    assert job["result"] is None


def test_missing_and_conflicting_input_are_rejected() -> None:
    missing = client.post("/api/v1/broll/jobs", data={})
    assert missing.status_code == 422
    assert missing.json()["detail"]["error"]["code"] == "INPUT_REQUIRED"
    assert set(missing.json()["detail"]["error"]) == {"code", "message", "retryable", "details"}
    conflict = client.post("/api/v1/broll/jobs", data={"script": "Hello"}, files={"audio": ("voice.mp3", b"x", "audio/mpeg")})
    assert conflict.status_code == 422
    assert conflict.json()["detail"]["error"]["code"] == "INPUT_CONFLICT"


def test_invalid_ratio_and_oversized_upload_are_rejected(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(main, "DATA_DIR", tmp_path)
    invalid_ratio = client.post("/api/v1/broll/jobs", data={"script": "A valid script.", "aspect_ratio": "4:3"})
    assert invalid_ratio.status_code == 422
    assert invalid_ratio.json()["detail"]["error"]["code"] == "INVALID_ASPECT_RATIO"
    monkeypatch.setattr(main, "MAX_UPLOAD_BYTES", 32)
    oversized = client.post("/api/v1/broll/jobs", data={"aspect_ratio": "16:9"}, files={"audio": ("large.wav", b"x" * 33, "audio/wav")})
    assert oversized.status_code == 422
    assert oversized.json()["detail"]["error"]["code"] == "INVALID_AUDIO_SIZE"


def test_malformed_audio_fails_with_structured_error_and_is_cleaned(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(main, "DATA_DIR", tmp_path)
    response = client.post("/api/v1/broll/jobs", data={"aspect_ratio": "16:9"}, files={"audio": ("broken.wav", b"not a wav file" * 32, "audio/wav")})
    assert response.status_code == 202
    job = wait_for_job(response.json()["job_id"])
    assert job["status"] == "failed"
    assert job["error"]["code"] == "INVALID_AUDIO"
    assert job["error"]["message"]
    assert job["result"] is None
    job_dir = tmp_path / job["job_id"]
    assert list(job_dir.iterdir()) == []


def test_silent_audio_is_rejected_and_cleaned(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(main, "DATA_DIR", tmp_path)
    silent = tmp_path / "silent.wav"
    subprocess.run([main._ffmpeg_path(), "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono:d=1", str(silent)], check=True)
    accepted = client.post("/api/v1/broll/jobs", data={"aspect_ratio": "16:9"}, files={"audio": ("silent.wav", silent.read_bytes(), "audio/wav")})
    job = wait_for_job(accepted.json()["job_id"])
    assert job["status"] == "failed"
    assert job["error"]["code"] == "INVALID_AUDIO"
    assert "silent" in job["error"]["message"].lower()
    assert list((tmp_path / job["job_id"]).iterdir()) == []


def test_audio_longer_than_ten_minutes_is_rejected(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(main, "DATA_DIR", tmp_path)
    long_audio = tmp_path / "long.mp3"
    subprocess.run([main._ffmpeg_path(), "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=601", "-c:a", "libmp3lame", "-b:a", "32k", str(long_audio)], check=True)
    accepted = client.post("/api/v1/broll/jobs", data={"aspect_ratio": "16:9"}, files={"audio": ("long.mp3", long_audio.read_bytes(), "audio/mpeg")})
    job = wait_for_job(accepted.json()["job_id"])
    assert job["status"] == "failed"
    assert job["error"]["code"] == "INVALID_AUDIO"
    assert "10 minutes" in job["error"]["message"]


def test_missing_ffprobe_fails_job_clearly(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(main, "DATA_DIR", tmp_path)
    monkeypatch.setattr(main, "_ffprobe_path", lambda: None)
    response = client.post("/api/v1/broll/jobs", data={"script": "Missing ffprobe test."})
    job = wait_for_job(response.json()["job_id"])
    assert job["status"] == "failed"
    assert "ffprobe is unavailable" in job["error"]["message"]
    assert job["result"] is None


def test_retryable_provider_rate_limit_is_retried_once(tmp_path, monkeypatch) -> None:
    from app.providers import ProviderError

    monkeypatch.setattr(main, "DATA_DIR", tmp_path)

    class RetryOnceProvider:
        name = "retry-once"
        version = "test"
        calls = 0

        def select_visual(self, scene_text, index, job_id):
            self.calls += 1
            if self.calls == 1:
                raise ProviderError("PROVIDER_RATE_LIMITED", "Provider asked for a short retry.", retryable=True)
            return main.MOCK_PROVIDER.select_visual(scene_text, index, job_id)

    provider = RetryOnceProvider()
    monkeypatch.setattr(main, "FOOTAGE_PROVIDER", provider)
    response = client.post("/api/v1/broll/jobs", data={"script": "Retry provider once."})
    job = wait_for_job(response.json()["job_id"])
    assert job["status"] == "succeeded", job["error"]
    assert provider.calls == 2


def test_provider_unavailable_and_permanent_failures_have_correct_retry_policy(tmp_path, monkeypatch) -> None:
    from app.providers import ProviderError

    monkeypatch.setattr(main, "DATA_DIR", tmp_path)

    class AlwaysUnavailable:
        name = "unavailable"
        version = "test"
        calls = 0

        def select_visual(self, scene_text, index, job_id):
            self.calls += 1
            raise ProviderError("PROVIDER_UNAVAILABLE", "Provider is temporarily unavailable.", retryable=True, details={"status": 503})

    unavailable = AlwaysUnavailable()
    monkeypatch.setattr(main, "FOOTAGE_PROVIDER", unavailable)
    response = client.post("/api/v1/broll/jobs", data={"script": "Temporary provider failure."})
    job = wait_for_job(response.json()["job_id"])
    assert job["status"] == "failed"
    assert job["error"]["code"] == "PROVIDER_UNAVAILABLE"
    assert job["error"]["retryable"] is True
    assert job["error"]["details"]["status"] == 503
    assert job["error"]["details"]["affected_scenes"][0]["provider_details"]["status"] == 503
    assert unavailable.calls == main.PROVIDER_MAX_ATTEMPTS

    class PermanentFailure(AlwaysUnavailable):
        name = "permanent-failure"

        def select_visual(self, scene_text, index, job_id):
            self.calls += 1
            raise ProviderError("PROVIDER_REJECTED", "Request cannot be fulfilled.", retryable=False)

    permanent = PermanentFailure()
    monkeypatch.setattr(main, "FOOTAGE_PROVIDER", permanent)
    second = client.post("/api/v1/broll/jobs", data={"script": "Permanent provider failure."})
    failed = wait_for_job(second.json()["job_id"])
    assert failed["status"] == "failed"
    assert failed["error"]["code"] == "PROVIDER_REJECTED"
    assert failed["error"]["retryable"] is False
    assert permanent.calls == 1


def test_provider_timeout_is_bounded_and_retryable(tmp_path, monkeypatch) -> None:
    import time as time_module

    monkeypatch.setattr(main, "DATA_DIR", tmp_path)
    monkeypatch.setattr(main, "PROVIDER_TIMEOUT_SECONDS", 0.05)

    class SlowProvider:
        name = "slow-provider"
        version = "test"
        calls = 0
        first_call_at = None

        def select_visual(self, scene_text, index, job_id):
            self.calls += 1
            if self.first_call_at is None:
                self.first_call_at = time_module.monotonic()
            time_module.sleep(0.2)
            return main.MOCK_PROVIDER.select_visual(scene_text, index, job_id)

    provider = SlowProvider()
    monkeypatch.setattr(main, "FOOTAGE_PROVIDER", provider)
    response = client.post("/api/v1/broll/jobs", data={"script": "Provider timeout test."})
    job = wait_for_job(response.json()["job_id"])
    elapsed = time.monotonic() - provider.first_call_at
    assert job["status"] == "failed"
    assert job["error"]["code"] == "PROVIDER_TIMEOUT"
    assert job["error"]["retryable"] is True
    assert provider.calls == main.PROVIDER_MAX_ATTEMPTS
    assert elapsed < 0.5


def test_visual_alternatives_preserve_a_provider_timeout_error_at_the_boundary(monkeypatch) -> None:
    """The outer watchdog must not mask a provider's own timed-out request."""
    import time as time_module

    monkeypatch.setattr(main, "PROVIDER_TIMEOUT_SECONDS", 0.05)

    class BoundaryTimeoutProvider:
        name = "boundary-timeout-provider"
        version = "test"

        def __init__(self) -> None:
            self.calls = 0

        def visual_options_for_ratio(self, scene_text, index, job_id, generation, ratio, exclude_asset_ids):
            self.calls += 1
            # Finish just after the request timeout, but within the outer
            # watchdog's scheduling grace, as a real HTTP adapter does when it
            # translates a socket timeout into a ProviderError.
            time_module.sleep(0.06)
            raise main.ProviderError(
                "PROVIDER_UNAVAILABLE",
                "Wikimedia Commons could not be reached.",
                retryable=True,
                details={"exception_type": "TimeoutError"},
            )

    provider = BoundaryTimeoutProvider()
    monkeypatch.setattr(main, "FOOTAGE_PROVIDER", provider)
    with pytest.raises(main.ProviderError) as captured:
        main._visual_options("A cyclist rides on a forest trail.", 0, "job-boundary")

    assert captured.value.code == "PROVIDER_UNAVAILABLE"
    assert captured.value.details == {"exception_type": "TimeoutError"}
    assert provider.calls == main.PROVIDER_MAX_ATTEMPTS


def test_provider_empty_and_invalid_download_results_fail_without_artifacts(tmp_path, monkeypatch) -> None:
    from app.providers import SceneVisual

    monkeypatch.setattr(main, "DATA_DIR", tmp_path)

    class EmptyProvider:
        name = "empty"
        version = "test"

        def select_visual(self, scene_text, index, job_id):
            return None

    monkeypatch.setattr(main, "FOOTAGE_PROVIDER", EmptyProvider())
    empty = client.post("/api/v1/broll/jobs", data={"script": "No media result."})
    empty_job = wait_for_job(empty.json()["job_id"])
    assert empty_job["status"] == "failed"
    assert empty_job["error"]["code"] == "PROVIDER_NO_MEDIA"
    assert empty_job["error"]["retryable"] is False

    invalid_asset = tmp_path / "corrupt-clip.mp4"
    invalid_asset.write_bytes(b"not an mp4")

    class CorruptAssetProvider:
        name = "corrupt-asset"
        version = "test"

        def select_visual(self, scene_text, index, job_id):
            return SceneVisual(
                asset_id=f"corrupt-{index}", color="0x112233", asset_path=invalid_asset,
                provider=self.name, source="test-only invalid asset", source_url="https://media.example.invalid/source",
                license="Test-only metadata", attribution="Test-only attribution", ai_generated=False,
                selection_reason="invalid media path test", relevance_score=None, license_verified=True,
            )

    monkeypatch.setattr(main, "FOOTAGE_PROVIDER", CorruptAssetProvider())
    corrupt = client.post("/api/v1/broll/jobs", data={"script": "Corrupt media result."})
    corrupt_job = wait_for_job(corrupt.json()["job_id"])
    assert corrupt_job["status"] == "failed"
    assert corrupt_job["error"]["code"] == "PROVIDER_INVALID_ASSET"
    assert corrupt_job["result"] is None


def test_interrupted_render_is_failed_and_partials_are_removed(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(main, "DATA_DIR", tmp_path)
    original_run = main.subprocess.run

    def interrupt_render(command, **kwargs):
        if "color=c=" in " ".join(command):
            raise subprocess.TimeoutExpired(command, timeout=kwargs.get("timeout", 120))
        return original_run(command, **kwargs)

    monkeypatch.setattr(main.subprocess, "run", interrupt_render)
    response = client.post("/api/v1/broll/jobs", data={"script": "Interrupt the renderer."})
    job = wait_for_job(response.json()["job_id"])
    assert job["status"] == "failed"
    assert "timed out" in job["error"]["message"].lower()
    assert job["result"] is None
    assert list((tmp_path / job["job_id"]).iterdir()) == []


def test_missing_required_artifact_prevents_success_and_cleans_partial_outputs(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(main, "DATA_DIR", tmp_path)
    verify = main._verify_required_artifacts

    def remove_timeline(job_id, job_dir, ratio, expected_duration):
        (job_dir / "timeline.json").unlink(missing_ok=True)
        return verify(job_id, job_dir, ratio, expected_duration)

    monkeypatch.setattr(main, "_verify_required_artifacts", remove_timeline)
    response = client.post("/api/v1/broll/jobs", data={"script": "A required artifact will be absent."})
    job = wait_for_job(response.json()["job_id"])
    assert job["status"] == "failed"
    assert "timeline.json" in job["error"]["message"]
    assert job["result"] is None
    assert list((tmp_path / job["job_id"]).iterdir()) == []


def test_invalid_manifest_prevents_success(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(main, "DATA_DIR", tmp_path)
    verify = main._verify_required_artifacts

    def corrupt_manifest(job_id, job_dir, ratio, expected_duration):
        (job_dir / "manifest.json").write_text("not valid JSON", encoding="utf-8")
        return verify(job_id, job_dir, ratio, expected_duration)

    monkeypatch.setattr(main, "_verify_required_artifacts", corrupt_manifest)
    accepted = client.post("/api/v1/broll/jobs", data={"script": "The manifest is invalid."}).json()
    job = wait_for_job(accepted["job_id"])
    assert job["status"] == "failed"
    assert "metadata artifact" in job["error"]["message"]
    assert job["result"] is None
    assert list((tmp_path / job["job_id"]).iterdir()) == []


def test_simultaneous_jobs_isolate_one_provider_failure_from_success(tmp_path, monkeypatch) -> None:
    from app.providers import ProviderError

    monkeypatch.setattr(main, "DATA_DIR", tmp_path)
    barrier = threading.Barrier(2)

    class MixedProvider:
        name = "mixed-test"
        version = "test"

        def select_visual(self, scene_text, index, job_id):
            barrier.wait(timeout=3)
            if scene_text.startswith("Broken job"):
                raise ProviderError("PROVIDER_REJECTED", "Test job intentionally rejected.", retryable=False)
            return main.MOCK_PROVIDER.select_visual(scene_text, index, job_id)

    monkeypatch.setattr(main, "FOOTAGE_PROVIDER", MixedProvider())

    def submit(script):
        return client.post("/api/v1/broll/jobs", data={"script": script, "aspect_ratio": "1:1"}).json()

    with ThreadPoolExecutor(max_workers=2) as submitters:
        accepted = list(submitters.map(submit, ["Broken job has its own output folder.", "Healthy job keeps its own caption."]))
    broken = wait_for_job(accepted[0]["job_id"])
    healthy = wait_for_job(accepted[1]["job_id"])
    assert broken["status"] == "failed"
    assert broken["error"]["code"] == "PROVIDER_REJECTED"
    assert healthy["status"] == "succeeded", healthy["error"]
    caption = client.get(healthy["result"]["captions"])
    assert caption.status_code == 200
    assert "Healthy job keeps its own caption" in caption.text
    assert "Broken job" not in caption.text
    manifest = client.get(healthy["result"]["manifest"]).json()
    assert manifest["job_id"] == healthy["job_id"]
    assert not list((tmp_path / broken["job_id"]).iterdir())
    assert (tmp_path / healthy["job_id"] / "video_1x1.mp4").is_file()
    assert client.get(f"/api/v1/jobs/{broken['job_id']}/artifacts/manifest.json").status_code == 404


def test_invalid_audio_extension_is_rejected() -> None:
    response = client.post("/api/v1/broll/jobs", data={"script": ""}, files={"audio": ("clip.txt", b"data", "text/plain")})
    assert response.status_code == 422
    assert response.json()["detail"]["error"]["code"] == "INVALID_AUDIO_TYPE"


def test_clear_error_when_ffmpeg_missing(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(main, "DATA_DIR", tmp_path)
    monkeypatch.setattr(main, "_ffmpeg_path", lambda: None)
    response = client.post("/api/v1/broll/jobs", data={"script": "Test rendering failure."})
    job = wait_for_job(response.json()["job_id"])
    assert job["status"] == "failed"
    assert "FFmpeg is unavailable" in job["error"]["message"]
    assert set(job["error"]) == {"code", "message", "retryable", "details"}


def test_idempotency_returns_original_job_and_rejects_changed_request() -> None:
    key = f"test-{uuid.uuid4()}"
    headers = {"Idempotency-Key": key}
    payload = {"script": "Idempotency keeps one job.", "aspect_ratio": "16:9"}
    before = len(main.jobs)
    first = client.post("/api/v1/broll/jobs", data=payload, headers=headers)
    second = client.post("/api/v1/broll/jobs", data=payload, headers=headers)
    assert first.status_code == second.status_code == 202
    assert first.json()["job_id"] == second.json()["job_id"]
    assert wait_for_job(first.json()["job_id"])["status"] in {"succeeded", "failed"}
    assert len(main.jobs) == before + 1
    changed = client.post("/api/v1/broll/jobs", data={**payload, "aspect_ratio": "1:1"}, headers=headers)
    assert changed.status_code == 409
    assert changed.json()["detail"]["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"


def test_readiness_reports_dependencies(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(main, "DATA_DIR", tmp_path / "jobs")
    monkeypatch.setattr(main, "_ffmpeg_path", lambda: "ffmpeg")
    monkeypatch.setattr(main, "_ffprobe_path", lambda: "ffprobe")
    response = client.get("/ready")
    assert response.status_code == 200
    assert response.json()["status"] == "ready"
    monkeypatch.setattr(main, "_ffmpeg_path", lambda: None)
    unavailable = client.get("/ready")
    assert unavailable.status_code == 503
    assert unavailable.json()["detail"]["error"]["code"] == "NOT_READY"


def test_correlation_id_is_returned_and_logged_as_json() -> None:
    output = io.StringIO()
    capture = logging.StreamHandler(output)
    capture.setFormatter(main.JsonFormatter())
    main.logger.addHandler(capture)
    try:
        response = client.get("/health", headers={"X-Correlation-ID": "assessment-run-7"})
    finally:
        main.logger.removeHandler(capture)
    assert response.headers["X-Correlation-ID"] == "assessment-run-7"
    logged = json.loads(output.getvalue().strip().splitlines()[-1])
    assert logged["timestamp"]
    assert logged["level"] == "INFO"
    assert logged["job_id"] == "-"
    assert logged["correlation_id"] == "assessment-run-7"


def test_job_log_has_job_and_correlation_ids_without_script_text(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(main, "DATA_DIR", tmp_path)
    output = io.StringIO()
    capture = logging.StreamHandler(output)
    capture.setFormatter(main.JsonFormatter())
    main.logger.addHandler(capture)
    script = "Private test phrase must not appear in logs."
    correlation_id = "job-log-test-42"
    try:
        accepted = client.post("/api/v1/broll/jobs", data={"script": script}, headers={"X-Correlation-ID": correlation_id}).json()
        job = wait_for_job(accepted["job_id"])
    finally:
        main.logger.removeHandler(capture)
    assert job["status"] == "succeeded"
    records = [json.loads(line) for line in output.getvalue().splitlines()]
    success = next(record for record in records if record["message"] == "job succeeded")
    assert success["job_id"] == accepted["job_id"]
    assert success["correlation_id"] == correlation_id
    assert script not in output.getvalue()


def test_missing_job_and_request_validation_use_standard_error_shape() -> None:
    missing = client.get("/api/v1/jobs/00000000-0000-0000-0000-000000000000")
    assert missing.status_code == 404
    assert set(missing.json()["detail"]["error"]) == {"code", "message", "retryable", "details"}
    invalid = client.post("/api/v1/broll/jobs", json={"script": "not multipart"})
    assert invalid.status_code == 422
    assert invalid.json()["detail"]["error"]["code"] == "INPUT_REQUIRED"


def test_unexpected_errors_are_generic_and_standardized() -> None:
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET", "scheme": "http", "path": "/", "raw_path": b"/", "query_string": b"", "root_path": "", "headers": []}
    response = asyncio.run(main.unexpected_error_handler(main.Request(scope), RuntimeError("sensitive internal text")))
    assert response.status_code == 500
    body = json.loads(response.body)
    assert body["detail"]["error"]["code"] == "INTERNAL_ERROR"
    assert body["detail"]["error"]["retryable"] is True
    assert "sensitive internal text" not in response.body.decode()


def test_validation_handler_does_not_echo_bad_input() -> None:
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST", "scheme": "http", "path": "/api/v1/broll/jobs", "raw_path": b"/api/v1/broll/jobs", "query_string": b"", "root_path": "", "headers": []}
    error = main.RequestValidationError([{"loc": ("body", "script"), "msg": "invalid", "type": "value_error", "input": "private-content"}])
    response = asyncio.run(main.validation_error_handler(main.Request(scope), error))
    payload = json.loads(response.body)
    assert response.status_code == 422
    assert payload["detail"]["error"]["code"] == "VALIDATION_ERROR"
    assert "private-content" not in response.body.decode()


def test_configuration_rejects_invalid_values() -> None:
    from app.config import load_settings

    with pytest.raises(ValueError, match="JOB_WORKERS"):
        load_settings({"JOB_WORKERS": "0"})
    with pytest.raises(ValueError, match="BROLL_PROVIDER"):
        load_settings({"BROLL_PROVIDER": "external"})
    with pytest.raises(ValueError, match="PROVIDER_TIMEOUT_SECONDS"):
        load_settings({"PROVIDER_TIMEOUT_SECONDS": "not-a-number"})
    with pytest.raises(ValueError, match="PROVIDER_TIMEOUT_SECONDS"):
        load_settings({"PROVIDER_TIMEOUT_SECONDS": "121"})


def test_real_provider_configuration_requires_server_credentials() -> None:
    from app.config import load_settings

    incomplete_pexels = load_settings({"BROLL_PROVIDER": "pexels"})
    assert incomplete_pexels.provider == "pexels"
    assert incomplete_pexels.pexels_api_key is None
    incomplete_tts = load_settings({"TTS_PROVIDER": "elevenlabs"})
    assert incomplete_tts.speech_provider == "elevenlabs"
    assert incomplete_tts.elevenlabs_api_key is None
    assert incomplete_tts.elevenlabs_voice_id is None
    with pytest.raises(ValueError, match="ELEVENLABS_MODEL_ID"):
        load_settings({"ELEVENLABS_MODEL_ID": "   "})
    settings = load_settings({"BROLL_PROVIDER": "pexels", "PEXELS_API_KEY": "test-key", "TTS_PROVIDER": "elevenlabs", "ELEVENLABS_API_KEY": "test-key", "ELEVENLABS_VOICE_ID": "test-voice"})
    assert settings.provider == "pexels"
    assert settings.speech_provider == "elevenlabs"
    assert load_settings({"SCENECRAFT_ACCESS_TOKEN": "test-token"}).access_token == "test-token"
    assert load_settings({}).access_token is None
    incomplete_commons = load_settings({"BROLL_PROVIDER": "wikimedia"})
    assert incomplete_commons.provider == "wikimedia"
    assert incomplete_commons.wikimedia_contact is None
    commons = load_settings({"BROLL_PROVIDER": "wikimedia", "WIKIMEDIA_CONTACT": "https://example.org/contact"})
    assert commons.provider == "wikimedia"
    local = load_settings({"BROLL_PROVIDER": "local"})
    assert local.provider == "local"


def test_selected_pexels_without_key_returns_actionable_error_instead_of_mock_media() -> None:
    from app.providers import ProviderError, UnconfiguredFootageProvider

    provider = UnconfiguredFootageProvider(
        "pexels-unconfigured", "videos-api-v1", "PEXELS_API_KEY is missing."
    )
    with pytest.raises(ProviderError) as error:
        provider.visual_options_for_ratio("A cyclist rides a forest trail at sunrise.", 0, "job-1", 0, "16:9", set())
    assert error.value.code == "BROLL_NOT_CONFIGURED"
    assert error.value.retryable is False
    assert "PEXELS_API_KEY" in str(error.value)


def test_project_dotenv_selects_elevenlabs_without_overriding_process_environment(tmp_path, monkeypatch):
    from app import config

    env_file = tmp_path / ".env"
    env_file.write_text(
        "TTS_PROVIDER=elevenlabs\n"
        "ELEVENLABS_API_KEY=test-only-not-a-real-key\n"
        "ELEVENLABS_VOICE_ID=test-only-voice\n"
        "ELEVENLABS_MODEL_ID=eleven_multilingual_v2\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("SCENECRAFT_DISABLE_DOTENV", raising=False)
    monkeypatch.delenv("TTS_PROVIDER", raising=False)
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    monkeypatch.delenv("ELEVENLABS_VOICE_ID", raising=False)
    monkeypatch.delenv("ELEVENLABS_MODEL_ID", raising=False)
    monkeypatch.setattr(config, "PROJECT_ENV_FILE", env_file)

    assert config.load_project_environment()
    settings = config.load_settings()
    provider = main._create_speech_provider(settings)
    assert settings.speech_provider == "elevenlabs"
    assert isinstance(provider, main.ElevenLabsSpeechProvider)
    assert provider._model_id == "eleven_multilingual_v2"
    assert provider._voice_id == "test-only-voice"

    monkeypatch.setenv("TTS_PROVIDER", "mock")
    config.load_project_environment()
    assert config.load_settings().speech_provider == "mock"


def test_elevenlabs_status_does_not_reuse_a_mock_voice_cache(tmp_path, monkeypatch):
    from app.config import load_settings

    monkeypatch.setattr(main, "DATA_DIR", tmp_path)
    job_id = "provider-switch-cache-test"
    (tmp_path / job_id).mkdir()
    scene_texts = ["A saved narration scene."]
    monkeypatch.setattr(main, "settings", load_settings({"TTS_PROVIDER": "mock"}))
    monkeypatch.setattr(main, "SPEECH_PROVIDER", None)
    _, demo_signature = main._voice_cache_paths(job_id, scene_texts)
    job = {
        "voice_input_type": "script",
        "scenes": [{"narration_text": scene_texts[0]}],
        "voice_generation": {"status": "ready", "signature": demo_signature, "provider": "mock_sine_test_tone", "mode": "demo_test_tone"},
    }

    class ElevenLabsFixture:
        name = "elevenlabs"
        version = "fixture"
        _model_id = "eleven_multilingual_v2"
        _voice_id = "fixture-voice"

    monkeypatch.setattr(main, "settings", load_settings({"TTS_PROVIDER": "elevenlabs", "ELEVENLABS_API_KEY": "test-only-key", "ELEVENLABS_VOICE_ID": "fixture-voice"}))
    monkeypatch.setattr(main, "SPEECH_PROVIDER", ElevenLabsFixture())
    _, live_signature = main._voice_cache_paths(job_id, scene_texts)
    assert live_signature != demo_signature
    status = main._voice_status(job_id, job)
    assert status["status"] == "stale"


def test_elevenlabs_provider_failure_never_falls_back_to_demo_tone(tmp_path, monkeypatch):
    from app.config import load_settings
    from app.providers import ProviderError

    monkeypatch.setattr(main, "DATA_DIR", tmp_path)
    monkeypatch.setattr(main, "settings", load_settings({
        "TTS_PROVIDER": "elevenlabs",
        "ELEVENLABS_API_KEY": "test-only-key",
        "ELEVENLABS_VOICE_ID": "fixture-voice",
    }))

    class FailingElevenLabsFixture:
        name = "elevenlabs"
        version = "test"
        _model_id = "eleven_multilingual_v2"
        _voice_id = "fixture-voice"

        def __init__(self):
            self.calls = 0

        def synthesize(self, text, destination):
            self.calls += 1
            raise ProviderError("PROVIDER_AUTH_FAILED", "Mocked authentication rejection.", retryable=False)

    speech = FailingElevenLabsFixture()
    monkeypatch.setattr(main, "SPEECH_PROVIDER", speech)
    response = client.post("/api/v1/broll/jobs", data={"script": "This job must not fall back to a test tone."})
    job = wait_for_job(response.json()["job_id"])
    assert job["status"] == "failed"
    assert job["error"]["code"] == "PROVIDER_AUTH_FAILED"
    assert speech.calls == 1
    assert job["result"] is None


def test_wikimedia_commons_provider_searches_scene_phrase_and_keeps_license_provenance(tmp_path, monkeypatch):
    from app import providers

    requested = {}
    payload = {"query": {"pages": [{"pageid": 123, "title": "File:River kayak.webm", "imageinfo": [{
        "url": "https://upload.wikimedia.org/wikipedia/commons/a/a1/River_kayak.webm",
        "thumburl": "https://upload.wikimedia.org/wikipedia/commons/thumb/a/a1/River_kayak.webm/480px-River_kayak.webm.jpg",
        "size": 4096, "mime": "video/webm", "mediatype": "VIDEO",
        "extmetadata": {"LicenseShortName": {"value": "CC BY-SA 4.0"}, "LicenseUrl": {"value": "https://creativecommons.org/licenses/by-sa/4.0/"}, "Artist": {"value": "<a href='/wiki/User:Creator'>Jane Creator</a>"}},
    }]}]}}

    class FakeResponse(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *args): self.close()
        def geturl(self): return "https://commons.wikimedia.org/w/api.php"

    def fake_urlopen(request, timeout):
        requested["url"] = request.full_url
        requested["ua"] = request.get_header("User-agent")
        requested["timeout"] = timeout
        return FakeResponse(json.dumps(payload).encode())

    monkeypatch.setattr(providers, "urlopen", fake_urlopen)
    adapter = providers.WikimediaCommonsFootageProvider(tmp_path, "contact@example.org", timeout=2)
    options = adapter.visual_options("A kayak travels along a river", 0, "job-1")
    assert len(options) == 1
    assert "filetype%3Avideo" in requested["url"]
    assert "kayak" in requested["url"] and "river" in requested["url"]
    assert "contact@example.org" in requested["ua"]
    assert options[0].source == "River kayak.webm"
    assert options[0].license.startswith("CC BY-SA 4.0")
    assert options[0].attribution == "Jane Creator"
    assert options[0].source_url.startswith("https://commons.wikimedia.org/wiki/File:")
    assert options[0].ai_generated is None
    assert options[0].relevance_score == 1.0


def test_wikimedia_commons_accepts_video_ogv_reported_as_application_ogg(tmp_path, monkeypatch):
    from app import providers

    payload = {"query": {"pages": [{"pageid": 124, "title": "File:Misty lake.ogv", "imageinfo": [{
        "url": "https://upload.wikimedia.org/wikipedia/commons/a/a1/Misty_lake.ogv",
        "size": 4096, "mime": "application/ogg", "mediatype": "VIDEO",
        "extmetadata": {"LicenseShortName": {"value": "CC BY 4.0"}, "LicenseUrl": {"value": "https://creativecommons.org/licenses/by/4.0/"}, "Artist": {"value": "Lake Creator"}},
    }]}]}}

    class FakeResponse(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *args): self.close()
        def geturl(self): return "https://commons.wikimedia.org/w/api.php"

    monkeypatch.setattr(providers, "urlopen", lambda *args, **kwargs: FakeResponse(json.dumps(payload).encode()))
    options = providers.WikimediaCommonsFootageProvider(tmp_path, "contact@example.org").visual_options("Mist drifts over a quiet lake.", 0, "job-1")
    assert len(options) == 1
    assert options[0].media_type == "video"
    assert options[0].download_url.endswith(".ogv")
    assert options[0].attribution == "Lake Creator"


def test_wikimedia_commons_rejects_application_ogg_without_video_mediatype(tmp_path, monkeypatch):
    from app import providers

    payload = {"query": {"pages": [{"pageid": 125, "title": "File:Lake audio.ogv", "imageinfo": [{
        "url": "https://upload.wikimedia.org/wikipedia/commons/a/a1/Lake_audio.ogv",
        "size": 4096, "mime": "application/ogg", "mediatype": "AUDIO",
        "extmetadata": {"LicenseShortName": {"value": "CC BY 4.0"}, "LicenseUrl": {"value": "https://creativecommons.org/licenses/by/4.0/"}, "Artist": {"value": "Audio Creator"}},
    }]}]}}

    class FakeResponse(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *args): self.close()
        def geturl(self): return "https://commons.wikimedia.org/w/api.php"

    monkeypatch.setattr(providers, "urlopen", lambda *args, **kwargs: FakeResponse(json.dumps(payload).encode()))
    provider = providers.WikimediaCommonsFootageProvider(tmp_path, "contact@example.org")
    with pytest.raises(providers.ProviderError, match="no video candidates") as error:
        provider.visual_options("Mist drifts over a quiet lake.", 0, "job-1")
    assert error.value.details["rejected_candidates"]["not_video_or_too_large"] >= 1


def test_scene_search_phrases_add_commons_friendly_outdoor_fallbacks():
    from app.pipeline import scene_search_phrases

    cyclist = scene_search_phrases("A cyclist rides along a forest trail at sunrise.", limit=8)
    lake = scene_search_phrases("Mist drifts across a quiet lake at sunrise.", limit=8)
    assert "cyclist forest trail" in cyclist
    assert "cycling trail" in cyclist
    assert "misty lake" in lake
    assert "lake sunrise" in lake


def test_wikimedia_commons_tries_bounded_alternative_phrases_after_empty_results(monkeypatch):
    from app import providers

    requested = []
    payload = {"query": {"pages": [{"pageid": 555, "title": "File:River kayaking.webm", "imageinfo": [{
        "url": "https://upload.wikimedia.org/wikipedia/commons/a/a1/River_kayaking.webm",
        "size": 4096, "mime": "video/webm", "mediatype": "VIDEO",
        "extmetadata": {"LicenseShortName": {"value": "CC BY 4.0"}, "LicenseUrl": {"value": "https://creativecommons.org/licenses/by/4.0/"}, "Artist": {"value": "Jane Creator"}},
    }]}]}}

    class FakeResponse(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *args): self.close()
        def geturl(self): return "https://commons.wikimedia.org/w/api.php"

    def search(request, timeout):
        requested.append(request.full_url)
        if len(requested) < 2:
            return FakeResponse(b'{"query":{"pages":[]}}')
        return FakeResponse(json.dumps(payload).encode())

    monkeypatch.setattr(providers, "urlopen", search)
    adapter = providers.WikimediaCommonsFootageProvider(Path("unused"), "contact@example.org", min_request_interval=0)
    options = adapter.visual_options("A hiker paddles a kayak beside a long mountain river at sunrise", 0, "job-1")
    assert len(requested) == 4
    assert len(options) == 1
    assert options[0].attribution == "Jane Creator"
    assert options[0].source_url.endswith("File:River_kayaking.webm")
    assert options[0].selection_requires_review is True


def test_wikimedia_commons_serial_alternative_search_uses_its_bounded_operation_budget(monkeypatch, tmp_path):
    """Several legal Commons searches must not be cut off as one HTTP request."""
    from app import providers
    import time as time_module

    payload = {"query": {"pages": [{"pageid": 556, "title": "File:Forest cycling.webm", "imageinfo": [{
        "url": "https://upload.wikimedia.org/wikipedia/commons/a/a1/Forest_cycling.webm",
        "size": 4096, "mime": "video/webm", "mediatype": "VIDEO",
        "extmetadata": {"LicenseShortName": {"value": "CC BY 4.0"}, "LicenseUrl": {"value": "https://creativecommons.org/licenses/by/4.0/"}, "Artist": {"value": "Cycle Creator"}},
    }]}]}}

    class FakeResponse(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *args): self.close()
        def geturl(self): return "https://commons.wikimedia.org/w/api.php"

    calls = 0

    def search(request, timeout):
        nonlocal calls
        calls += 1
        time_module.sleep(0.06)
        return FakeResponse(b'{"query":{"pages":[]}}' if calls == 1 else json.dumps(payload).encode())

    monkeypatch.setattr(providers, "urlopen", search)
    monkeypatch.setattr(main, "PROVIDER_TIMEOUT_SECONDS", 0.05)
    adapter = providers.WikimediaCommonsFootageProvider(tmp_path, "contact@example.org", timeout=0.05, min_request_interval=0, max_retry_after_seconds=0)
    adapter.max_search_phrases = 2
    monkeypatch.setattr(main, "FOOTAGE_PROVIDER", adapter)

    options = main._visual_options("A cyclist rides along a forest trail.", 0, "job-serial-search")

    assert calls == 2
    assert len(options) == 1
    assert adapter.operation_timeout_seconds == 0.2


def test_wikimedia_commons_failed_search_returns_structured_provider_error(monkeypatch):
    from app import providers
    from urllib.error import HTTPError

    def fail(*args, **kwargs):
        raise HTTPError("https://commons.wikimedia.org/w/api.php", 503, "busy", {}, None)

    monkeypatch.setattr(providers, "urlopen", fail)
    provider = providers.WikimediaCommonsFootageProvider(Path("unused"), "contact@example.org")
    with pytest.raises(providers.ProviderError) as error:
        provider.visual_options("River kayak", 0, "job-1")
    assert error.value.code == "PROVIDER_UNAVAILABLE"
    assert error.value.retryable is True
    assert error.value.details["stage"] == "commons_search"
    assert isinstance(error.value.details["elapsed_ms"], int)


def test_wikimedia_commons_429_exposes_retry_after_without_retrying(monkeypatch):
    from app import providers
    from email.message import Message
    from urllib.error import HTTPError

    calls = 0
    headers = Message()
    headers.add_header("Retry-After", "7")

    def rate_limited(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise HTTPError("https://commons.wikimedia.org/w/api.php", 429, "rate limited", headers, None)

    monkeypatch.setattr(providers, "urlopen", rate_limited)
    adapter = providers.WikimediaCommonsFootageProvider(Path("unused"), "contact@example.org")
    with pytest.raises(providers.ProviderError) as error:
        adapter.visual_options("River kayak", 0, "job-1")

    assert error.value.code == "PROVIDER_RATE_LIMITED"
    assert error.value.retryable is True
    assert error.value.details["retry_after"] == "7"
    assert error.value.details["retry_after_seconds"] == 7
    assert calls == 1


@pytest.mark.parametrize("metadata", [
    {"Artist": {"value": "Jane Creator"}},
    {"LicenseShortName": {"value": "CC BY 4.0"}, "LicenseUrl": {"value": "https://creativecommons.org/licenses/by/4.0/"}},
    {"LicenseShortName": {"value": "CC BY 4.0"}, "Artist": {"value": "Jane Creator"}},
])
def test_wikimedia_commons_rejects_missing_or_incomplete_license_and_creator_metadata(tmp_path, monkeypatch, metadata):
    from app import providers

    payload = {"query": {"pages": [{"pageid": 321, "title": "File:River.webm", "imageinfo": [{
        "url": "https://upload.wikimedia.org/wikipedia/commons/a/a1/River.webm",
        "size": 4096, "mime": "video/webm", "mediatype": "VIDEO", "extmetadata": metadata,
    }]}]}}

    class FakeResponse(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *args): self.close()
        def geturl(self): return "https://commons.wikimedia.org/w/api.php"

    monkeypatch.setattr(providers, "urlopen", lambda *args, **kwargs: FakeResponse(json.dumps(payload).encode()))
    adapter = providers.WikimediaCommonsFootageProvider(tmp_path, "contact@example.org")
    with pytest.raises(providers.ProviderError) as error:
        adapter.visual_options("River kayak", 0, "job-1")
    assert error.value.code == "PROVIDER_NO_MEDIA"
    assert error.value.details["rejected_candidates"]


def test_wikimedia_commons_download_failure_is_retryable_with_server_delay(tmp_path, monkeypatch):
    from app import providers
    from email.message import Message
    from urllib.error import HTTPError

    headers = Message()
    headers.add_header("Retry-After", "5")

    def fail_download(request, timeout):
        raise HTTPError(request.full_url, 503, "temporarily unavailable", headers, None)

    monkeypatch.setattr(providers, "urlopen", fail_download)
    adapter = providers.WikimediaCommonsFootageProvider(tmp_path, "contact@example.org", timeout=2)
    visual = providers.SceneVisual(
        asset_id="commons-123", color="black", asset_path=None, provider="wikimedia-commons",
        source="River.webm", source_url="https://commons.wikimedia.org/wiki/File:River.webm",
        license="CC BY 4.0; see https://creativecommons.org/licenses/by/4.0/", attribution="Jane Creator",
        ai_generated=None, selection_reason="mocked test candidate", relevance_score=None,
        download_url="https://upload.wikimedia.org/wikipedia/commons/a/a1/River.webm",
        rights_basis="commons_file_metadata",
    )
    with pytest.raises(providers.ProviderError) as error:
        adapter.materialize(visual, "job-1")

    assert error.value.code == "PROVIDER_DOWNLOAD_FAILED"
    assert error.value.retryable is True
    assert error.value.details["status_code"] == 503
    assert error.value.details["retry_after_seconds"] == 5


def test_local_library_filename_matching_and_user_rights_attestation(tmp_path):
    from app.providers import LocalFootageLibraryProvider

    library = tmp_path / "library"
    library.mkdir()
    clip = library / "ocean_surfing_drone.mp4"
    main._run([main._ffmpeg_path(), "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=blue:s=160x90:r=30", "-t", "0.3", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(clip)])
    LocalFootageLibraryProvider.sidecar_path(clip).write_text(json.dumps({"license": "My own footage", "attribution": "SceneCraft user", "rights_confirmed": True}), encoding="utf-8")
    provider = LocalFootageLibraryProvider(library)
    options = provider.visual_options("Ocean waves surfing", 0, "job-1")
    assert len(options) == 1
    assert options[0].provider == "local-library"
    assert options[0].selection_reason.startswith("Filename matched scene keywords: ocean")
    assert options[0].relevance_score == pytest.approx(2 / 3)
    assert options[0].rights_basis == "user_attestation"
    assert options[0].license_verified is False
    assert options[0].asset_path == clip


def test_imported_local_footage_renders_and_marks_rights_as_user_declared(tmp_path, monkeypatch):
    from app.providers import LocalFootageLibraryProvider

    library = tmp_path / "library"
    monkeypatch.setattr(main, "LOCAL_FOOTAGE_PROVIDER", LocalFootageLibraryProvider(library))
    monkeypatch.setattr(main, "DATA_DIR", tmp_path / "jobs")
    sample = tmp_path / "sunrise_hiker.mp4"
    main._run([main._ffmpeg_path(), "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=orange:s=160x90:r=30", "-t", "0.5", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(sample)])
    with sample.open("rb") as clip:
        imported = client.post("/api/v1/footage/imports", files={"video": (sample.name, clip, "video/mp4")}, data={"license": "My own footage", "attribution": "Creator", "rights_confirmed": "true"})
    assert imported.status_code == 201, imported.text
    assert imported.json()["rights_basis"] == "user_attestation"
    monkeypatch.setattr(main, "FOOTAGE_PROVIDER", main.LOCAL_FOOTAGE_PROVIDER)
    created = client.post("/api/v1/broll/jobs", data={"script": "A hiker enjoys the sunrise."})
    job = wait_for_job(created.json()["job_id"])
    assert job["status"] == "succeeded", job.get("error")
    timeline = json.loads(client.get(job["result"]["timeline"]).content)
    manifest = json.loads(client.get(job["result"]["manifest"]).content)
    assert timeline["scenes"][0]["provider"] == "local-library"
    assert timeline["scenes"][0]["license_verified"] is False
    assert manifest["rights"]["unverified_assets_used"] is True
    assert client.get(job["result"]["video"]).headers["content-type"].startswith("video/mp4")


def test_two_scene_local_fixture_pipeline_smoke_renders_and_preserves_provenance(tmp_path, monkeypatch):
    from fractions import Fraction
    from app.providers import LocalFootageLibraryProvider

    library = tmp_path / "footage-library"
    library.mkdir()
    fixtures = [
        ("forest_fox.mp4", "forest", "brown", "fox explores a forest trail"),
        ("river_kayak.mp4", "river", "blue", "kayak passes along the river"),
    ]
    for filename, _keyword, color, _scene_text in fixtures:
        clip = library / filename
        main._run([
            main._ffmpeg_path(), "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", f"color=c={color}:s=320x180:r=30",
            "-vf", "drawbox=x='mod(t*60,iw-24)':y=60:w=24:h=24:color=white:t=fill",
            "-t", "0.5", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(clip),
        ])
        LocalFootageLibraryProvider.sidecar_path(clip).write_text(json.dumps({
            "license": "Test-only synthetic fixture; not licensed stock footage",
            "attribution": "Generated locally by FFmpeg for this automated test",
            "rights_confirmed": True,
        }), encoding="utf-8")

    provider = LocalFootageLibraryProvider(library)
    monkeypatch.setattr(main, "LOCAL_FOOTAGE_PROVIDER", provider)
    monkeypatch.setattr(main, "FOOTAGE_PROVIDER", provider)
    monkeypatch.setattr(main, "SPEECH_PROVIDER", None)
    monkeypatch.setattr(main, "DATA_DIR", tmp_path / "jobs")

    created = client.post("/api/v1/broll/jobs", data={
        "script": "A fox explores a forest trail. A kayak passes along the river.",
        "aspect_ratio": "16:9",
    })
    assert created.status_code == 202, created.text
    job = wait_for_job(created.json()["job_id"])
    assert job["status"] == "succeeded", job.get("error")
    assert len(job["scenes"]) == 2

    timeline = client.get(job["result"]["timeline"]).json()
    manifest = client.get(job["result"]["manifest"]).json()
    assert len(timeline["scenes"]) == 2
    assert len({scene["clip"] for scene in timeline["scenes"]}) == 2
    assert [scene["provider"] for scene in timeline["scenes"]] == ["local-library", "local-library"]
    assert all(scene["rights_basis"] == "user_attestation" for scene in timeline["scenes"])
    assert all(scene["license_verified"] is False for scene in timeline["scenes"])
    assert all("not licensed stock footage" in scene["license"] for scene in timeline["scenes"])
    assert all("Generated locally by FFmpeg" in scene["attribution"] for scene in timeline["scenes"])
    assert [asset["license"] for asset in manifest["assets"]] == [scene["license"] for scene in timeline["scenes"]]
    assert [asset["attribution"] for asset in manifest["assets"]] == [scene["attribution"] for scene in timeline["scenes"]]
    assert manifest["provider"]["speech"][0:4] == "mock"
    assert any("440 Hz test tone" in warning for warning in manifest["warnings"])

    video_response = client.get(job["result"]["video"])
    assert video_response.status_code == 200
    assert video_response.headers["content-type"].startswith("video/mp4")
    video_path = tmp_path / "two-scene-smoke.mp4"
    video_path.write_bytes(video_response.content)
    assert video_path.stat().st_size > 0
    probe = main._probe_render(video_path)
    assert probe["video"]["codec_name"] == "h264"
    assert probe["audio"]["codec_name"] == "aac"
    assert (probe["video"]["width"], probe["video"]["height"]) == (1920, 1080)
    assert float(Fraction(probe["video"]["avg_frame_rate"])) == 30
    assert abs(probe["duration"] - timeline["duration_seconds"]) <= 1 / 30


def test_long_script_is_segmented_into_multiple_scene_searches():
    script = " ".join(f"Scene number {index} explores a different place and subject." for index in range(1, 9))
    scenes = main._split_scenes(script)
    assert len(scenes) == 8
    assert [scene.split(".")[0] for scene in scenes] == [f"Scene number {index} explores a different place and subject" for index in range(1, 9)]
    assert all(scene.strip() and len(scene.split()) <= 32 for scene in scenes)


def test_long_script_attempts_footage_for_every_scene_and_reports_each_missing_scene(tmp_path, monkeypatch):
    from app.providers import LocalFootageLibraryProvider, ProviderError

    script = " ".join(f"Scene number {index} explores a different place and subject." for index in range(1, 7))
    calls = []

    class NoResultsProvider:
        name = "test-remote"
        version = "test"

        def visual_options(self, scene_text, index, job_id, generation=0):
            return self.visual_options_for_ratio(scene_text, index, job_id, generation, "16:9", set())

        def visual_options_for_ratio(self, scene_text, index, job_id, generation, ratio, exclude_asset_ids):
            calls.append((index, scene_text))
            raise ProviderError("PROVIDER_NO_MEDIA", "No eligible video matched this scene.", retryable=False)

    monkeypatch.setattr(main, "DATA_DIR", tmp_path / "jobs")
    monkeypatch.setattr(main, "LOCAL_FOOTAGE_PROVIDER", LocalFootageLibraryProvider(tmp_path / "empty-library"))
    monkeypatch.setattr(main, "FOOTAGE_PROVIDER", NoResultsProvider())
    monkeypatch.setattr(type(main.MOCK_PROVIDER), "narration_duration", lambda self, _script: 1.5)
    response = client.post("/api/v1/broll/jobs", data={"script": script})
    job = wait_for_job(response.json()["job_id"])
    assert job["status"] == "failed"
    assert job["error"]["code"] == "SCENE_FOOTAGE_UNAVAILABLE"
    assert len(calls) == len(job["scenes"]) == 6
    assert [call[0] for call in calls] == list(range(6))
    assert [scene["scene_id"] for scene in job["error"]["details"]["affected_scenes"]] == [f"scene-{index}" for index in range(1, 7)]
    assert not (tmp_path / "jobs" / job["job_id"] / "video_16x9.mp4").exists()


def test_missing_remote_scene_retains_partial_storyboard_and_never_exports_fake_video(tmp_path, monkeypatch):
    from app.providers import FallbackFootageProvider, LocalFootageLibraryProvider, ProviderError

    library = tmp_path / "footage-library"
    library.mkdir()
    clip = library / "forest_fox.mp4"
    main._run([main._ffmpeg_path(), "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=green:s=160x90:r=30", "-t", "0.4", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(clip)])
    LocalFootageLibraryProvider.sidecar_path(clip).write_text(json.dumps({
        "license": "Test-only synthetic fixture; not licensed stock footage",
        "attribution": "Generated locally for the test",
        "rights_confirmed": True,
    }), encoding="utf-8")
    local = LocalFootageLibraryProvider(library)

    class EmptyRemote:
        name = "test-remote"
        version = "test"

        def visual_options_for_ratio(self, scene_text, index, job_id, generation, ratio, exclude_asset_ids):
            return []

    monkeypatch.setattr(main, "DATA_DIR", tmp_path / "jobs")
    monkeypatch.setattr(main, "LOCAL_FOOTAGE_PROVIDER", local)
    monkeypatch.setattr(main, "FOOTAGE_PROVIDER", FallbackFootageProvider(EmptyRemote(), local))
    created = client.post("/api/v1/broll/jobs", data={"script": "A fox runs through the forest. A submarine explores the deep ocean."})
    job = wait_for_job(created.json()["job_id"])
    assert job["status"] == "failed"
    assert job["result"] is None
    assert job["error"]["code"] == "PROVIDER_NO_MEDIA"
    assert job["error"]["details"]["affected_scenes"][0]["scene_id"] == "scene-2"
    assert len(job["scenes"]) == 2
    assert job["scenes"][0]["narration_text"] == "A fox runs through the forest."
    assert job["scenes"][0]["selected_visual"]["provider"] == "local-library"
    assert job["scenes"][1]["narration_text"] == "A submarine explores the deep ocean."
    assert job["scenes"][1]["caption_text"] == "A submarine explores the deep ocean."
    assert job["scenes"][1]["visual_options"] == []
    assert "Import a rights-cleared local clip" in job["scenes"][1]["error"]["recovery"]
    assert not (tmp_path / "jobs" / job["job_id"] / "video_16x9.mp4").exists()


def test_provider_information_is_honest_and_never_returns_credentials() -> None:
    response = client.get("/api/v1/providers")
    assert response.status_code == 200
    payload = response.json()
    assert payload["mode"] == "demo"
    assert payload["footage_provider"] == "mock-local"
    assert payload["speech_provider"] == "mock-sine-test-tone"
    assert "not stock footage" in payload["disclosure"]
    assert "private-test-key" not in json.dumps(payload)


def test_provider_failure_is_returned_in_job_status(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(main, "DATA_DIR", tmp_path)

    def fail_scene(self, index):
        raise RuntimeError("mock provider failed")

    monkeypatch.setattr(type(main.MOCK_PROVIDER), "scene_color", fail_scene)
    response = client.post("/api/v1/broll/jobs", data={"script": "Provider failure test."})
    job = wait_for_job(response.json()["job_id"])
    assert job["status"] == "failed"
    assert job["error"]["code"] == "PROCESSING_FAILED"
    assert job["error"]["message"] == "mock provider failed"
    assert set(job["error"]) == {"code", "message", "retryable", "details"}


def test_pexels_provider_search_downloads_only_documented_host_and_keeps_provenance(tmp_path, monkeypatch) -> None:
    from app import providers

    sample_video = tmp_path / "sample.mp4"
    main._run([main._ffmpeg_path(), "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=blue:s=640x360:r=30", "-t", "1", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(sample_video)])
    video_bytes = sample_video.read_bytes()
    requested = []

    class FakeResponse(io.BytesIO):
        def __init__(self, payload, url, headers=None):
            super().__init__(payload)
            self._url = url
            self.headers = headers or {}
        def __enter__(self): return self
        def __exit__(self, *args): self.close()
        def geturl(self): return self._url

    videos = [{"id": 101 + index, "url": f"https://www.pexels.com/video/test-{index}/", "user": {"name": f"Contributor {index}"}, "video_files": [{"file_type": "video/mp4", "width": 640, "height": 360, "link": f"https://player.vimeo.com/external/269971860.sd.mp4?profile_id=164" if index == 0 else f"https://videos.pexels.com/video-files/{101 + index}/clip.mp4"}], "video_pictures": [{"picture": "https://images.pexels.com/videos/101/pictures/preview-0.jpg"}]} for index in range(4)]

    def fake_urlopen(request, timeout):
        url = request.full_url if hasattr(request, "full_url") else request
        requested.append((url, timeout))
        if "api.pexels.com" in url:
            assert request.get_header("Authorization") == "test-api-key"
            return FakeResponse(json.dumps({"videos": videos}).encode(), url)
        return FakeResponse(video_bytes, url)

    monkeypatch.setattr(providers, "urlopen", fake_urlopen)
    adapter = providers.PexelsFootageProvider("test-api-key", tmp_path / "jobs", timeout=1, max_download_bytes=1_000_000)
    options = adapter.visual_options_for_ratio("The scientist studies ocean water", 0, "job-1", 0, "9:16", set())
    assert len(options) == 3
    assert "query=scientist+studies+ocean+water" in requested[0][0]
    assert "orientation=portrait" in requested[0][0]
    assert all(option.asset_path is None and option.download_url for option in options)
    assert all(option.source_url.startswith("https://www.pexels.com/video/") for option in options)
    assert all(option.license_verified and option.relevance_score is None for option in options)
    assert all(option.thumbnail_url.startswith("https://images.pexels.com/") for option in options)
    materialized = adapter.materialize(options[0], "job-1")
    assert materialized.asset_path and materialized.asset_path.is_file()
    main._probe_footage(materialized.asset_path, materialized.asset_id)
    assert len([url for url, _ in requested if "player.vimeo.com" in url]) == 1


@pytest.mark.parametrize(("status", "expected_code", "retryable"), [(401, "PROVIDER_AUTH_FAILED", False), (429, "PROVIDER_RATE_LIMITED", True)])
def test_pexels_provider_reports_auth_and_rate_limit_errors_without_exposing_key(monkeypatch, status, expected_code, retryable):
    from app import providers
    from urllib.error import HTTPError

    def unauthorized(*args, **kwargs):
        raise HTTPError("https://api.pexels.com", status, "provider response", {}, None)

    monkeypatch.setattr(providers, "urlopen", unauthorized)
    adapter = providers.PexelsFootageProvider("private-test-key", Path("unused"))
    with pytest.raises(providers.ProviderError) as error:
        adapter.visual_options("Ocean waves", 0, "job-1")
    assert error.value.code == expected_code
    assert error.value.retryable is retryable
    assert "private-test-key" not in str(error.value)


def test_pexels_rate_limit_preserves_retry_after_without_retrying(monkeypatch):
    from app import providers
    from email.message import Message
    from urllib.error import HTTPError

    headers = Message()
    headers.add_header("Retry-After", "9")
    calls = 0

    def rate_limited(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise HTTPError("https://api.pexels.com/v1/videos/search", 429, "rate limited", headers, None)

    monkeypatch.setattr(providers, "urlopen", rate_limited)
    adapter = providers.PexelsFootageProvider("private-test-key", Path("unused"))
    with pytest.raises(providers.ProviderError) as error:
        adapter.visual_options("Ocean waves", 0, "job-1")

    assert error.value.code == "PROVIDER_RATE_LIMITED"
    assert error.value.details["retry_after_seconds"] == 9
    assert calls == 1


def test_pexels_download_rejects_non_video_content_type(tmp_path, monkeypatch):
    from app import providers

    class FakeResponse(io.BytesIO):
        headers = {"Content-Type": "text/html"}
        def __enter__(self): return self
        def __exit__(self, *args): self.close()
        def geturl(self): return "https://videos.pexels.com/video-files/123/clip.mp4"

    monkeypatch.setattr(providers, "urlopen", lambda *args, **kwargs: FakeResponse(b"<html>not a video</html>"))
    adapter = providers.PexelsFootageProvider("test-key", tmp_path)
    visual = providers.SceneVisual(
        asset_id="pexels-video-123", color="black", asset_path=None, provider="pexels",
        source="Test clip", source_url="https://www.pexels.com/video/test-123/",
        license="Pexels License", attribution="Test contributor", ai_generated=False,
        selection_reason="test", relevance_score=None,
        download_url="https://videos.pexels.com/video-files/123/clip.mp4",
    )
    with pytest.raises(providers.ProviderError, match="unexpected media type"):
        adapter.materialize(visual, "job-1")
    assert not list((tmp_path / "job-1" / ".provider-assets").glob("*.part"))


@pytest.mark.parametrize(("url", "allowed"), [
    ("https://videos.pexels.com/video-files/123/clip.mp4", True),
    ("https://player.vimeo.com/external/123.mp4?profile_id=164", True),
    ("https://player.vimeo.com/video/123", False),
    ("https://example.com/video-files/123/clip.mp4", False),
    ("http://videos.pexels.com/video-files/123/clip.mp4", False),
])
def test_pexels_video_url_allowlist_matches_documented_hosts_only(url, allowed):
    from app.providers import PexelsFootageProvider

    assert PexelsFootageProvider._is_allowed_video_url(url) is allowed


def test_elevenlabs_adapter_posts_text_server_side_and_validates_audio(tmp_path, monkeypatch):
    from app import providers

    sample_mp3 = tmp_path / "speech.mp3"
    main._run([main._ffmpeg_path(), "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-c:a", "libmp3lame", str(sample_mp3)])
    audio_bytes = sample_mp3.read_bytes()
    recorded = {}

    class FakeResponse(io.BytesIO):
        headers = {"character-cost": "15"}
        def __enter__(self): return self
        def __exit__(self, *args): self.close()

    def fake_urlopen(request, timeout):
        recorded["url"] = request.full_url
        recorded["api_key"] = request.get_header("Xi-api-key")
        recorded["body"] = json.loads(request.data)
        recorded["timeout"] = timeout
        return FakeResponse(audio_bytes)

    monkeypatch.setattr(providers, "urlopen", fake_urlopen)
    adapter = providers.ElevenLabsSpeechProvider("private-test-key", "voice-test", timeout=2)
    destination = tmp_path / "out.mp3"
    metadata = adapter.synthesize("Hello from a mocked provider response.", destination)
    assert destination.read_bytes() == audio_bytes
    assert recorded["api_key"] == "private-test-key"
    assert recorded["body"]["text"] == "Hello from a mocked provider response."
    assert "voice-test" in recorded["url"]
    assert metadata["provider"] == "elevenlabs"
    assert metadata["provider_character_cost"] == "15"


def test_elevenlabs_409_exposes_only_sanitized_provider_diagnostics(monkeypatch, tmp_path):
    from urllib.error import HTTPError
    from app import providers

    private_key = "private-test-key"
    response_body = json.dumps({"detail": {
        "type": "conflict",
        "code": "concurrent_modification",
        "message": f"Request conflict; xi-api-key={private_key}",
        "request_id": "safe-request-id-123",
    }}).encode()

    def fake_urlopen(request, timeout):
        raise HTTPError(request.full_url, 409, "Conflict", {}, io.BytesIO(response_body))

    monkeypatch.setattr(providers, "urlopen", fake_urlopen)
    adapter = providers.ElevenLabsSpeechProvider(private_key, "voice-test")
    with pytest.raises(providers.ProviderError) as captured:
        adapter.synthesize("Test request.", tmp_path / "voice.mp3")

    error = captured.value
    assert error.code == "PROVIDER_CONFLICT"
    assert error.retryable is False
    assert error.details == {
        "status_code": 409,
        "provider_error_code": "concurrent_modification",
        "provider_error_type": "conflict",
        "provider_request_id": "safe-request-id-123",
    }
    assert "Request conflict" in str(error)
    assert "[redacted]" in str(error)
    assert private_key not in str(error)
    assert private_key not in repr(error.details)


def test_elevenlabs_free_tier_library_voice_402_has_actionable_error(monkeypatch, tmp_path):
    from urllib.error import HTTPError
    from app import providers

    response_body = json.dumps({"detail": {
        "type": "payment_required",
        "code": "voice_not_available_for_plan",
        "message": "Free users cannot use library voices via the API.",
    }}).encode()

    def fake_urlopen(request, timeout):
        raise HTTPError(request.full_url, 402, "Payment Required", {}, io.BytesIO(response_body))

    monkeypatch.setattr(providers, "urlopen", fake_urlopen)
    adapter = providers.ElevenLabsSpeechProvider("private-test-key", "configured-voice")
    with pytest.raises(providers.ProviderError) as captured:
        adapter.synthesize("Test narration.", tmp_path / "voice.mp3")

    error = captured.value
    assert error.code == "PROVIDER_VOICE_PLAN_RESTRICTED"
    assert error.retryable is False
    assert error.details["status_code"] == 402
    assert error.details["provider_error_code"] == "voice_not_available_for_plan"
    assert "Free users cannot use library voices via the API" in str(error)
    assert "ELEVENLABS_VOICE_ID" in str(error)
    assert "restart SceneCraft Studio" in str(error)
    assert "private-test-key" not in str(error)
    assert "configured-voice" not in str(error)


def test_elevenlabs_409_failure_reaches_voice_status_and_safe_log(tmp_path, monkeypatch, caplog):
    from app import providers

    job = _new_draft(tmp_path, monkeypatch, script="A short narration for conflict reporting.")
    class Provider:
        name = "elevenlabs"

    monkeypatch.setattr(main.logger, "propagate", True)
    monkeypatch.setattr(main, "SPEECH_PROVIDER", Provider())
    # Match the production flow: the request signature is calculated from the
    # provider that will perform the work. Changing providers after capturing
    # it is correctly treated as a stale request, not a provider failure.
    texts = [scene["narration_text"] for scene in main.jobs[job["job_id"]]["scenes"]]
    signature = main._voice_cache_paths(job["job_id"], texts)[1]
    provider_error = providers.ProviderError(
        "PROVIDER_CONFLICT",
        "ElevenLabs speech request failed with HTTP 409: Voice is unavailable for this request.",
        retryable=False,
        details={"status_code": 409, "provider_error_code": "voice_access_denied"},
    )

    monkeypatch.setattr(main, "_prepare_speech_audio", lambda *args, **kwargs: (_ for _ in ()).throw(provider_error))
    main._generate_voice_task(job["job_id"], signature)

    response = client.get(f"/api/v1/jobs/{job['job_id']}/voice")
    assert response.status_code == 200
    status = response.json()
    assert status["status"] == "failed"
    assert status["error"]["code"] == "PROVIDER_CONFLICT"
    assert "HTTP 409" in status["error"]["message"]
    assert status["error"]["details"]["provider_error_code"] == "voice_access_denied"
    assert "speech provider request failed" in caplog.text
    assert "voice_access_denied" in caplog.text
    assert "private-test-key" not in caplog.text


@pytest.mark.parametrize("failure", ["timeout", "rate_limit", "invalid_audio"])
def test_elevenlabs_adapter_reports_bounded_provider_failures(failure, monkeypatch, tmp_path):
    from urllib.error import HTTPError
    from app import providers

    def fake_urlopen(request, timeout):
        if failure == "timeout":
            raise TimeoutError("provider timeout")
        if failure == "rate_limit":
            raise HTTPError(request.full_url, 429, "slow down", {}, None)
        return io.BytesIO(b"not audio")

    monkeypatch.setattr(providers, "urlopen", fake_urlopen)
    adapter = providers.ElevenLabsSpeechProvider("private-test-key", "voice-test", timeout=0.1)
    with pytest.raises(providers.ProviderError) as error:
        adapter.synthesize("Test narration.", tmp_path / "invalid.mp3")
    if failure == "timeout":
        assert error.value.code == "PROVIDER_UNAVAILABLE"
        assert error.value.retryable is True
    elif failure == "rate_limit":
        assert error.value.code == "PROVIDER_RATE_LIMITED"
        assert error.value.retryable is True
        assert error.value.details["status_code"] == 429
    else:
        assert error.value.code == "PROVIDER_INVALID_RESULT"
        assert error.value.retryable is False


def test_voice_generation_requires_credentials_and_returns_actionable_error(tmp_path, monkeypatch):
    from dataclasses import replace
    from app.config import load_settings

    job = _new_draft(tmp_path, monkeypatch, script="A script job needs voice configuration.")
    monkeypatch.setattr(main, "SPEECH_PROVIDER", None)
    monkeypatch.setattr(main, "settings", load_settings({"TTS_PROVIDER": "elevenlabs"}))
    response = client.post(f"/api/v1/jobs/{job['job_id']}/voice")
    assert response.status_code == 503
    error = response.json()["detail"]["error"]
    assert error["code"] == "TTS_NOT_CONFIGURED"
    assert error["retryable"] is False
    assert error["details"]["required_environment"] == ["ELEVENLABS_API_KEY", "ELEVENLABS_VOICE_ID"]
    assert "ELEVENLABS_API_KEY" in error["message"] and "ELEVENLABS_VOICE_ID" in error["message"]
    assert "private-test-key" not in response.text
    provider_info = client.get("/api/v1/providers")
    assert provider_info.status_code == 200
    assert provider_info.json()["speech_configured"] is False
    assert provider_info.json()["mode"] == "unconfigured"
    assert "private-test-key" not in provider_info.text


def test_speech_segment_cache_is_scoped_to_provider_voice_and_model(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DATA_DIR", tmp_path)
    job_id = "voice-cache-provider-scope"
    (tmp_path / job_id).mkdir()
    scratch = tmp_path / job_id / ".voice-rendering"
    scratch.mkdir()

    class FixtureSpeechProvider:
        name = "fixture-speech"
        version = "1"

        def __init__(self, voice_id, model_id):
            self._voice_id = voice_id
            self._model_id = model_id
            self.calls = 0

        def synthesize(self, text, destination):
            self.calls += 1
            main._run([main._ffmpeg_path(), "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=760:duration=0.4", "-c:a", "libmp3lame", str(destination)])
            return {"provider": self.name, "model": self._model_id, "voice_id": self._voice_id}

    first_voice = FixtureSpeechProvider("voice-a", "model-a")
    monkeypatch.setattr(main, "SPEECH_PROVIDER", first_voice)
    main._prepare_speech_audio(job_id, ["Same unchanged scene text."], scratch)
    main._prepare_speech_audio(job_id, ["Same unchanged scene text."], scratch)
    assert first_voice.calls == 1

    second_voice = FixtureSpeechProvider("voice-b", "model-a")
    monkeypatch.setattr(main, "SPEECH_PROVIDER", second_voice)
    main._prepare_speech_audio(job_id, ["Same unchanged scene text."], scratch)
    assert second_voice.calls == 1

    third_model = FixtureSpeechProvider("voice-b", "model-b")
    monkeypatch.setattr(main, "SPEECH_PROVIDER", third_model)
    main._prepare_speech_audio(job_id, ["Same unchanged scene text."], scratch)
    assert third_model.calls == 1


def test_voice_generation_does_not_apply_old_timing_after_narration_edit(tmp_path, monkeypatch):
    import threading

    monkeypatch.setattr(main, "DATA_DIR", tmp_path)
    job = _new_draft(tmp_path, monkeypatch, script="Keep this timing until fresh narration is ready.")
    scene = job["scenes"][0]
    initial_timing = (scene["start"], scene["end"])
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    class SlowSpeechProvider:
        name = "slow-fixture-speech"
        version = "test"
        _model_id = "fixture-model"
        _voice_id = "fixture-voice"

        def synthesize(self, text, destination):
            started.set()
            assert release.wait(3)
            main._run([main._ffmpeg_path(), "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=760:duration=1.4", "-c:a", "libmp3lame", str(destination)])
            finished.set()
            return {"provider": self.name, "model": self._model_id, "voice_id": self._voice_id}

    monkeypatch.setattr(main, "SPEECH_PROVIDER", SlowSpeechProvider())
    queued = client.post(f"/api/v1/jobs/{job['job_id']}/voice")
    assert queued.status_code == 202
    assert started.wait(3)
    changed = client.patch(f"/api/v1/jobs/{job['job_id']}/scenes/{scene['scene_id']}", json={"narration_text": "New text with its own voice timing."})
    assert changed.status_code == 200
    release.set()
    assert finished.wait(5)
    deadline = time.monotonic() + 5
    task_finished = False
    while time.monotonic() < deadline:
        voice_state = main.jobs[job["job_id"]].get("voice_generation", {})
        if voice_state.get("status") == "ready" or "while voice generation was running" in voice_state.get("message", ""):
            task_finished = True
            break
        time.sleep(0.05)
    assert task_finished, "voice task did not finish after the provider response"
    status = client.get(f"/api/v1/jobs/{job['job_id']}/voice").json()
    latest_scene = client.get(f"/api/v1/jobs/{job['job_id']}/scenes/{scene['scene_id']}").json()
    assert (latest_scene["start"], latest_scene["end"]) == initial_timing
    assert status["status"] == "stale"


def test_voice_audio_is_cached_across_draft_and_final_renders(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DATA_DIR", tmp_path)
    monkeypatch.setattr(main, "FOOTAGE_PROVIDER", main.MOCK_PROVIDER)

    class FixtureSpeechProvider:
        name = "fixture-speech-provider"
        version = "test-only"
        _model_id = "fixture-model"
        _voice_id = "fixture-voice"

        def __init__(self):
            self.calls = []

        def synthesize(self, text, destination):
            self.calls.append(text)
            main._run([main._ffmpeg_path(), "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", f"sine=frequency=760:duration={0.55 if text.startswith('First') else 0.85}", "-c:a", "libmp3lame", str(destination)])
            return {"provider": self.name, "model": self._model_id, "voice_id": self._voice_id, "provider_character_cost": None}

    speech = FixtureSpeechProvider()
    monkeypatch.setattr(main, "SPEECH_PROVIDER", speech)
    created = client.post("/api/v1/broll/jobs", data={"script": "First scene. Second scene follows.", "aspect_ratio": "16:9"})
    assert created.status_code == 202
    job = wait_for_job(created.json()["job_id"])
    assert job["status"] == "succeeded", job["error"]
    assert speech.calls == ["First scene.", "Second scene follows."]

    voice = client.get(f"/api/v1/jobs/{job['job_id']}/voice").json()
    assert voice["status"] == "ready"
    assert voice["provider"] == speech.name
    assert voice["mode"] == "generated_speech"
    assert voice["voice_id"] == "fixture-voice"
    audio_response = client.get(voice["audio_url"])
    assert audio_response.status_code == 200
    assert audio_response.headers["content-type"].startswith("audio/wav")
    assert audio_response.content[:4] == b"RIFF"
    assert client.get(voice["audio_url"].split("signature=")[0] + "signature=" + "0" * 64).status_code == 404

    reused = client.post(f"/api/v1/jobs/{job['job_id']}/voice")
    assert reused.status_code == 200
    assert reused.json()["audio_url"] == voice["audio_url"]
    assert len(speech.calls) == 2

    preview = client.post(f"/api/v1/jobs/{job['job_id']}/draft-preview", json={"aspect_ratio": "16:9"})
    assert preview.status_code == 202
    for _ in range(100):
        preview_status = client.get(f"/api/v1/jobs/{job['job_id']}/draft-preview").json()
        if preview_status["status"] in {"succeeded", "failed"}:
            break
        time.sleep(0.1)
    assert preview_status["status"] == "succeeded", preview_status.get("error")
    assert len(speech.calls) == 2

    for scene in job["scenes"]:
        accepted = client.patch(f"/api/v1/jobs/{job['job_id']}/scenes/{scene['scene_id']}", json={"visual_id": scene["visual_options"][0]["id"]})
        assert accepted.status_code == 200
    queued = client.post(f"/api/v1/jobs/{job['job_id']}/render", json={"aspect_ratio": "16:9"})
    assert queued.status_code == 202
    final_job = wait_for_job(job["job_id"])
    assert final_job["status"] == "succeeded", final_job["error"]
    assert len(speech.calls) == 2
    manifest = client.get(final_job["result"]["manifest"]).json()
    assert manifest["provider"]["speech"] == speech.name
    assert not any("440 Hz test tone" in warning for warning in manifest["warnings"])
    assert main._probe_render(tmp_path / job["job_id"] / "video_16x9.mp4")["audio"]["codec_name"] == "aac"


def test_uploaded_voiceover_status_does_not_claim_transcription(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DATA_DIR", tmp_path)
    audio_path = tmp_path / "upload.wav"
    main._run([main._ffmpeg_path(), "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=520:duration=0.8", str(audio_path)])
    response = client.post("/api/v1/broll/jobs", files={"audio": ("upload.wav", audio_path.read_bytes(), "audio/wav")}, data={"aspect_ratio": "16:9"})
    assert response.status_code == 202
    job = wait_for_job(response.json()["job_id"])
    assert job["status"] == "succeeded", job["error"]
    voice = client.get(f"/api/v1/jobs/{job['job_id']}/voice").json()
    assert voice["status"] == "uploaded"
    assert "not transcribed" in voice["label"]


def test_scene_speech_adapter_full_render_keeps_measured_audio_and_caption_timing(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DATA_DIR", tmp_path)

    class FixtureToneSpeech:
        name = "mock-fixture-tone"
        version = "test-only"

        def synthesize(self, text, destination):
            duration = 0.55 if text.startswith("First") else 0.9
            main._run([main._ffmpeg_path(), "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}", "-c:a", "libmp3lame", str(destination)])
            return {"provider": self.name, "model": self.version, "voice_id": "fixture-only"}

    monkeypatch.setattr(main, "SPEECH_PROVIDER", FixtureToneSpeech())
    accepted = client.post("/api/v1/broll/jobs", data={"script": "First scene is short. Second scene is longer."})
    assert accepted.status_code == 202
    job = wait_for_job(accepted.json()["job_id"])
    assert job["status"] == "succeeded", job["error"]
    video = client.get(job["result"]["video"])
    assert video.status_code == 200
    rendered = tmp_path / "fixture-render.mp4"
    rendered.write_bytes(video.content)
    duration, mean_db = main._probe_audio(rendered)
    timeline = client.get(job["result"]["timeline"]).json()
    captions = client.get(job["result"]["captions"]).text
    manifest = client.get(job["result"]["manifest"]).json()
    assert mean_db > -90
    assert manifest["provider"]["speech"] == "mock-fixture-tone"
    assert "Mock narration is a 440 Hz test tone" in " ".join(manifest["warnings"])
    assert [scene["narration_text"] for scene in timeline["scenes"]] == ["First scene is short.", "Second scene is longer."]
    assert timeline["scenes"][0]["end"] == timeline["scenes"][1]["start"]
    assert timeline["scenes"][-1]["end"] == pytest.approx(timeline["duration_seconds"])
    assert "00:00:00,000 -->" in captions
    assert duration == pytest.approx(timeline["duration_seconds"], abs=1 / main.FRAME_RATE)


def test_openapi_file_matches_registered_api_routes() -> None:
    specification = (main.ROOT / "openapi.yaml").read_text(encoding="utf-8")
    contract = yaml.safe_load(specification)
    generated_paths = main.app.openapi()["paths"]
    assert contract["openapi"] == "3.1.0"
    for path in contract["paths"]:
        assert path in generated_paths
        for method in contract["paths"][path]:
            assert method in generated_paths[path]
    assert contract["paths"]["/api/v1/broll/jobs"]["post"]["responses"]["202"]
    assert contract["paths"]["/api/v1/broll/jobs"]["post"]["responses"]["409"]
    assert contract["paths"]["/api/v1/jobs/{job_id}"]["get"]["responses"]["404"]
    assert contract["paths"]["/api/v1/jobs/{job_id}/draft-preview"]["post"]["responses"]["202"]
    assert contract["paths"]["/api/v1/jobs/{job_id}/draft-preview"]["get"]["responses"]["200"]
    assert contract["paths"]["/api/v1/jobs/{job_id}/scenes/{scene_id}/visuals/{visual_id}/preview"]["get"]["responses"]["200"]


def test_compose_configuration_has_only_the_local_app_service() -> None:
    compose = yaml.safe_load((main.ROOT / "compose.yaml").read_text(encoding="utf-8"))
    assert list(compose["services"]) == ["app"]
    assert compose["services"]["app"]["volumes"] == ["./data:/app/data"]
    assert compose["services"]["app"]["environment"]["PROVIDER_TIMEOUT_SECONDS"] == "${PROVIDER_TIMEOUT_SECONDS:-10}"
    environment = compose["services"]["app"]["environment"]
    assert environment["BROLL_PROVIDER"] == "${BROLL_PROVIDER:-mock}"
    assert environment["PEXELS_API_KEY"] == "${PEXELS_API_KEY:-}"
    assert environment["TTS_PROVIDER"] == "${TTS_PROVIDER:-mock}"
    assert environment["ELEVENLABS_API_KEY"] == "${ELEVENLABS_API_KEY:-}"
    assert environment["ELEVENLABS_VOICE_ID"] == "${ELEVENLABS_VOICE_ID:-}"
    assert environment["SCENECRAFT_ACCESS_TOKEN"] == "${SCENECRAFT_ACCESS_TOKEN:-}"
    dockerfile = (main.ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "apt-get install -y --no-install-recommends ffmpeg" in dockerfile
    assert "ffmpeg -version" in dockerfile and "ffprobe -version" in dockerfile
    assert 'CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]' in dockerfile
    assert "USER scenecraft" in dockerfile
    dockerignore = (main.ROOT / ".dockerignore").read_text(encoding="utf-8")
    assert ".env" in dockerignore
    assert "data" in dockerignore


def test_batch_runner_records_success_and_failure(tmp_path) -> None:
    from app.batch_runner import run_batch

    class FakeClient:
        def create_job(self, case):
            return {"job_id": case["name"], "status_url": case["name"]}

        def get_job(self, status_url):
            if status_url == "broken":
                return {"status": "failed", "error": {"code": "PROCESSING_FAILED"}, "result": None}
            return {"status": "succeeded", "error": None, "result": {"video": "/artifact.mp4"}}

    summary = run_batch(FakeClient(), tmp_path / "batch.json", [{"name": "good", "script": "Ok", "aspect_ratio": "16:9"}, {"name": "broken", "script": "Fail", "aspect_ratio": "1:1"}], poll_interval=0)
    saved = json.loads((tmp_path / "batch.json").read_text(encoding="utf-8"))
    assert (summary["attempted"], summary["succeeded"], summary["failed"]) == (2, 1, 1)
    assert summary["job_succeeded"] == 1
    assert summary["validation_failed"] == 0
    assert saved["jobs"][1]["error"]["code"] == "PROCESSING_FAILED"


def test_evaluation_case_pack_loads_ten_varied_scripts() -> None:
    from app.batch_runner import load_cases

    cases = load_cases(main.ROOT / "test-pack" / "evaluation-inputs.json")
    assert len(cases) == 10
    assert {case["aspect_ratio"] for case in cases} == {"16:9", "9:16", "1:1"}
    assert len({case["name"] for case in cases}) == 10
    assert all(case["script"].strip() for case in cases)


def test_batch_runner_downloads_and_validates_artifacts(tmp_path) -> None:
    from app.batch_runner import run_batch

    job_id = "batch-job"
    result = {
        "video": f"/api/v1/jobs/{job_id}/artifacts/video_16x9.mp4",
        "captions": f"/api/v1/jobs/{job_id}/artifacts/captions.srt",
        "timeline": f"/api/v1/jobs/{job_id}/artifacts/timeline.json",
        "manifest": f"/api/v1/jobs/{job_id}/artifacts/manifest.json",
    }

    class ArtifactClient:
        def create_job(self, case):
            return {"job_id": job_id, "status_url": "/status"}

        def get_job(self, status_url):
            return {"status": "succeeded", "error": None, "result": result}

        def download(self, url, target):
            if url.endswith("video_16x9.mp4"):
                contents = b"\x00\x00\x00\x18ftypisom"
            elif url.endswith("captions.srt"):
                contents = b"1\n00:00:00,000 --> 00:00:01,000\nHello\n"
            elif url.endswith("timeline.json"):
                contents = json.dumps({"duration_seconds": 1, "scenes": [{"start": 0, "end": 1}]}).encode()
            else:
                contents = json.dumps({"job_id": job_id}).encode()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(contents)

    summary = run_batch(
        ArtifactClient(),
        tmp_path / "results.json",
        [{"name": "one", "script": "Hello", "aspect_ratio": "16:9"}],
        poll_interval=0,
        download_dir=tmp_path / "downloads",
    )
    record = summary["jobs"][0]
    assert summary["failed"] == 0
    assert summary["job_succeeded"] == 1
    assert summary["validation_failed"] == 0
    assert record["artifact_validation"]["passed"] is True
    assert all(Path(path).is_file() for path in record["artifact_validation"]["downloaded_files"].values())


def _new_draft(tmp_path, monkeypatch, script="A first editable scene. A second scene follows."):
    monkeypatch.setattr(main, "DATA_DIR", tmp_path)
    accepted = client.post("/api/v1/broll/jobs", data={"script": script, "aspect_ratio": "1:1"})
    assert accepted.status_code == 202
    job = wait_for_job(accepted.json()["job_id"])
    assert job["status"] == "succeeded", job["error"]
    return job


def test_storyboard_add_split_remove_retimes_scenes_and_invalid_requests_do_not_mutate(tmp_path, monkeypatch) -> None:
    job = _new_draft(tmp_path, monkeypatch, script="First sentence has enough words. Second sentence also has enough words.")
    job_id = job["job_id"]
    added = client.post(f"/api/v1/jobs/{job_id}/scenes", json={"narration_text": "A new ending scene has its own narration.", "caption_text": "A new ending caption.", "duration": 3.5})
    assert added.status_code == 201
    new_scene = added.json()["scene"]
    assert new_scene["scene_id"] not in {scene["scene_id"] for scene in job["scenes"]}
    assert new_scene["visual_decision"] == "suggested"
    assert new_scene["selected_visual_id"] is None
    current = client.get(f"/api/v1/jobs/{job_id}").json()
    original = current["scenes"][0]
    before_ids = [scene["scene_id"] for scene in current["scenes"]]
    invalid = client.post(f"/api/v1/jobs/{job_id}/scenes/{original['scene_id']}/split", json={"split_at": original["start"] + 0.01})
    assert invalid.status_code == 422
    assert [scene["scene_id"] for scene in client.get(f"/api/v1/jobs/{job_id}").json()["scenes"]] == before_ids
    split_at = (original["start"] + original["end"]) / 2
    split = client.post(f"/api/v1/jobs/{job_id}/scenes/{original['scene_id']}/split", json={"split_at": split_at})
    assert split.status_code == 200
    pieces = split.json()["scenes"]
    assert len({piece["scene_id"] for piece in pieces}) == 2
    assert all(piece["duration"] > 0 and not piece["selected_visual_id"] for piece in pieces)
    updated = client.get(f"/api/v1/jobs/{job_id}").json()["scenes"]
    assert all(round(left["end"], 6) == round(right["start"], 6) for left, right in zip(updated, updated[1:]))
    assert abs(sum(piece["duration"] for piece in pieces) - original["duration"]) < 0.000001
    deleted = client.delete(f"/api/v1/jobs/{job_id}/scenes/{new_scene['scene_id']}", json={"confirm": True})
    assert deleted.status_code == 200
    assert client.delete(f"/api/v1/jobs/{job_id}/scenes/{pieces[0]['scene_id']}", json={"confirm": False}).status_code == 422
    assert client.get(f"/api/v1/jobs/{job_id}/voice").json()["status"] == "stale"


def test_scene_local_video_upload_is_isolated_validated_and_accepted(tmp_path, monkeypatch) -> None:
    job = _new_draft(tmp_path, monkeypatch, script="One scene for a local upload. Another scene remains separate.")
    job_id, first, second = job["job_id"], job["scenes"][0], job["scenes"][1]
    video = tmp_path / "source.mp4"
    main._run([main._ffmpeg_path(), "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=teal:s=640x360:r=30", "-t", "1", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(video)])
    rejected = client.post(f"/api/v1/jobs/{job_id}/scenes/{first['scene_id']}/local-video", files={"video": ("bad.txt", b"not video", "text/plain")}, data={"license": "Owned", "attribution": "Editor", "rights_confirmed": "true"})
    assert rejected.status_code == 422
    uploaded = client.post(f"/api/v1/jobs/{job_id}/scenes/{first['scene_id']}/local-video", files={"video": ("../source.mp4", video.read_bytes(), "video/mp4")}, data={"license": "Owned test footage", "attribution": "Test editor", "rights_confirmed": "true"})
    assert uploaded.status_code == 201
    scene = uploaded.json()["scene"]
    assert scene["selected_visual"]["provider"] == "scene-local-upload"
    assert scene["visual_decision"] == "accepted"
    preview = client.get(f"/api/v1/jobs/{job_id}/scenes/{first['scene_id']}/visuals/{scene['selected_visual_id']}/preview")
    assert preview.status_code == 200
    assert preview.headers["content-type"].startswith("video/")
    other = client.get(f"/api/v1/jobs/{job_id}/scenes/{second['scene_id']}").json()
    assert other["selected_visual_id"] != scene["selected_visual_id"]
    assert not list((tmp_path / job_id / ".scene-uploads").parent.glob("source.mp4"))


def test_scene_edit_api_validates_scene_ownership_and_fields(tmp_path, monkeypatch) -> None:
    job = _new_draft(tmp_path, monkeypatch)
    scene_id = job["scenes"][0]["scene_id"]
    assert len(job["scenes"][0]["visual_options"]) == 3
    assert job["scenes"][0]["visual_options"][0]["relevance_score"] is None
    assert "not stock footage" in job["scenes"][0]["visual_options"][0]["license"]

    caption = client.patch(f"/api/v1/jobs/{job['job_id']}/scenes/{scene_id}", json={"caption_text": "Human-edited caption."})
    assert caption.status_code == 200
    assert caption.json()["scene"]["caption_text"] == "Human-edited caption."
    assert caption.json()["render_required"] is True

    invalid = client.patch(f"/api/v1/jobs/{job['job_id']}/scenes/scene-999", json={"caption_text": "No such scene"})
    assert invalid.status_code == 404
    assert invalid.json()["detail"]["error"]["code"] == "SCENE_NOT_FOUND"
    empty = client.patch(f"/api/v1/jobs/{job['job_id']}/scenes/{scene_id}", json={"caption_text": "  "})
    assert empty.status_code == 422
    assert empty.json()["detail"]["error"]["code"] == "INVALID_SCENE_TEXT"
    forbidden = client.patch(f"/api/v1/jobs/{job['job_id']}/scenes/{scene_id}", json={"caption_text": "ok", "unknown": "no"})
    assert forbidden.status_code == 422
    assert forbidden.json()["detail"]["error"]["code"] == "VALIDATION_ERROR"


def test_visual_replacement_preserves_text_and_requires_acceptance(tmp_path, monkeypatch) -> None:
    job = _new_draft(tmp_path, monkeypatch, script="Keep this narration. Keep its caption too.")
    scene = job["scenes"][0]
    scene_url = f"/api/v1/jobs/{job['job_id']}/scenes/{scene['scene_id']}"
    narration = "Revised narration remains attached to this scene."
    caption = "Caption survives the visual change."
    edited = client.patch(scene_url, json={"narration_text": narration, "caption_text": caption})
    assert edited.status_code == 200
    alternate = scene["visual_options"][1]["id"]
    replaced = client.patch(scene_url, json={"visual_id": alternate})
    assert replaced.status_code == 200
    assert replaced.json()["scene"]["narration_text"] == narration
    assert replaced.json()["scene"]["caption_text"] == caption
    assert replaced.json()["scene"]["visual_decision"] == "accepted"

    second_scene = job["scenes"][1]
    second_route = f"/api/v1/jobs/{job['job_id']}/scenes/{second_scene['scene_id']}"
    rejected = client.patch(second_route, json={"visual_decision": "rejected", "visual_id": None})
    assert rejected.status_code == 200
    render = client.post(f"/api/v1/jobs/{job['job_id']}/render")
    assert render.status_code == 422
    assert render.json()["detail"]["error"]["code"] == "VISUALS_NEED_REVIEW"
    accepted = client.patch(f"/api/v1/jobs/{job['job_id']}/scenes/{second_scene['scene_id']}", json={"visual_id": second_scene["visual_options"][0]["id"]})
    assert accepted.status_code == 200


def test_invalid_visual_patch_does_not_partially_apply_caption() -> None:
    job = client.post("/api/v1/broll/jobs", data={"script": "Atomic scene edits prevent partial saves."}).json()
    finished = wait_for_job(job["job_id"])
    scene = finished["scenes"][0]
    route = f"/api/v1/jobs/{job['job_id']}/scenes/{scene['scene_id']}"
    response = client.patch(route, json={"caption_text": "Should not be saved.", "visual_id": "another-jobs-visual"})
    assert response.status_code == 422
    current = client.get(route).json()
    assert current["caption_text"] != "Should not be saved."


def test_regenerate_scene_visual_is_fresh_and_keeps_caption(tmp_path, monkeypatch) -> None:
    job = _new_draft(tmp_path, monkeypatch, script="Preserve our text during a new visual suggestion. Keep the second scene unchanged.")
    scene = job["scenes"][0]
    route = f"/api/v1/jobs/{job['job_id']}/scenes/{scene['scene_id']}"
    saved = client.patch(route, json={"narration_text": "Edited scene narration.", "caption_text": "Edited scene caption."})
    assert saved.status_code == 200
    original_options = {item["id"] for item in scene["visual_options"]}
    other_scene_id = job["scenes"][1]["scene_id"]
    other_selected_id = job["scenes"][1]["selected_visual_id"]
    seen = {}

    def capture_exclusions(scene_text, index, job_id, generation=0, ratio="16:9", exclude_asset_ids=None):
        seen.update(scene_text=scene_text, index=index, job_id=job_id, generation=generation, ratio=ratio, excluded=set(exclude_asset_ids or set()))
        return main.MOCK_PROVIDER.visual_options(scene_text, index, job_id, generation)

    monkeypatch.setattr(main, "_visual_options", capture_exclusions)
    refreshed = client.post(f"{route}/regenerate")
    assert refreshed.status_code == 202
    changed = refreshed.json()["scene"]
    assert changed["narration_text"] == "Edited scene narration."
    assert changed["caption_text"] == "Edited scene caption."
    assert changed["visual_decision"] == "suggested"
    assert changed["visual_options"][0]["id"] != scene["visual_options"][0]["id"]
    assert seen["scene_text"] == "Edited scene narration."
    assert seen["generation"] == 1
    assert seen["ratio"] == "1:1"
    assert original_options.issubset(seen["excluded"])
    assert other_selected_id in seen["excluded"]
    assert other_scene_id != scene["scene_id"]
    new_ids = {item["id"] for item in changed["visual_options"]}
    assert new_ids
    assert new_ids.isdisjoint(original_options)
    assert changed["selected_visual_id"] is None
    assert changed["caption_text"] == "Edited scene caption."


def test_find_alternatives_returns_clear_error_when_provider_repeats_only_current_options(tmp_path, monkeypatch) -> None:
    job = _new_draft(tmp_path, monkeypatch, script="This scene has no fresh provider candidates.")
    scene = job["scenes"][0]
    current_options = main.jobs[job["job_id"]]["_visual_catalog"][scene["scene_id"]]

    class RepeatingProvider:
        name = "repeating-test-provider"
        version = "test"

        def visual_options_for_ratio(self, scene_text, index, job_id, generation, ratio, exclude_asset_ids):
            return current_options

    monkeypatch.setattr(main, "FOOTAGE_PROVIDER", RepeatingProvider())
    response = client.post(f"/api/v1/jobs/{job['job_id']}/scenes/{scene['scene_id']}/regenerate")
    assert response.status_code == 422
    assert response.json()["detail"]["error"]["code"] == "PROVIDER_NO_MEDIA"
    assert "no new visual alternatives" in response.json()["detail"]["error"]["message"].lower()


def test_find_alternatives_provider_error_preserves_scene_choices_and_text(tmp_path, monkeypatch) -> None:
    from app.providers import ProviderError

    job = _new_draft(tmp_path, monkeypatch, script="Keep this scene intact if search fails.")
    scene = job["scenes"][0]
    original_options = scene["visual_options"]
    original_caption = scene["caption_text"]

    class UnavailableProvider:
        name = "unavailable-test-provider"
        version = "test"

        def visual_options_for_ratio(self, *args):
            raise ProviderError("PROVIDER_RATE_LIMITED", "Try again after the provider cooldown.", retryable=True, details={"status": 429})

    monkeypatch.setattr(main, "FOOTAGE_PROVIDER", UnavailableProvider())
    response = client.post(f"/api/v1/jobs/{job['job_id']}/scenes/{scene['scene_id']}/regenerate")
    assert response.status_code == 503
    error = response.json()["detail"]["error"]
    assert error["code"] == "PROVIDER_RATE_LIMITED"
    assert error["retryable"] is True
    assert error["details"]["status"] == 429
    unchanged = client.get(f"/api/v1/jobs/{job['job_id']}/scenes/{scene['scene_id']}").json()
    assert unchanged["visual_options"] == original_options
    assert unchanged["caption_text"] == original_caption


def test_full_rerender_uses_latest_edits_in_all_downloads(tmp_path, monkeypatch) -> None:
    job = _new_draft(tmp_path, monkeypatch, script="First sentence to revise. Second sentence to approve.")
    burned_caption_files = []
    original_burn = main._burn_captions_into_video

    def record_burn(ffmpeg, source, captions_path, output, height):
        burned_caption_files.append(captions_path.read_text(encoding="utf-8"))
        return original_burn(ffmpeg, source, captions_path, output, height)

    monkeypatch.setattr(main, "_burn_captions_into_video", record_burn)
    first, second = job["scenes"]
    first_route = f"/api/v1/jobs/{job['job_id']}/scenes/{first['scene_id']}"
    second_route = f"/api/v1/jobs/{job['job_id']}/scenes/{second['scene_id']}"
    latest_narration = "A revised sentence changes the test tone duration."
    latest_caption = "The latest caption is exported."
    response = client.patch(first_route, json={"narration_text": latest_narration, "caption_text": latest_caption, "visual_id": first["visual_options"][2]["id"]})
    assert response.status_code == 200
    assert client.patch(second_route, json={"visual_decision": "accepted"}).status_code == 200
    invalid_ratio = client.post(f"/api/v1/jobs/{job['job_id']}/render", json={"aspect_ratio": "5:4"})
    assert invalid_ratio.status_code == 422
    assert invalid_ratio.json()["detail"]["error"]["code"] == "INVALID_ASPECT_RATIO"
    queued = client.post(f"/api/v1/jobs/{job['job_id']}/render", json={"aspect_ratio": "9:16"})
    assert queued.status_code == 202
    assert queued.json()["full_render"] is True
    finished = wait_for_job(job["job_id"])
    assert finished["status"] == "succeeded", finished["error"]
    timeline = client.get(finished["result"]["timeline"]).json()
    captions = client.get(finished["result"]["captions"]).text
    manifest = client.get(finished["result"]["manifest"]).json()
    assert timeline["scenes"][0]["narration_text"] == latest_narration
    assert timeline["scenes"][0]["caption_text"] == latest_caption
    assert timeline["scenes"][0]["clip"] == first["visual_options"][2]["id"]
    assert "The latest caption is exported." in captions
    assert "A revised sentence changes the test tone duration." not in captions
    assert len(burned_caption_files) == 1
    assert "The latest caption is exported." in burned_caption_files[0]
    assert "A revised sentence changes the test tone duration." not in burned_caption_files[0]
    assert timeline["scenes"][0]["visual_decision"] == "accepted"
    assert manifest["assets"][0]["asset_id"] == first["visual_options"][2]["id"]
    assert abs(timeline["duration_seconds"] - manifest["timings"]["rendered_duration_seconds"]) < 0.001
    video = client.get(finished["result"]["video"])
    assert video.status_code == 200 and b"ftyp" in video.content[:32]
    assert manifest["settings"]["aspect_ratio"] == "9:16"
    assert manifest["settings"]["captions"] == "burned_into_final_mp4"
    assert (tmp_path / job["job_id"] / "video_9x16.mp4").read_bytes() == video.content


def test_uploaded_audio_is_kept_private_for_full_rerender(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(main, "DATA_DIR", tmp_path)
    audio = tmp_path / "source.wav"
    subprocess.run([main._ffmpeg_path(), "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=600:duration=2", str(audio)], check=True)
    accepted = client.post("/api/v1/broll/jobs", data={"aspect_ratio": "16:9"}, files={"audio": ("voice.wav", audio.read_bytes(), "audio/wav")})
    job = wait_for_job(accepted.json()["job_id"])
    assert job["status"] == "succeeded", job["error"]
    scene = job["scenes"][0]
    route = f"/api/v1/jobs/{job['job_id']}/scenes/{scene['scene_id']}"
    assert client.patch(route, json={"narration_text": "Human notes, not a transcript.", "caption_text": "Transcript unavailable · reviewed manually"}).status_code == 200
    assert client.patch(route, json={"visual_decision": "accepted"}).status_code == 200
    assert (tmp_path / job["job_id"] / "source_audio.wav").is_file()
    queued = client.post(f"/api/v1/jobs/{job['job_id']}/render")
    assert queued.status_code == 202
    finished = wait_for_job(job["job_id"])
    assert finished["status"] == "succeeded", finished["error"]
    timeline = client.get(finished["result"]["timeline"]).json()
    assert timeline["scenes"][0]["narration_text"] == "Human notes, not a transcript."
    assert "Transcript unavailable" in client.get(finished["result"]["captions"]).text
    assert client.get(f"/api/v1/jobs/{job['job_id']}/artifacts/source_audio.wav").status_code == 404


def test_failed_rerender_preserves_previous_files_and_reports_failure(tmp_path, monkeypatch) -> None:
    job = _new_draft(tmp_path, monkeypatch, script="Keep the last good video if a rerender fails.")
    first_scene = job["scenes"][0]
    route = f"/api/v1/jobs/{job['job_id']}/scenes/{first_scene['scene_id']}"
    assert client.patch(route, json={"visual_decision": "accepted"}).status_code == 200
    old_video = client.get(job["result"]["video"]).content
    original_run = main.subprocess.run

    def fail_only_scene_render(command, **kwargs):
        if any("color=c=" in part for part in command):
            raise subprocess.TimeoutExpired(command, timeout=kwargs.get("timeout", 120))
        return original_run(command, **kwargs)

    monkeypatch.setattr(main.subprocess, "run", fail_only_scene_render)
    assert client.post(f"/api/v1/jobs/{job['job_id']}/render").status_code == 202
    failed = wait_for_job(job["job_id"])
    assert failed["status"] == "failed"
    assert failed["error"]["code"] == "PROCESSING_FAILED"
    assert failed["result"] is None
    assert (tmp_path / job["job_id"] / "video_1x1.mp4").read_bytes() == old_video


def test_draft_preview_lifecycle_isolation_and_artifacts(tmp_path, monkeypatch) -> None:
    job = _new_draft(tmp_path, monkeypatch, script="First scene of draft. Second scene of draft.")
    job_id = job["job_id"]
    initial_video_path = tmp_path / job_id / "video_1x1.mp4"
    assert initial_video_path.is_file()
    initial_video_bytes = initial_video_path.read_bytes()

    assert job["scenes"][0]["visual_decision"] == "suggested"
    assert job["scenes"][1]["visual_decision"] == "suggested"

    # Saving a candidate for a draft preview must preserve its pending-review
    # state; only the explicit Accept action may allow final rendering.
    first_scene = job["scenes"][0]
    alternate = first_scene["visual_options"][1]["id"]
    revised_narration = "Saved narration appears in the full draft."
    revised_caption = "Saved caption appears in the draft preview."
    saved_candidate = client.patch(
        f"/api/v1/jobs/{job_id}/scenes/{first_scene['scene_id']}",
        json={"visual_id": alternate, "visual_decision": "suggested", "narration_text": revised_narration, "caption_text": revised_caption},
    )
    assert saved_candidate.status_code == 200
    assert saved_candidate.json()["scene"]["selected_visual_id"] == alternate
    assert saved_candidate.json()["scene"]["visual_decision"] == "suggested"

    resp = client.post(f"/api/v1/jobs/{job_id}/draft-preview", json={"aspect_ratio": "1:1"})
    assert resp.status_code == 202
    data = resp.json()
    assert data["status"] == "queued"
    assert data["job_id"] == job_id
    preview_id = data["preview_id"]
    status_url = data["status_url"]

    preview_job = None
    for _ in range(100):
        preview_resp = client.get(status_url)
        assert preview_resp.status_code == 200
        preview_job = preview_resp.json()
        if preview_job["status"] in {"succeeded", "failed"}:
            break
        time.sleep(0.1)

    assert preview_job is not None
    assert preview_job["status"] == "succeeded", preview_job.get("error")
    assert preview_job["job_id"] == job_id
    assert preview_job["preview_id"] == preview_id

    result = preview_job["result"]
    assert result is not None
    assert "video" in result and "captions" in result and "timeline" in result and "manifest" in result
    draft_video_resp = client.get(result["video"])
    assert draft_video_resp.status_code == 200
    assert b"ftyp" in draft_video_resp.content[:32]
    preview_timeline = client.get(result["timeline"]).json()
    assert preview_timeline["scenes"][0]["clip"] == alternate
    assert preview_timeline["scenes"][0]["narration_text"] == revised_narration
    assert preview_timeline["scenes"][0]["caption_text"] == revised_caption
    assert [scene["scene_id"] for scene in preview_timeline["scenes"]] == [scene["scene_id"] for scene in job["scenes"]]
    assert preview_timeline["scenes"][0]["visual_decision"] == "suggested"

    draft_mp4 = tmp_path / preview_id / "video_1x1.mp4"
    assert draft_mp4.is_file()
    assert draft_mp4.read_bytes() == draft_video_resp.content

    assert initial_video_path.read_bytes() == initial_video_bytes

    parent_job = client.get(f"/api/v1/jobs/{job_id}").json()
    assert parent_job["scenes"][0]["visual_decision"] == "suggested"
    assert parent_job["scenes"][1]["visual_decision"] == "suggested"

    render_resp = client.post(f"/api/v1/jobs/{job_id}/render")
    assert render_resp.status_code == 422
    assert render_resp.json()["detail"]["error"]["code"] == "VISUALS_NEED_REVIEW"


def test_refreshed_draft_preview_replaces_caption_artifact_for_each_scene(tmp_path, monkeypatch) -> None:
    job = _new_draft(tmp_path, monkeypatch, script="First narration scene. Second narration scene.")
    job_id = job["job_id"]

    def render_preview() -> dict:
        queued = client.post(f"/api/v1/jobs/{job_id}/draft-preview")
        assert queued.status_code == 202
        for _ in range(100):
            preview = client.get(f"/api/v1/jobs/{job_id}/draft-preview").json()
            if preview["status"] in {"succeeded", "failed"}:
                assert preview["status"] == "succeeded", preview.get("error")
                return preview
            time.sleep(0.1)
        raise AssertionError("draft preview did not finish")

    first_preview = render_preview()
    first_srt = client.get(first_preview["result"]["captions"]).text
    assert "First narration scene." in first_srt
    assert "Second narration scene." in first_srt

    replacement_captions = ["Fresh caption for scene one.", "Fresh caption for scene two."]
    for scene, caption in zip(job["scenes"], replacement_captions):
        updated = client.patch(
            f"/api/v1/jobs/{job_id}/scenes/{scene['scene_id']}",
            json={"caption_text": caption},
        )
        assert updated.status_code == 200

    refreshed_preview = render_preview()
    refreshed_srt = client.get(refreshed_preview["result"]["captions"]).text
    for caption in replacement_captions:
        assert refreshed_srt.count(caption) == 1
    assert "First narration scene." not in refreshed_srt
    assert "Second narration scene." not in refreshed_srt


def test_draft_preview_validation_and_errors(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(main, "DATA_DIR", tmp_path)

    assert client.post("/api/v1/jobs/nonexistent-job-id/draft-preview").status_code == 404
    assert client.get("/api/v1/jobs/nonexistent-job-id/draft-preview").status_code == 404

    job = _new_draft(tmp_path, monkeypatch, script="Scene one. Scene two.")
    job_id = job["job_id"]

    bad_ratio = client.post(f"/api/v1/jobs/{job_id}/draft-preview", json={"aspect_ratio": "4:3"})
    assert bad_ratio.status_code == 422
    assert bad_ratio.json()["detail"]["error"]["code"] == "INVALID_ASPECT_RATIO"

    client.patch(f"/api/v1/jobs/{job_id}/scenes/{job['scenes'][0]['scene_id']}", json={"visual_decision": "rejected", "visual_id": None})
    missing_resp = client.post(f"/api/v1/jobs/{job_id}/draft-preview")
    assert missing_resp.status_code == 422
    assert missing_resp.json()["detail"]["error"]["code"] == "DRAFT_VISUALS_MISSING"


def test_per_scene_candidate_preview_and_isolation(tmp_path, monkeypatch) -> None:
    from app.providers import SceneVisual

    image_path = tmp_path / "test-candidate.png"
    main._run([main._ffmpeg_path(), "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=purple:s=640x360", "-frames:v", "1", str(image_path)])

    visual_1 = SceneVisual(
        asset_id="candidate-img-1", color="purple", asset_path=image_path, provider="local-library",
        source="Test local candidate", source_url="https://example.invalid/candidate-1",
        license="Test license", attribution="Test attribution", ai_generated=False,
        selection_reason="Test candidate", relevance_score=0.9,
        download_url=None, rights_basis="user_attestation", media_type="image",
    )
    visual_2 = SceneVisual(
        asset_id="candidate-img-2", color="blue", asset_path=None, provider="mock-local",
        source="Generated demo card", source_url=None,
        license="Generated mock graphic", attribution="SceneCraft Studio", ai_generated=True,
        selection_reason="Mock demo", relevance_score=None,
        download_url=None, rights_basis="generated_mock", media_type="image",
    )

    class MultiOptionProvider:
        name = "multi-test-provider"
        version = "test"

        def visual_options_for_ratio(self, scene_text, index, job_id, generation, ratio, exclude_asset_ids):
            return [visual_1, visual_2]

    monkeypatch.setattr(main, "DATA_DIR", tmp_path / "jobs")
    monkeypatch.setattr(main, "FOOTAGE_PROVIDER", MultiOptionProvider())
    job = client.post("/api/v1/broll/jobs", data={"script": "Inspect candidate without saving."}).json()
    finished = wait_for_job(job["job_id"])
    assert finished["status"] == "succeeded", finished["error"]

    job_id = finished["job_id"]
    scene_id = finished["scenes"][0]["scene_id"]
    initial_selected = finished["scenes"][0]["selected_visual_id"]
    initial_decision = finished["scenes"][0]["visual_decision"]

    preview_resp = client.get(f"/api/v1/jobs/{job_id}/scenes/{scene_id}/visuals/candidate-img-1/preview")
    assert preview_resp.status_code == 200
    assert preview_resp.headers["content-type"] in {"image/png", "image/x-png"}
    assert preview_resp.content == image_path.read_bytes()

    scene_after_preview = client.get(f"/api/v1/jobs/{job_id}/scenes/{scene_id}").json()
    assert scene_after_preview["selected_visual_id"] == initial_selected
    assert scene_after_preview["visual_decision"] == initial_decision

    mock_resp = client.get(f"/api/v1/jobs/{job_id}/scenes/{scene_id}/visuals/candidate-img-2/preview")
    assert mock_resp.status_code == 422
    assert mock_resp.json()["detail"]["error"]["code"] == "PREVIEW_UNAVAILABLE"

    patch_resp = client.patch(f"/api/v1/jobs/{job_id}/scenes/{scene_id}", json={"visual_id": "candidate-img-1", "visual_decision": "accepted"})
    assert patch_resp.status_code == 200
    assert patch_resp.json()["scene"]["selected_visual_id"] == "candidate-img-1"
    assert patch_resp.json()["scene"]["visual_decision"] == "accepted"


def test_per_scene_candidate_preview_not_found_and_download_failure(tmp_path, monkeypatch) -> None:
    from app.providers import ProviderError, SceneVisual

    image_path = tmp_path / "valid-test-img.png"
    main._run([main._ffmpeg_path(), "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=green:s=640x360", "-frames:v", "1", str(image_path)])

    visual_ok = SceneVisual(
        asset_id="good-asset", color="green", asset_path=image_path, provider="test-prov",
        source="Good candidate", source_url="https://example.invalid/good",
        license="Test license", attribution="Test attribution", ai_generated=False,
        selection_reason="Test candidate", relevance_score=0.9,
        download_url=None, rights_basis="user_attestation", media_type="image",
    )
    visual_fail = SceneVisual(
        asset_id="failing-download-asset", color="red", asset_path=None, provider="test-prov",
        source="Failing candidate", source_url="https://example.invalid/failing",
        license="Test license", attribution="Test attribution", ai_generated=False,
        selection_reason="Test failing download", relevance_score=0.8,
        download_url="https://example.invalid/download.mp4", rights_basis="user_attestation", media_type="video",
    )

    class FailingDownloadProvider:
        name = "test-prov"
        version = "test"

        def visual_options_for_ratio(self, scene_text, index, job_id, generation, ratio, exclude_asset_ids):
            return [visual_ok, visual_fail]

        def materialize(self, selected, job_id):
            raise ProviderError("PROVIDER_DOWNLOAD_FAILED", "Simulated network timeout during candidate preview", retryable=False)

    monkeypatch.setattr(main, "DATA_DIR", tmp_path / "jobs")
    monkeypatch.setattr(main, "FOOTAGE_PROVIDER", FailingDownloadProvider())

    job = client.post("/api/v1/broll/jobs", data={"script": "Failing candidate preview test."}).json()
    finished = wait_for_job(job["job_id"])
    assert finished["status"] == "succeeded", finished.get("error")
    job_id = finished["job_id"]
    scene_id = finished["scenes"][0]["scene_id"]

    assert client.get(f"/api/v1/jobs/bad-job/scenes/{scene_id}/visuals/failing-download-asset/preview").status_code == 404
    assert client.get(f"/api/v1/jobs/{job_id}/scenes/bad-scene/visuals/failing-download-asset/preview").status_code == 404
    assert client.get(f"/api/v1/jobs/{job_id}/scenes/{scene_id}/visuals/nonexistent-asset/preview").status_code == 404

    fail_preview = client.get(f"/api/v1/jobs/{job_id}/scenes/{scene_id}/visuals/failing-download-asset/preview")
    assert fail_preview.status_code == 422
    assert fail_preview.json()["detail"]["error"]["code"] == "PREVIEW_UNAVAILABLE"


def test_stale_export_state_after_saved_edits(tmp_path, monkeypatch) -> None:
    job = _new_draft(tmp_path, monkeypatch, script="Scene one to approve. Scene two to approve.")
    job_id = job["job_id"]
    for scene in job["scenes"]:
        client.patch(f"/api/v1/jobs/{job_id}/scenes/{scene['scene_id']}", json={"visual_decision": "accepted"})
    render_resp = client.post(f"/api/v1/jobs/{job_id}/render")
    assert render_resp.status_code == 202
    final_job = wait_for_job(job_id)
    assert final_job["status"] == "succeeded"
    assert final_job["render_kind"] == "final"
    assert final_job.get("dirty") is False

    first_scene_id = final_job["scenes"][0]["scene_id"]
    client.patch(f"/api/v1/jobs/{job_id}/scenes/{first_scene_id}", json={"caption_text": "Updated caption creates stale export."})
    updated_job = client.get(f"/api/v1/jobs/{job_id}").json()
    assert updated_job["dirty"] is True
