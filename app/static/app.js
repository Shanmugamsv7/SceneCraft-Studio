const form = document.querySelector("#job-form");
const notice = document.querySelector("#notice");
const audioInput = document.querySelector("#audio");
const sceneList = document.querySelector("#scene-list");
const statusMessage = document.querySelector("#status-message");
let currentJobId = null;
let currentStatusUrl = null;
let currentJob = null;
let scenes = [];
let working = false;
let captionObjectUrl = null;
let renderedRatio = null;
let currentResult = null;
let draftPreviewResult = null;
let draftPreviewStatus = null;
let draftPreviewRatio = null;
let activePreviewSceneId = null;
let previewLoadError = false;
let activePreviewView = "draft";
let draftPreviewStale = false;
let voicePreviewObjectUrl = null;
let currentVoiceStatus = null;
let captionRequestId = 0;
let captionTrackId = 0;
const ratioValue = () => document.querySelector('input[name="aspect_ratio"]:checked').value;
let selectedRatio = ratioValue();

const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const api = async (url, options = {}) => {
  const response = await fetch(url, options);
  const body = response.status === 204 ? null : await response.json();
  if (!response.ok) {
    const payload = body?.detail?.error || body?.error;
    const error = new Error(payload?.message || "The request could not be completed.");
    error.code = payload?.code;
    error.retryable = payload?.retryable;
    throw error;
  }
  return body;
};
const ratioLabel = (ratio) => ({ "16:9": "16:9 Landscape", "9:16": "9:16 Portrait", "1:1": "1:1 Square" })[ratio] || ratio;

async function loadProviderInfo() {
  try {
    const info = await api("/api/v1/providers");
    const voiceProviderStatus = document.querySelector("#voice-provider-status");
    if (voiceProviderStatus) {
      voiceProviderStatus.textContent = info.speech_voice_status || info.speech_provider;
      voiceProviderStatus.className = `voice-provider-status ${info.speech_provider === "mock-sine-test-tone" ? "demo" : info.speech_configured === false ? "error" : ""}`;
    }
    const note = document.querySelector(".engine-note");
    note.textContent = `${info.mode.toUpperCase()} MODE · ${info.disclosure}`;
    for (const [provider, url] of Object.entries(info.provider_links || {})) {
      if (!url) continue;
      const makeProviderLink = (text) => {
        const link = document.createElement("a");
        link.href = url;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        link.textContent = text;
        return link;
      };
      const providerName = provider === "wikimedia_commons" ? "Wikimedia Commons" : "Pexels";
      note.append(makeProviderLink(` ${providerName} video source`));
      const credit = document.querySelector("#provider-credit");
      credit.hidden = false;
      if (credit.childNodes.length) credit.append(document.createTextNode(" · "));
      else credit.textContent = "Video sources: ";
      credit.append(makeProviderLink(providerName));
    }
  } catch {
    // Keep the local interface usable if the provider-information endpoint is temporarily unavailable.
  }
}

function clearCaptionTracks() {
  const video = document.querySelector("#preview-video");
  // Invalidate a fetch that started for a previous player state.  Merely
  // removing a <track> is not enough: its SRT request may resolve later.
  captionRequestId += 1;
  captionTrackId += 1;
  // Some browsers retain a detached TextTrack until it is explicitly
  // disabled. Clear both the DOM tracks and the video track list so a
  // refreshed draft cannot display the same native cue twice.
  Array.from(video.textTracks).forEach((textTrack) => { textTrack.mode = "disabled"; });
  video.querySelectorAll("track").forEach((track) => track.remove());
  if (captionObjectUrl) URL.revokeObjectURL(captionObjectUrl);
  captionObjectUrl = null;
}

function setCaptionTrack(srt, expectedVideoUrl) {
  const video = document.querySelector("#preview-video");
  clearCaptionTracks();
  const trackId = captionTrackId;
  const vtt = `WEBVTT\n\n${srt.replace(/^\uFEFF/, "").replace(/(\d{2}:\d{2}:\d{2}),(\d{3})/g, "$1.$2")}`;
  captionObjectUrl = URL.createObjectURL(new Blob([vtt], { type: "text/vtt" }));
  const track = document.createElement("track");
  track.id = "caption-track";
  track.kind = "captions";
  track.srclang = "en";
  track.label = "SceneCraft captions";
  track.default = true;
  // A hidden track loads its cues without painting. It is made visible only
  // once the VTT has loaded for the video currently in the player.
  track.addEventListener("load", () => {
    if (trackId !== captionTrackId || !track.isConnected || video.dataset.exportUrl !== expectedVideoUrl) return;
    // Showing a loaded native track makes the browser evaluate cues against
    // the current media time. Reassigning the same time also makes that
    // synchronization explicit after an asynchronous track load, without
    // restarting the video or relying on a timeout.
    const playbackTime = video.currentTime;
    track.track.mode = "showing";
    if (Number.isFinite(playbackTime) && video.readyState >= 1) video.currentTime = playbackTime;
  }, { once: true });
  video.append(track);
  track.track.mode = "hidden";
  track.src = captionObjectUrl;
}

async function loadCaptionsForPreview(result) {
  const requestId = ++captionRequestId;
  if (!result?.captions) return;
  const expectedVideoUrl = new URL(result.video, window.location.href).href;
  // Make the video selection first. The caption track must be attached to
  // this exact draft/final asset, never the previous result.
  syncMainPreview();
  const video = document.querySelector("#preview-video");
  if (video.dataset.exportUrl !== expectedVideoUrl) return;
  const response = await fetch(result.captions).catch(() => null);
  if (!response?.ok) return;
  const captions = await response.text();
  if (requestId !== captionRequestId) return;
  if (video.dataset.exportUrl !== expectedVideoUrl) return;
  setCaptionTrack(captions, expectedVideoUrl);
  const firstCue = captions.trim().split(/\r?\n\s*\r?\n/)[0] || "";
  document.querySelector("#preview-caption-text").textContent = firstCue.split(/\r?\n/).slice(2).join(" ") || "Caption text is empty.";
}

function previewCaptionResult(job) {
  const draftResult = draftPreviewResult?.video
    ? draftPreviewResult
    : job.draft_preview?.result?.video
      ? job.draft_preview.result
      : job.render_kind !== "final" ? job.result : null;
  const finalResult = job.render_kind === "final" ? job.result : null;
  return window.SceneCraftPreview.selectCaptionResult({ draftResult, finalResult, activeView: activePreviewView });
}

function showNotice(message) {
  notice.textContent = message;
  notice.hidden = !message;
}

