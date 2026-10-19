(function (root) {
  "use strict";

  function resolvePreviewState({ result, scenes, activeSceneId, stale }) {
    if (result?.video) {
      return { kind: stale ? "stale" : "rendered", videoUrl: result.video };
    }

    const scene = (scenes || []).find((item) => item.scene_id === activeSceneId) || (scenes || [])[0];
    if (!scene) return { kind: "empty" };
    const options = scene.visual_options || [];
    const decision = scene._draft?.visual_decision ?? scene.visual_decision ?? "suggested";
    const selectedId = scene._draft?.visual_id ?? scene.selected_visual_id;
    const visual = options.find((item) => item.id === selectedId) || (decision === "rejected" ? null : options[0]);
    return visual ? { kind: "source", scene, visual } : { kind: "needs-visual", scene };
  }

  function selectMainPreview({ draftResult, finalResult, activeView }) {
    const preferred = activeView === "final" ? finalResult : draftResult;
    const fallback = activeView === "final" ? draftResult : finalResult;
    const result = preferred?.video ? preferred : fallback?.video ? fallback : null;
    return {
      result,
      kind: !result ? null : result === finalResult ? "final" : "draft",
    };
  }

  function selectCaptionResult({ draftResult, finalResult, activeView }) {
    return selectMainPreview({ draftResult, finalResult, activeView }).result;
  }

  root.SceneCraftPreview = { resolvePreviewState, selectMainPreview, selectCaptionResult };
  if (typeof module !== "undefined" && module.exports) module.exports = root.SceneCraftPreview;
})(typeof globalThis !== "undefined" ? globalThis : this);