function setStatus(status, progress, message) {
  document.querySelector("#job-state").textContent = status ? status[0].toUpperCase() + status.slice(1) : "Ready when you are";
  document.querySelector("#progress").value = progress || 0;
  document.querySelector("#progress-label").textContent = `${progress || 0}%`;
  statusMessage.textContent = message;
  const dot = document.querySelector("#status-dot");
  dot.className = `status-dot ${status === "failed" ? "error" : status === "succeeded" ? "done" : status ? "busy" : "idle"}`;
}

function sceneDirty(scene) {
  return scene._draft?.narration_text !== undefined || scene._draft?.caption_text !== undefined || scene._draft?.visual_decision !== undefined || scene._draft?.visual_id !== undefined;
}

function updateEditState() {
  const dirty = scenes.some(sceneDirty);
  const ratioDirty = renderedRatio && selectedRatio !== renderedRatio;
  document.querySelector("#edit-state").textContent = dirty ? "Unsaved scene changes" : (currentJob?.dirty || ratioDirty) ? "Saved · render required" : "No unsaved changes";
  const previewState = document.querySelector("#preview-state");
  if (previewState) previewState.textContent = renderedRatio && (dirty || currentJob?.dirty || ratioDirty) ? "Last render · edits pending" : renderedRatio ? "Current export" : "No export yet";
  document.querySelector("#save-button").disabled = !dirty || working;
  const canRender = currentJob && ["succeeded", "failed"].includes(currentJob.status) && scenes.length > 0 && scenes.every((scene) => {
    const decision = scene._draft?.visual_decision ?? scene.visual_decision;
    const selected = scene._draft?.visual_id !== undefined ? scene._draft.visual_id : scene.selected_visual_id;
    return decision === "accepted" && Boolean(selected);
  });
  document.querySelector("#render-button").disabled = !canRender || working;
  document.querySelector("#render-hint").textContent = canRender ? "This rebuilds the complete video with your saved edits." : "Accept or replace each suggested visual to enable rendering.";

  const voiceButton = document.querySelector("#generate-voice-button");
  if (voiceButton) {
    const uploadSelected = Boolean(audioInput.files?.length);
    const uploadedJob = currentJob?.voice_input_type === "user_upload";
    voiceButton.disabled = !currentJobId || working || uploadSelected || uploadedJob || ["queued", "running"].includes(currentJob?.status);
    voiceButton.textContent = currentVoiceStatus?.status === "ready" ? "Refresh narration" : "Generate narration";
  }

  const draftButton = document.querySelector("#draft-preview-button");
  const draftStatusEl = document.querySelector("#draft-preview-status");
  const hasDraft = Boolean(draftPreviewResult?.video || currentJob?.draft_preview?.result?.video);
  if (draftButton) {
    draftButton.textContent = hasDraft ? "Refresh draft preview" : "Create draft preview";
    const allHaveVisual = scenes.length > 0 && scenes.every((scene) => {
      const vid = scene._draft?.visual_id !== undefined ? scene._draft.visual_id : scene.selected_visual_id;
      const dec = scene._draft?.visual_decision ?? scene.visual_decision;
      return Boolean(vid) && dec !== "rejected";
    });
    const draftBusy = currentJob?.draft_preview?.status === "queued" || currentJob?.draft_preview?.status === "running" || draftPreviewStatus === "running";
    draftButton.disabled = !currentJobId || !allHaveVisual || working || draftBusy;
    if (draftBusy) {
      draftButton.textContent = "Generating draft preview…";
      if (draftStatusEl) draftStatusEl.textContent = `Draft preview rendering · ${currentJob?.draft_preview?.progress || 0}%`;
    } else if (!allHaveVisual) {
      if (draftStatusEl) draftStatusEl.textContent = "Choose a visual for every scene to preview the complete draft.";
    } else if (hasDraft && (draftPreviewStale || dirty || ratioDirty)) {
      if (draftStatusEl) draftStatusEl.textContent = "Saved edits are pending. Refresh draft preview to update the full video.";
    } else if (hasDraft) {
      if (draftStatusEl) draftStatusEl.textContent = "Draft preview ready. Review playback before final export.";
    } else {
      if (draftStatusEl) draftStatusEl.textContent = "Save a visual choice for every scene to preview the complete draft.";
    }
  }

  const modeBar = document.querySelector("#preview-mode-bar");
  const viewDraftTab = document.querySelector("#view-draft-tab");
  const viewFinalTab = document.querySelector("#view-final-tab");
  const hasFinalVideo = Boolean(currentResult?.video && currentJob?.render_kind === "final");
  const hasDraftVideo = Boolean(draftPreviewResult?.video);
  if (modeBar) {
    modeBar.hidden = !(hasFinalVideo && hasDraftVideo);
    if (viewDraftTab) {
      viewDraftTab.className = `preview-mode-tab ${activePreviewView === "draft" ? "active" : ""}`;
      viewDraftTab.disabled = !hasDraftVideo;
    }
    if (viewFinalTab) {
      viewFinalTab.className = `preview-mode-tab ${activePreviewView === "final" ? "active" : ""}`;
      viewFinalTab.disabled = !hasFinalVideo;
    }
  }

  // Scene cards are not rebuilt every time the global busy state changes.
  // Reconcile these controls here so a card rendered during a request does
  // not remain disabled after the request has finished.
  document.querySelectorAll('.find-alternatives-button[data-scene-id]').forEach((button) => {
    const scene = scenes.find((item) => item.scene_id === button.dataset.sceneId);
    const searching = Boolean(scene?._searchingAlternatives);
    button.disabled = !scene || searching || working;
    button.textContent = searching ? "Searching alternatives…" : "Find alternatives";
  });

  syncMainPreview();
}

function sceneField(scene, name) { return scene._draft?.[name] ?? scene[name] ?? ""; }
function sceneVisualId(scene) { return scene._draft?.visual_id !== undefined ? (scene._draft.visual_id || "") : (scene.selected_visual_id || ""); }
function sceneDecision(scene) { return scene._draft?.visual_decision ?? scene.visual_decision ?? "suggested"; }

function renderScenes() {
  sceneList.replaceChildren();
  document.querySelector("#scene-count").textContent = scenes.length ? `${scenes.length} scenes · review narration, captions, and visual choices` : "Scene suggestions appear after the first render.";
  if (!scenes.length) {
    const empty = document.createElement("div");
    empty.className = "empty-scenes";
    empty.innerHTML = '<span aria-hidden="true">▤</span><p>Your scenes will be organized here.</p><small>Review narration, captions, and visuals individually.</small>';
    sceneList.append(empty);
    updateEditState();
    return;
  }
  scenes.forEach((scene, index) => {
    const card = document.createElement("article");
    card.className = "scene-card";
    card.dataset.sceneId = scene.scene_id;
    if (scene._searchingAlternatives) card.setAttribute("aria-busy", "true");
    card.addEventListener("focusin", () => { activePreviewSceneId = scene.scene_id; syncMainPreview(); });
    const decision = sceneDecision(scene);
    const top = document.createElement("div");
    top.className = "scene-topline";
    top.innerHTML = `<span class="scene-number">SCENE ${String(index + 1).padStart(2, "0")}</span><span class="scene-time">${Number(scene.duration || (scene.end - scene.start)).toFixed(2)} sec · ${Number(scene.start).toFixed(2)}–${Number(scene.end).toFixed(2)}s</span>`;
    const pill = document.createElement("span");
    pill.className = `decision-pill ${decision}`;
    pill.textContent = decision === "suggested" ? "Needs review" : decision;
    top.append(pill);
    card.append(top);

    const fields = document.createElement("div");
    fields.className = "scene-fields";
    for (const [name, label, rows] of [["narration_text", "Narration · editable", 3], ["caption_text", "Caption · editable", 3]]) {
      const wrap = document.createElement("div");
      const fieldLabel = document.createElement("label");
      fieldLabel.htmlFor = `${scene.scene_id}-${name}`;
      fieldLabel.textContent = label;
      const input = document.createElement("textarea");
      input.id = fieldLabel.htmlFor;
      input.rows = rows;
      input.maxLength = name === "caption_text" ? 500 : 2000;
      input.value = sceneField(scene, name);
      input.addEventListener("input", () => {
        scene._draft ||= {};
        scene._draft[name] = input.value;
        draftPreviewStale = Boolean(draftPreviewResult?.video || currentResult?.video);
        activePreviewSceneId = scene.scene_id;
        if (name === "narration_text" && !voicePreviewObjectUrl) {
          showVoiceStatus({ status: "stale", message: "Narration is edited. Save the scene, then generate voice again; the old audio will not be reused." });
        }
        updateEditState();
      });
      wrap.append(fieldLabel, input);
      fields.append(wrap);
    }
    card.append(fields);

    if (scene.error) {
      const unavailable = document.createElement("div");
      unavailable.className = "scene-error";
      unavailable.setAttribute("role", "status");
      const errorTitle = document.createElement("b");
      errorTitle.textContent = scene.error.code === "SCENE_RELEVANCE_REVIEW_REQUIRED" ? "Review the visual match before rendering" : `${scene.error.code || "FOOTAGE_UNAVAILABLE"} · no eligible clip found`;
      const errorMessage = document.createElement("span");
      errorMessage.textContent = scene.error.message || "This scene needs a rights-cleared video clip.";
      const recovery = document.createElement("small");
      recovery.textContent = scene.error.recovery || "Import a rights-cleared local clip with a descriptive filename, then find alternatives for this scene.";
      unavailable.append(errorTitle, errorMessage, recovery);
      card.append(unavailable);
    }
    const visualTitle = document.createElement("p");
    visualTitle.className = "visual-title";
    const hasProviderFootage = (scene.visual_options || []).some((visual) => ["wikimedia-commons", "wikimedia-commons-images", "pexels"].includes(visual.provider));
    const hasLocalFootage = (scene.visual_options || []).some((visual) => visual.provider === "local-library");
    const hasMockGraphics = (scene.visual_options || []).some((visual) => visual.provider === "mock-local");
    visualTitle.textContent = hasProviderFootage ? "PROVIDER-SOURCED VISUAL CANDIDATES · VERIFY SOURCE AND RIGHTS" : hasLocalFootage ? "LOCAL CLIPS · RIGHTS DECLARED BY YOU" : hasMockGraphics ? "DEMO GRAPHICS · GENERATED, NOT FOOTAGE" : "SOURCE VISUAL PREVIEWS";
    card.append(visualTitle);
    if (scene.scene_analysis) {
      const direction = document.createElement("p");
      direction.className = "scene-rationale";
      const analysis = scene.scene_analysis;
      direction.textContent = `Visual direction: ${analysis.subject}; ${analysis.action}; ${analysis.setting}.`;
      card.append(direction);
    }
    const options = document.createElement("div");
    options.className = "visual-options";
    const selectedId = sceneVisualId(scene);
    const inspectedId = scene._inspectingVisualId || selectedId || (scene.visual_options && scene.visual_options[0]?.id);
    for (const visual of scene.visual_options || []) {
      const choice = document.createElement("button");
      choice.type = "button";
      const isSelected = selectedId === visual.id;
      const isInspected = inspectedId === visual.id;
      choice.className = `visual-choice ${isSelected ? "selected" : ""} ${isInspected ? "inspecting" : ""}`;
      choice.setAttribute("aria-pressed", isSelected ? "true" : "false");
      choice.title = `${visual.selection_reason}. ${visual.license}`;
      const swatch = document.createElement(visual.thumbnail_url ? "img" : "span");
      swatch.className = "visual-swatch";
      if (visual.thumbnail_url) {
        swatch.src = visual.thumbnail_url;
        swatch.alt = `Source ${visual.media_type === "image" ? "image" : "video"} thumbnail: ${visual.source || "provider visual"}`;
        swatch.loading = "lazy";
        swatch.referrerPolicy = "no-referrer";
        swatch.style.objectFit = "cover";
      } else {
        if (visual.provider === "local-library") {
          swatch.style.background = "#eef2ee";
          swatch.textContent = "▶";
          swatch.setAttribute("aria-label", "Local clip selected; thumbnail is unavailable");
        } else {
          swatch.style.background = visual.color?.startsWith("0x") ? `#${visual.color.slice(2)}` : (visual.color || "linear-gradient(140deg,#557967,#263e35)");
          swatch.setAttribute("aria-hidden", "true");
        }
      }
      const description = document.createElement("span");
      description.textContent = visual.source || "Generated local color card";
      const small = document.createElement("small");
      small.textContent = visual.provider === "local-library" ? "Imported local footage · user-declared rights" : visual.provider === "mock-local" ? "Generated mock graphic · demo only" : visual.provider === "wikimedia-commons-images" ? "Provider-sourced still image · animated in render" : visual.ai_generated ? "AI-generated media" : "Provider-sourced video";
      description.append(small);
      choice.append(swatch, description);
      choice.addEventListener("click", () => {
        scene._inspectingVisualId = visual.id;
        activePreviewSceneId = scene.scene_id;
        renderScenes();
      });
      options.append(choice);
    }
    card.append(options);

    const scenePreview = document.createElement("div");
    scenePreview.className = "scene-candidate-preview";
    scenePreview.setAttribute("aria-live", "polite");
    const previewHeading = document.createElement("b");
    previewHeading.textContent = "Scene preview · source candidate, not the full draft";
    const previewDecision = document.createElement("small");
    previewDecision.className = "candidate-review-state";
    previewDecision.textContent = selectedId === inspectedId && decision === "accepted" ? "Accepted visual" : "Previewing candidate · not accepted";
    const previewContext = document.createElement("small");
    previewContext.textContent = `${Number(scene.start).toFixed(2)}–${Number(scene.end).toFixed(2)}s · caption: ${sceneField(scene, "caption_text")}`;
    const previewStatus = document.createElement("small");
    previewStatus.className = "scene-preview-status";
    previewStatus.setAttribute("role", "status");
    scenePreview.append(previewHeading, previewDecision, previewContext, previewStatus);
    const previewVisual = (scene.visual_options || []).find((item) => item.id === inspectedId);
    if (previewVisual?.provider === "mock-local") {
      const mock = document.createElement("div");
      mock.className = "scene-candidate-mock";
      mock.style.background = previewVisual.color?.startsWith("0x") ? `#${previewVisual.color.slice(2)}` : (previewVisual.color || "#557967");
      mock.textContent = "Generated mock graphic · demo only";
      scenePreview.append(mock);
    } else if (previewVisual?.media_type === "image") {
      previewStatus.textContent = "Loading source image preview…";
      const previewImage = document.createElement("img");
      previewImage.className = "scene-candidate-image";
      previewImage.alt = `Scene ${index + 1} selected source image preview with subtle motion`;
      previewImage.src = `/api/v1/jobs/${encodeURIComponent(currentJobId)}/scenes/${encodeURIComponent(scene.scene_id)}/visuals/${encodeURIComponent(previewVisual.id)}/preview?_candidate=${encodeURIComponent(previewVisual.id)}`;
      previewImage.onload = () => { previewStatus.textContent = "Source image ready · subtle motion preview"; };
      previewImage.onerror = () => { previewStatus.textContent = "Image preview could not be loaded. Review the source link or choose another candidate."; };
      scenePreview.append(previewImage);
    } else if (previewVisual) {
      const previewMedia = document.createElement(previewVisual.media_type === "image" ? "img" : "video");
      previewMedia.className = "scene-candidate-media";
      previewMedia.setAttribute("aria-label", `Scene ${index + 1} ${previewVisual.media_type} candidate preview`);
      previewMedia.preload = "metadata";
      if (previewMedia.tagName === "VIDEO") { previewMedia.controls = true; previewMedia.playsInline = true; previewMedia.muted = true; }
      previewMedia.src = `/api/v1/jobs/${encodeURIComponent(currentJobId)}/scenes/${encodeURIComponent(scene.scene_id)}/visuals/${encodeURIComponent(previewVisual.id)}/preview?_candidate=${encodeURIComponent(previewVisual.id)}`;
      previewMedia.addEventListener("loadstart", () => { previewStatus.textContent = "Loading source preview…"; }, { once: true });
      previewMedia.addEventListener(previewMedia.tagName === "VIDEO" ? "loadeddata" : "load", () => { previewStatus.textContent = "Source preview ready."; }, { once: true });
      previewMedia.addEventListener("error", () => { previewStatus.textContent = "Source preview failed to load. Try another candidate or Find alternatives."; }, { once: true });
      scenePreview.append(previewMedia);
    } else {
      previewContext.textContent = "Select a candidate above to preview it with this scene’s timing and caption.";
    }
    card.append(scenePreview);

    const actions = document.createElement("div");
    actions.className = "scene-actions";
    const accept = document.createElement("button");
    accept.className = "mini-button accept";
    accept.type = "button";
    const selectedForAccept = (scene.visual_options || []).find((visual) => visual.id === inspectedId);
    accept.textContent = `Accept selected ${selectedForAccept?.media_type === "image" ? "image" : "video"}`;
    const isAlreadyAccepted = selectedId === inspectedId && decision === "accepted";
    accept.disabled = !inspectedId || isAlreadyAccepted;
    accept.addEventListener("click", () => {
      scene._draft ||= {};
      scene._draft.visual_id = inspectedId;
      scene._draft.visual_decision = "accepted";
      draftPreviewStale = Boolean(draftPreviewResult?.video || currentResult?.video);
      activePreviewSceneId = scene.scene_id;
      renderScenes();
    });
    const reject = document.createElement("button");
    reject.className = "mini-button reject";
    reject.type = "button";
    reject.textContent = "Reject suggestion";
    reject.disabled = decision === "rejected" && !selectedId;
    reject.addEventListener("click", () => {
      scene._draft ||= {};
      scene._draft.visual_id = null;
      scene._draft.visual_decision = "rejected";
      draftPreviewStale = Boolean(draftPreviewResult?.video || currentResult?.video);
      activePreviewSceneId = scene.scene_id;
      renderScenes();
    });
    const regenerate = document.createElement("button");
    regenerate.className = "mini-button find-alternatives-button";
    regenerate.dataset.sceneId = scene.scene_id;
    regenerate.type = "button";
    regenerate.disabled = Boolean(scene._searchingAlternatives || working);
    regenerate.textContent = scene._searchingAlternatives ? "Searching alternatives…" : "Find alternatives";
    regenerate.addEventListener("click", () => regenerateVisual(scene));
    const selected = previewVisual;
    const source = document.createElement(selected?.source_url ? "a" : "span");
    source.className = "scene-source";
    if (selected?.source_url) {
      source.href = selected.source_url;
      source.target = "_blank";
      source.rel = "noopener noreferrer";
      source.textContent = `${selected.attribution || selected.source} · View source`;
    } else source.textContent = selected ? `${selected.source} · ${selected.license}` : "No visual selected";
    const rationale = document.createElement("p");
    rationale.className = "scene-rationale";
    rationale.textContent = selected ? `Why this option: ${selected.selection_reason}. Metadata keyword overlap: ${selected.keyword_overlap_score ?? "not calculated"}; scene relevance evidence: ${selected.relevance_score ?? "not calculated"}${selected.selection_requires_review ? " · weak match, human review required" : ""}.` : "No visual is selected. Choose an option before rendering.";
    if (selected) {
      const provenance = document.createElement("p");
      provenance.className = "scene-provenance";
      provenance.textContent = `${selected.attribution || "Creator not identified"} · ${selected.license || "License metadata unavailable"}`;
      card.append(provenance);
    }
    card.append(rationale);
    if (scene._searchingAlternatives || scene._alternativeError) {
      const searchStatus = document.createElement("p");
      searchStatus.className = scene._alternativeError ? "scene-error" : "field-hint";
      searchStatus.setAttribute("role", "status");
      searchStatus.textContent = scene._searchingAlternatives ? "Searching this scene with the selected footage provider…" : scene._alternativeError;
      card.append(searchStatus);
    }
    actions.append(accept, reject, regenerate, source);
    card.append(actions);
    sceneList.append(card);
  });
  updateEditState();
}

function syncMainPreview() {
  const hasFinalVideo = Boolean(currentResult?.video && currentJob?.render_kind === "final");
  const jobDraftResult = currentResult?.video && currentJob?.render_kind !== "final" ? currentResult : null;
  const availableDraftResult = draftPreviewResult?.video ? draftPreviewResult : jobDraftResult;
  const hasDraftVideo = Boolean(availableDraftResult?.video);
  const selectedPreview = window.SceneCraftPreview.selectMainPreview({
    draftResult: availableDraftResult,
    finalResult: hasFinalVideo ? currentResult : null,
    activeView: activePreviewView,
  });
  const activeResult = selectedPreview.result;
  const isDraft = selectedPreview.kind === "draft";
  const dirty = scenes.some(sceneDirty);
  const activeRatio = isDraft ? (draftPreviewRatio || selectedRatio) : (renderedRatio || selectedRatio);
  const ratioDirty = selectedRatio !== activeRatio;
  const stale = Boolean(activeResult?.video && (
    (isDraft && (draftPreviewStale || dirty || ratioDirty)) ||
    (!isDraft && (dirty || currentJob?.dirty || ratioDirty || ["queued", "running", "failed"].includes(currentJob?.status)))
  ));
  const state = window.SceneCraftPreview.resolvePreviewState({ result: activeResult, scenes, activeSceneId: activePreviewSceneId, stale });
  const empty = document.querySelector("#preview-empty");
  const source = document.querySelector("#source-preview");
  const video = document.querySelector("#preview-video");
  const image = document.querySelector("#source-preview-image");
  const swatch = document.querySelector("#source-preview-swatch");
  const note = document.querySelector("#source-preview-note");
  const title = document.querySelector("#preview-title");
  const description = document.querySelector("#preview-description");
  const previewRatio = document.querySelector("#preview-ratio");
  image.hidden = true; swatch.hidden = true; note.hidden = true;
  if (state.kind === "rendered" || state.kind === "stale") {
    empty.hidden = true; source.hidden = true; video.hidden = false;
    title.textContent = isDraft ? (state.kind === "stale" ? "Previous draft preview · edits pending" : "Full draft preview · not final export") : (state.kind === "stale" ? "Previous export - edits need rendering" : "Final rendered video");
    description.textContent = isDraft ? "This playable MP4 uses the saved scene order, visual choices, captions, and timing. Candidate choices remain pending until accepted." : state.kind === "stale" ? "This is the last completed MP4. Your latest scene or format changes are not in this export yet." : "This is the actual final exported MP4, with the accepted visuals and current captions.";
    previewRatio.textContent = activeRatio;
    document.querySelector("#preview-state").textContent = isDraft ? (state.kind === "stale" ? "Draft is older than saved edits" : "Full draft preview ready") : state.kind === "stale" ? "Last export - edits pending" : "Final export ready";
    document.querySelector("#preview-load-error").hidden = !previewLoadError;
    if (activeResult?.video) {
      const baseUrl = new URL(activeResult.video, window.location.href);
      const sameExport = video.dataset.exportUrl === baseUrl.href;
      const versionedUrl = new URL(baseUrl);
      if (!sameExport || !video.src) versionedUrl.searchParams.set("_sc_export", `${currentJobId || "job"}-${Date.now()}`);
      const nextVideoUrl = sameExport && video.src ? video.src : versionedUrl.href;
      if (video.src !== nextVideoUrl) {
        video.dataset.exportUrl = baseUrl.href;
        video.src = nextVideoUrl;
        video.load();
      }
    }
    if (state.kind === "stale") {
      const activeScene = scenes.find((scene) => scene.scene_id === activePreviewSceneId) || scenes[0];
      if (activeScene) document.querySelector("#preview-caption-text").textContent = activeScene._draft?.caption_text ?? activeScene.caption_text ?? "No caption text for this scene.";
    }
    return;
  }
  video.hidden = true; video.pause();
  if (state.kind === "source") {
    empty.hidden = true; source.hidden = false;
    title.textContent = "Selected source preview";
    description.textContent = "Source visual only - not a rendered video.";
    previewRatio.textContent = selectedRatio;
    source.style.aspectRatio = selectedRatio.replace(":", " / ");
    const { visual, scene } = state;
    const mediaLabel = visual.media_type === "image" ? "IMAGE" : "CLIP";
    document.querySelector("#source-preview-label").textContent = visual.provider === "mock-local" ? "GENERATED MOCK GRAPHIC - DEMO ONLY" : visual.media_type === "image" ? "SOURCE IMAGE PREVIEW - NOT THE RENDERED VIDEO" : "SOURCE CLIP THUMBNAIL - NOT THE RENDERED VIDEO";
    document.querySelector("#source-preview-title").textContent = "Scene " + (scenes.indexOf(scene) + 1) + ": " + (visual.source || "Selected source");
    document.querySelector("#source-preview-credit").textContent = (visual.attribution || "Creator not identified") + " - " + (visual.license || "License metadata unavailable");
    if (visual.thumbnail_url) {
      image.src = visual.thumbnail_url;
      image.alt = "Source " + mediaLabel.toLowerCase() + " thumbnail: " + (visual.source || "selected visual");
      image.hidden = false;
    } else if (visual.provider === "mock-local" || visual.ai_generated === true) {
      swatch.style.background = visual.color?.startsWith("0x") ? "#" + visual.color.slice(2) : (visual.color || "linear-gradient(140deg,#557967,#263e35)");
      swatch.hidden = false;
    } else {
      note.textContent = visual.media_type === "image" ? "This still image has no preview thumbnail available." : "This source clip has no preview thumbnail available. Review its source and license details in the storyboard.";
      note.hidden = false;
    }
    document.querySelector("#preview-caption-text").textContent = scene._draft?.caption_text ?? scene.caption_text ?? "No caption text for this scene.";
    document.querySelector("#preview-state").textContent = "Source preview - not rendered";
    return;
  }
  source.hidden = true; empty.hidden = false;
  title.textContent = "Preview"; previewRatio.textContent = selectedRatio;
  const hasScenes = scenes.length > 0;
  document.querySelector("#preview-empty-title").textContent = state.kind === "needs-visual" ? "Choose a visual for this scene" : hasScenes ? "No visual is selected" : "Your preview will appear here";
  document.querySelector("#preview-empty-message").textContent = state.kind === "needs-visual" ? "Select an alternative or find another source before rendering." : hasScenes ? "Select a storyboard scene and choose one of its visual suggestions." : "Create a draft to preview its selected visuals.";
  description.textContent = "Your selected source visual will appear here before export.";
  document.querySelector("#preview-state").textContent = "No export yet";
}

function setRatioDisplay(ratio) {
  const previewRatio = renderedRatio || ratio;
  document.querySelector("#preview-ratio").textContent = previewRatio;
  document.querySelector("#export-ratio").textContent = ratioLabel(ratio);
  const video = document.querySelector("#preview-video");
  video.style.aspectRatio = previewRatio.replace(":", " / ");
  syncMainPreview();
}

function showResults(result, { forceReload = false } = {}) {
  currentResult = result;
  previewLoadError = false;
  const container = document.querySelector("#results");
  container.replaceChildren();
  if (!result) {
    const empty = document.createElement("p");
    empty.className = "empty-export";
    empty.textContent = "No files exported yet";
    container.append(empty);
    document.querySelector("#preview-video").hidden = true;
    document.querySelector("#preview-empty").hidden = false;
    clearCaptionTracks();
    document.querySelector("#preview-caption-text").textContent = "Captions will be available after the render.";
    renderedRatio = null;
    document.querySelector("#preview-ratio").textContent = selectedRatio;
    document.querySelector("#preview-state").textContent = "No export yet";
    document.querySelector("#preview-load-error").hidden = true;
    syncMainPreview();
    return;
  }
  for (const [name, url] of Object.entries(result)) {
    const link = document.createElement("a");
    link.href = url;
    link.download = "";
    const label = document.createElement("span");
    label.textContent = ({ video: currentJob?.render_kind === "final" ? "Final MP4 video" : "Draft MP4 preview", captions: "Captions · SRT", timeline: "Scene timeline · JSON", manifest: "Source manifest · JSON" })[name] || name;
    const icon = document.createElement("span");
    icon.textContent = "↓";
    link.append(label, icon);
    container.append(link);
  }
  const video = document.querySelector("#preview-video");
  if (!result.video) {
    video.hidden = true;
    document.querySelector("#preview-empty-title").textContent = "Rendered video unavailable";
    document.querySelector("#preview-empty-message").textContent = "The job reports success but did not provide an MP4 link. Refresh job status or inspect the job error.";
    document.querySelector("#preview-empty").hidden = false;
    document.querySelector("#preview-state").textContent = "MP4 link missing";
    return;
  }
  const baseUrl = new URL(result.video, window.location.href);
  const sameExport = video.dataset.exportUrl === baseUrl.href;
  const versionedUrl = new URL(baseUrl);
  if (!sameExport || forceReload || !video.src) versionedUrl.searchParams.set("_sc_export", `${currentJobId || "job"}-${Date.now()}`);
  const nextVideoUrl = sameExport && !forceReload && video.src ? video.src : versionedUrl.href;
  const needsLoad = video.src !== nextVideoUrl;
  if (needsLoad) {
    video.dataset.exportUrl = baseUrl.href;
    video.src = nextVideoUrl;
    document.querySelector("#preview-state").textContent = "Loading latest rendered MP4…";
  }
  video.hidden = false;
  document.querySelector("#source-preview").hidden = true;
  document.querySelector("#preview-empty").hidden = true;
  document.querySelector("#preview-load-error").hidden = true;
  if (needsLoad) video.load();
  fetch(result.timeline).then((response) => response.ok ? response.json() : null).then((timeline) => {
    if (timeline) document.querySelector("#export-duration").textContent = `${Number(timeline.duration_seconds).toFixed(2)} seconds`;
  }).catch(() => {});
  fetch(result.manifest).then((response) => response.ok ? response.json() : null).then((manifest) => {
    if (!manifest) return;
    renderedRatio = manifest.settings?.aspect_ratio || selectedRatio;
    setRatioDisplay(selectedRatio);
    updateEditState();
    const providers = new Set((manifest.assets || []).map((asset) => asset.rights_basis));
    document.querySelector("#export-mode").textContent = providers.has("generated_mock") || manifest.rights?.status === "generated_mock_only" ? "Demo graphics" : "Source clips · review credits";
  }).catch(() => {});
}

function loadScenes(job) {
  const incoming = job.scenes || [];
  if (scenes.some(sceneDirty)) {
    // Refresh status without throwing away edits currently being typed.
    for (const updated of incoming) {
      const existing = scenes.find((scene) => scene.scene_id === updated.scene_id);
      if (existing && sceneDirty(existing)) Object.assign(existing, { ...updated, _draft: existing._draft });
      else if (existing) Object.assign(existing, updated);
    }
  } else scenes = incoming.map((scene) => ({ ...scene }));
  renderScenes();
}

async function refreshStatus({ refreshScenes = false } = {}) {
  if (!currentJobId || !currentStatusUrl) return;
  const previousStatus = currentJob?.status;
  const job = await api(currentStatusUrl);
  currentJob = job;
  if (job.draft_preview?.result?.video) {
    draftPreviewResult = job.draft_preview.result;
    draftPreviewStatus = job.draft_preview.status;
    draftPreviewRatio = job.draft_preview.ratio || draftPreviewRatio;
  }
  setStatus(job.status, job.progress, job.error?.message || (job.status === "succeeded" ? "Render is ready. Review the storyboard and save any changes." : `Processing your video · ${job.progress}%`));
  if (job.status === "succeeded") {
    showResults(job.result, { forceReload: previewLoadError || previousStatus !== "succeeded" || currentResult?.video !== job.result?.video });
    if (job.render_kind === "final") {
      activePreviewView = "final";
    }
    await loadCaptionsForPreview(previewCaptionResult(job));
    await refreshVoiceStatus();
    if (refreshScenes) loadScenes(job);
  } else if (job.status === "failed") {
    if (job.scenes?.length) loadScenes(job);
    showNotice(`${job.error?.code || "RENDER_FAILED"}: ${job.error?.message || "The render failed."}`);
    await refreshVoiceStatus();
  }
  updateEditState();
  return job;
}

async function watchJob() {
  while (currentJobId) {
    const job = await refreshStatus({ refreshScenes: true });
    if (["succeeded", "failed"].includes(job.status)) break;
    await wait(700);
  }
}

async function saveEdits() {
  if (!currentJobId || working) return false;
  working = true;
  updateEditState();
  try {
    for (const scene of scenes) {
      if (!sceneDirty(scene)) continue;
      const payload = { ...scene._draft };
      const result = await api(`/api/v1/jobs/${currentJobId}/scenes/${scene.scene_id}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
      Object.assign(scene, result.scene);
      delete scene._draft;
    }
    currentJob = await api(`/api/v1/jobs/${currentJobId}`);
    showNotice("");
    renderScenes();
    return true;
  } catch (error) { showNotice(error.message); return false; }
  finally { working = false; updateEditState(); }
}

function showVoiceStatus(status) {
  currentVoiceStatus = status;
  const message = document.querySelector("#voice-status");
  const player = document.querySelector("#voice-preview");
  const providerStatus = document.querySelector("#voice-provider-status");
  if (status?.status === "ready" && status.audio_url) {
    if (providerStatus) {
      const isDemoTone = status.mode === "demo_test_tone";
      providerStatus.textContent = isDemoTone ? "DEMO - generated 440 Hz test tone, not speech" : `${status.provider || "Speech provider"} narration ready`;
      providerStatus.className = `voice-provider-status ${isDemoTone ? "demo" : ""}`;
    }
    const url = new URL(status.audio_url, window.location.href);
    url.searchParams.set("_voice", status.signature || Date.now());
    if (player.dataset.voiceUrl !== url.href) {
      player.pause();
      player.src = url.href;
      player.dataset.voiceUrl = url.href;
      player.hidden = false;
      player.load();
    }
    message.textContent = `${status.label || "Narration ready"} - ${Number(status.duration_seconds).toFixed(2)} seconds. ${status.timing_strategy || ""}`;
  } else if (status?.status === "uploaded") {
    message.textContent = status.label || "Uploaded voice-over is used as supplied; speech is not transcribed.";
  } else if (status?.status === "running" || status?.status === "queued") {
    message.textContent = status.status === "queued" ? "Voice generation queued…" : "Generating narration audio…";
  } else if (status?.status === "failed") {
    if (providerStatus) {
      const provider = status.provider === "elevenlabs" || status.error?.code?.startsWith("PROVIDER_") ? "ElevenLabs" : "Speech provider";
      const httpStatus = status.error?.details?.status_code;
      providerStatus.textContent = `${provider} request failed${httpStatus ? ` (HTTP ${httpStatus})` : ""}`;
      providerStatus.className = "voice-provider-status error";
    }
    message.textContent = `${status.error?.code || "VOICE_GENERATION_FAILED"}: ${status.error?.message || "Narration audio could not be generated."}`;
  } else if (status?.status === "stale") {
    message.textContent = status.message || "Narration changed. Generate voice again to update the audio and timing.";
  } else {
    message.textContent = status?.message || "Create a script storyboard, then generate narration for its saved scene text.";
  }
  if (status?.status !== "ready" && !voicePreviewObjectUrl) {
    player.pause();
    player.removeAttribute("src");
    player.hidden = true;
    delete player.dataset.voiceUrl;
  }
  updateEditState();
}

async function refreshVoiceStatus() {
  if (!currentJobId) return null;
  try {
    const status = await api(`/api/v1/jobs/${currentJobId}/voice`);
    showVoiceStatus(status);
    return status;
  } catch (error) {
    showVoiceStatus({ status: "failed", error: { code: error.code, message: error.message } });
    return null;
  }
}

async function generateVoice() {
  if (!currentJobId || working) return;
  if (scenes.some(sceneDirty)) {
    const saved = await saveEdits();
    if (!saved) return;
  }
  working = true;
  updateEditState();
  showNotice("");
  showVoiceStatus({ status: "queued" });
  try {
    let status = await api(`/api/v1/jobs/${currentJobId}/voice`, { method: "POST" });
    const statusUrl = status.status_url || `/api/v1/jobs/${currentJobId}/voice`;
    while (status.status === "queued" || status.status === "running") {
      showVoiceStatus(status);
      await wait(350);
      status = await api(statusUrl);
    }
    if (status.status === "failed") throw new Error(`${status.error?.code || "VOICE_GENERATION_FAILED"}: ${status.error?.message || "Retry voice generation."}`);
    showVoiceStatus(status);
    if (status.status === "ready") showNotice("");
  } catch (error) {
    showVoiceStatus({ status: "failed", error: { code: error.code, message: error.message } });
    showNotice(error.message);
  } finally {
    working = false;
    updateEditState();
  }
}

async function regenerateVisual(scene) {
  if (!currentJobId || working) return;
  if (sceneDirty(scene) && !(await saveEdits())) return;
  const target = scenes.find((item) => item.scene_id === scene.scene_id);
  if (!target) return;
  target._alternativeError = null;
  target._searchingAlternatives = true;
  renderScenes();
  working = true;
  updateEditState();
  try {
    const result = await api(`/api/v1/jobs/${currentJobId}/scenes/${target.scene_id}/regenerate`, { method: "POST" });
    Object.assign(target, result.scene);
    delete target._draft;
    target._alternativeError = null;
    currentJob = await api(`/api/v1/jobs/${currentJobId}`);
    currentJob.dirty = true;
    showNotice("");
  } catch (error) {
    const sceneNumber = scenes.indexOf(target) + 1;
    const message = error.code === "PROVIDER_NO_MEDIA"
      ? "No new eligible alternatives were found. Import a rights-cleared local clip or try again after the provider has more results."
      : error.code === "PROVIDER_RATE_LIMITED"
        ? "The footage provider is rate-limiting searches. Wait before trying again, or import a rights-cleared local clip."
        : `Alternative search failed${error.code ? ` (${error.code})` : ""}: ${error.message}`;
    target._alternativeError = message;
    showNotice(`Scene ${sceneNumber}: ${message}`);
  } finally {
    target._searchingAlternatives = false;
    working = false;
    renderScenes();
    updateEditState();
  }
}

async function createOrRefreshDraftPreview() {
  if (!currentJobId || working) return;
  if (scenes.some(sceneDirty)) {
    const saved = await saveEdits();
    if (!saved) return;
  }
  working = true;
  draftPreviewStatus = "running";
  updateEditState();
  showNotice("");
  try {
    const queued = await api(`/api/v1/jobs/${currentJobId}/draft-preview`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ aspect_ratio: selectedRatio }),
    });
    const statusUrl = queued.status_url || `/api/v1/jobs/${currentJobId}/draft-preview`;
    let preview = queued;
    while (preview.status === "queued" || preview.status === "running") {
      await wait(700);
      preview = await api(statusUrl);
      if (currentJob) {
        currentJob.draft_preview = preview;
      }
      draftPreviewStatus = preview.status;
      updateEditState();
    }
    if (preview.status === "succeeded") {
      draftPreviewResult = preview.result;
      draftPreviewStatus = "succeeded";
      draftPreviewRatio = preview.ratio || selectedRatio;
      draftPreviewStale = false;
      activePreviewView = "draft";
      await loadCaptionsForPreview(preview.result);
      showNotice("");
    } else if (preview.status === "failed") {
      draftPreviewStatus = "failed";
      showNotice(`${preview.error?.code || "DRAFT_PREVIEW_FAILED"}: ${preview.error?.message || "Draft preview failed to render."}`);
    }
  } catch (error) {
    draftPreviewStatus = "failed";
    showNotice(error.message);
  } finally {
    working = false;
    updateEditState();
    syncMainPreview();
  }
}

async function renderFullVideo() {
  if (!currentJobId || working) return;
  const saved = await saveEdits();
  if (!saved) return;
  working = true;
  updateEditState();
  showNotice("");
  try {
    const queued = await api(`/api/v1/jobs/${currentJobId}/render`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ aspect_ratio: selectedRatio }) });
    if (!queued.full_render) throw new Error("The server did not confirm a complete video render.");
    currentStatusUrl = queued.status_url;
    currentJob = { ...currentJob, status: "queued", progress: 0 };
    setStatus("queued", 0, "Saved scene edits queued for a complete video rebuild.");
    working = false;
    updateEditState();
    await watchJob();
    activePreviewView = "final";
    syncMainPreview();
  } catch (error) { showNotice(error.message); }
  finally { working = false; updateEditState(); }
}

async function downloadAllCaptions() {
  if (!currentJobId) {
    showNotice("Create a storyboard with captions before downloading an SRT file.");
    return;
  }
  try {
    const response = await fetch(`/api/v1/jobs/${currentJobId}/captions.srt`);
    if (!response.ok) {
      const payload = await response.json().catch(() => null);
      throw new Error(payload?.detail?.error?.message || "Captions are not available to download.");
    }
    const blob = await response.blob();
    if (!blob.size) throw new Error("There are no saved captions to download.");
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = "scenecraft-captions.srt";
    link.click();
    URL.revokeObjectURL(url);
  } catch (error) {
    showNotice(error.message);
  }
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  showNotice("");
  const script = document.querySelector("#script").value.trim();
  const audio = audioInput.files[0];
  if (Boolean(script) === Boolean(audio)) {
    showNotice("Enter a script or choose an audio file, but not both.");
    return;
  }
  const data = new FormData();
  data.append("script", script);
  data.append("aspect_ratio", ratioValue());
  if (audio) data.append("audio", audio);
  working = true;
  const createButton = document.querySelector("#create-button");
  const createButtonLabel = createButton.textContent;
  createButton.disabled = true;
  createButton.textContent = "Generating…";
  setStatus("queued", 0, "Sending your script to the local render queue.");
  try {
    const accepted = await api("/api/v1/broll/jobs", { method: "POST", body: data });
    if (!accepted.job_id || !accepted.status_url) throw new Error("The API response did not include a job ID and status URL.");
    currentJobId = accepted.job_id;
    currentStatusUrl = accepted.status_url;
    currentJob = null;
    document.querySelector("#job-short").textContent = `Job ${currentJobId.slice(0, 8)}`;
    document.querySelector("#project-title").textContent = audio ? audio.name : (script.split(/[.!?]/)[0].slice(0, 42) || "Untitled video draft");
    setRatioDisplay(ratioValue());
    scenes = [];
    renderScenes();
    showResults(null);
    draftPreviewResult = null;
    draftPreviewStatus = null;
    draftPreviewRatio = null;
    draftPreviewStale = false;
    activePreviewView = "draft";
    working = false;
    updateEditState();
    await watchJob();
  } catch (error) { showNotice(error.message); setStatus("failed", 0, error.message); }
  finally { working = false; createButton.disabled = false; createButton.textContent = createButtonLabel; updateEditState(); }
});

document.querySelector("#local-footage-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const importForm = event.currentTarget;
  const button = importForm.querySelector('button[type="submit"]');
  const status = document.querySelector("#local-import-status");
  button.disabled = true;
  status.textContent = "Validating and importing the local clip…";
  const data = new FormData(importForm);
  data.set("rights_confirmed", String(data.has("rights_confirmed")));
  try {
    const imported = await api("/api/v1/footage/imports", { method: "POST", body: data });
    status.textContent = `${imported.filename} imported. ${imported.message} For an existing job, choose “Find alternatives” on the matching scene to search the local library.`;
    importForm.reset();
    showNotice("");
  } catch (error) {
    status.textContent = `Import failed: ${error.message}`;
    showNotice(error.message);
  } finally {
    button.disabled = false;
  }
});

audioInput.addEventListener("change", () => {
  const audio = document.querySelector("#voice-preview");
  if (voicePreviewObjectUrl) URL.revokeObjectURL(voicePreviewObjectUrl);
  voicePreviewObjectUrl = null;
  if (audioInput.files[0]) {
    voicePreviewObjectUrl = URL.createObjectURL(audioInput.files[0]);
    audio.src = voicePreviewObjectUrl;
    audio.hidden = false;
    document.querySelector("#audio-name").textContent = `${audioInput.files[0].name} · spoken words are not transcribed`;
    document.querySelector("#voice-status").textContent = "Uploaded audio preview · this file is used as supplied; speech is not transcribed.";
  } else {
    audio.pause();
    audio.removeAttribute("src");
    audio.hidden = true;
    document.querySelector("#audio-name").textContent = "Audio is not transcribed.";
    if (currentJobId) refreshVoiceStatus();
  }
  updateEditState();
});
document.querySelectorAll('input[name="aspect_ratio"]').forEach((input) => input.addEventListener("change", () => {
  draftPreviewStale = Boolean(draftPreviewResult?.video || currentResult?.video);
  setRatioDisplay(ratioValue());
    selectedRatio = ratioValue();
    if (currentJobId) updateEditState();
}));
document.querySelector("#save-button").addEventListener("click", saveEdits);
document.querySelector("#render-button").addEventListener("click", renderFullVideo);
document.querySelector("#generate-voice-button")?.addEventListener("click", generateVoice);
document.querySelector("#preview-video").addEventListener("loadeddata", () => {
  previewLoadError = false;
  document.querySelector("#preview-load-error").hidden = true;
  document.querySelector("#preview-state").textContent = "Current export · MP4 loaded";
});
document.querySelector("#preview-video").addEventListener("error", () => {
  previewLoadError = true;
  document.querySelector("#preview-load-error").hidden = false;
  document.querySelector("#preview-state").textContent = "Rendered MP4 could not be loaded";
});
document.querySelector("#refresh-button").addEventListener("click", () => refreshStatus({ refreshScenes: true }).catch((error) => showNotice(error.message)));
document.querySelector("#example-script").addEventListener("click", () => {
  document.querySelector("#script").value = "A cyclist rides along a forest trail at sunrise. The path opens onto a quiet lake, where mist drifts across the water.";
  document.querySelector("#script").focus();
  showNotice("");
});
selectedRatio = ratioValue();
setRatioDisplay(selectedRatio);
document.querySelector("#draft-preview-button")?.addEventListener("click", createOrRefreshDraftPreview);
document.querySelector("#download-all-captions")?.addEventListener("click", downloadAllCaptions);
document.querySelector("#view-draft-tab")?.addEventListener("click", () => {
  activePreviewView = "draft";
  updateEditState();
  syncMainPreview();
});
document.querySelector("#view-final-tab")?.addEventListener("click", () => {
  activePreviewView = "final";
  updateEditState();
  syncMainPreview();
});
loadProviderInfo();
