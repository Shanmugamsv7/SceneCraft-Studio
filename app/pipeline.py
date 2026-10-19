from __future__ import annotations

import math
import re
from dataclasses import dataclass


FRAME_RATE = 30
MAX_SCENE_WORDS = 32
SEARCH_STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "been", "begin", "begins", "by", "day", "essential", "for", "from", "has", "have", "how", "in", "into", "is", "it", "its", "life", "lives", "millions", "of", "on", "or", "our", "part", "people", "that", "the", "their", "this", "to", "we", "what", "when", "where", "which", "who", "with", "every", "morning", "changing", "become", "became", "daily", "decision-making", "making", "using", "technology", "through", "runs", "run", "travels", "travel", "along", "beside", "near",
}


def scene_search_phrases(scene_text: str, limit: int = 4) -> list[str]:
    """Create short visual queries from scene actions and concrete subjects.

    This is a small, deterministic rule set rather than a model: terms in the
    narration remain unchanged, while common action/topic patterns suggest
    better search phrases.
    """
    text = " ".join(scene_text.split())
    lower = text.lower()
    phrases: list[str] = []

    def add(phrase: str) -> None:
        if phrase not in phrases and len(phrases) < limit:
            phrases.append(phrase)

    if re.search(r"\b(check(?:ing|s)?|look(?:ing)? at|scroll(?:ing|s)?|browse|us(?:e|ing|es))\b.{0,35}\b(phone|phones|smartphone|smartphones|mobile|cellphone)s?\b", lower) or re.search(r"\b(phone|phones|smartphone|smartphones)\b.{0,30}\b(check(?:ing|s)?|scroll(?:ing|s)?|us(?:e|ing|es))\b", lower):
        add("people checking smartphones")
        add("person using a mobile phone")
        add("hands scrolling on a smartphone")
    if re.search(r"\b(communicat\w*|messag\w*|connect\w*|call\w*)\b", lower):
        add("people communicating with mobile phones")
    if re.search(r"\b(learn\w*|educat\w*|study|studying|student|students|teach\w*)\b", lower):
        add("students learning with digital devices")
    if re.search(r"\b(work\w*|office|job|employee|employees|business)\b", lower):
        add("people working with computers")
    if (
        re.search(r"\b(review\w*|question\w*|check\w*|evaluat\w*)\b", lower)
        and re.search(r"\b(decision\w*|result\w*|output\w*)\b", lower)
        and re.search(r"\b(automated|automatic|algorithm\w*|ai|artificial intelligence|machine learning)\b", lower)
    ):
        add("people reviewing automated system results")
        add("person questioning AI generated decisions")
    if re.search(r"\b(decid\w*|choos\w*|planning|plan\w*)\b", lower):
        add("person making a decision using technology")

    if len(phrases) < limit:
        tokens = [token.lower() for token in re.findall(r"[A-Za-z0-9'-]+", text)]
        useful = [token for token in tokens if token not in SEARCH_STOP_WORDS and len(token) > 2]
        if useful:
            add(" ".join(useful[:6]))
            if len(useful) > 3:
                add(" ".join(useful[:3]))
            if len(useful) > 4:
                add(" ".join(useful[-3:]))
            if len(useful) > 6:
                add(" ".join(useful[3:6]))
    return phrases


def analyze_scene(scene_text: str) -> dict[str, object]:
    """Return conservative, rule-based scene cues without changing narration."""
    lower = scene_text.lower()
    phrases = scene_search_phrases(scene_text)
    subject = "people" if re.search(r"\b(people|person|everyone|we|they|users|students|workers)\b", lower) else "subject not explicit in narration"
    action = "action not confidently extracted"
    if any(word in lower for word in ("check", "checking", "scroll", "scrolling", "browse", "using")) and any(word in lower for word in ("phone", "phones", "smartphone", "mobile")):
        subject, action = "person using a smartphone", "checking or browsing on the phone"
    elif any(word in lower for word in ("communicate", "communicating", "message", "calling")):
        action = "communicating or exchanging messages"
    elif any(word in lower for word in ("learn", "learning", "study", "studying", "teach")):
        subject, action = "learner or student", "learning or studying"
    elif any(word in lower for word in ("work", "working", "office", "job")):
        subject, action = "worker", "working with technology"
    setting = "morning/everyday context" if "morning" in lower or "daily" in lower else "setting not stated in narration"
    if "school" in lower or "classroom" in lower:
        setting = "school or classroom"
    elif "office" in lower or "workplace" in lower:
        setting = "office or workplace"
    return {"subject": subject, "action": action, "setting": setting, "visual_concepts": phrases}


def caption_chunks(text: str, max_line_chars: int = 42, max_lines: int = 2) -> list[str]:
    """Word-wrap captions into readable cues without dropping or reordering words."""
    words = text.split()
    if not words:
        return []
    lines: list[str] = []
    line = ""
    for word in words:
        candidate = f"{line} {word}".strip()
        if line and len(candidate) > max_line_chars:
            lines.append(line)
            line = word
        else:
            line = candidate
    if line:
        lines.append(line)
    return ["\n".join(lines[index:index + max_lines]) for index in range(0, len(lines), max_lines)]


def _search_token(token: str) -> str:
    token = token.lower().strip("'-")
    if token.endswith("ies") and len(token) > 4:
        return token[:-3] + "y"
    if token.endswith("ing") and len(token) > 5:
        return token[:-3]
    if token.endswith("s") and len(token) > 4:
        return token[:-1]
    return token


def score_keyword_overlap(phrase: str, title: str, description: str = "") -> float:
    """Return lexical coverage of a query by provider-supplied metadata.

    This is a retrieval diagnostic only. It does not establish that the media
    depicts the scene's subject or action.
    """
    target_tokens = [_search_token(token) for token in re.findall(r"[A-Za-z0-9'-]+", phrase) if token.lower() not in SEARCH_STOP_WORDS]
    candidate_tokens = {_search_token(token) for token in re.findall(r"[A-Za-z0-9'-]+", f"{title} {description}")}
    synonym_groups = (
        {"smartphone", "phone", "mobile", "cellphone", "iphone", "android"},
        {"check", "look", "scroll", "browse", "use"},
        {"person", "people", "woman", "man", "user", "users", "human"},
        {"device", "devices", "computer", "laptop", "tablet", "phone"},
        {"communicat", "communication", "message", "messaging", "call", "connect"},
        {"learn", "learning", "education", "study", "student", "teach"},
        {"work", "working", "worker", "workers", "employee", "employees", "business"},
        {"decision", "decide", "choose", "choice", "plan"},
    )
    def matches(target: str) -> bool:
        group = next((items for items in synonym_groups if target in items), {target})
        return bool(group & candidate_tokens)
    weights = [2.0 if token in {"smartphone", "phone", "mobile", "cellphone", "check", "scroll", "learn", "student", "work", "computer", "communicat"} else 1.0 for token in target_tokens]
    return sum(weight for token, weight in zip(target_tokens, weights) if matches(token)) / sum(weights) if weights else 0.0


def score_scene_relevance(phrase: str, title: str, description: str = "", scene_context: str | None = None) -> float:
    """Return conservative metadata evidence for the scene, not query overlap.

    Some concepts need multiple signals to be visually meaningful. In
    particular, an "office" or building result is not evidence that people
    are working with computers, and a generic technology mention is not
    evidence of someone using a phone.
    """
    score = score_keyword_overlap(phrase, title, description)

    # Require the subject/action pair for the visual concepts where a broad
    # topical hit can otherwise produce a misleadingly high score.
    action_words = {"check", "checks", "checking", "look", "looks", "looking", "scroll", "scrolls", "scrolling", "browse", "browses", "browsing", "use", "uses", "using"}
    phone_words = {"smartphone", "smartphones", "phone", "phones", "mobile", "cellphone", "iphone", "android"}
    work_words = {"work", "working", "worker", "workers", "employee", "employees", "job", "jobs"}
    computer_words = {"computer", "computers", "laptop", "laptops", "tablet", "tablets"}
    candidate_text = f"{title} {description}"
    candidate_lower = {token.lower() for token in re.findall(r"[A-Za-z0-9'-]+", candidate_text)}
    target_lower = {token.lower() for token in re.findall(r"[A-Za-z0-9'-]+", phrase)}
    context_lower = {token.lower() for token in re.findall(r"[A-Za-z0-9'-]+", scene_context or phrase)}
    specific_terms = context_lower | target_lower
    # Score every alternative query against the complete narration scene too.
    # Otherwise a broad fallback query can discard the phone/action constraint
    # and let an unrelated building win on incidental description words.
    if specific_terms & phone_words and specific_terms & action_words:
        human_words = {"person", "people", "human", "woman", "man", "user", "hand", "hands", "commuter", "commuters"}
        if not (candidate_lower & phone_words and candidate_lower & action_words and candidate_lower & human_words):
            return min(score, 0.24)
    if specific_terms & work_words and specific_terms & computer_words:
        candidate_work = candidate_lower & work_words
        candidate_tool = candidate_lower & computer_words
        # People with a device, or an explicit work action with a computer,
        # are meaningful evidence. An office/building by itself is not.
        candidate_person = candidate_lower & {"person", "people", "worker", "workers", "employee", "employees", "man", "woman", "student", "students"}
        if not candidate_tool or not (candidate_work or candidate_person):
            return min(score, 0.24)
    review_actions = {"review", "reviews", "reviewing", "question", "questions", "questioning", "check", "checking", "evaluate", "evaluating", "inspect", "inspecting"}
    decision_terms = {"decision", "decisions", "result", "results", "output", "outputs", "recommendation", "recommendations"}
    automation_terms = {"automated", "automatic", "automation", "algorithm", "algorithms", "algorithmic", "ai", "artificial", "intelligence", "machine", "learning", "system", "systems", "model", "models"}
    review_scene = bool(context_lower & review_actions and context_lower & decision_terms and context_lower & automation_terms)
    if review_scene:
        people = {"person", "people", "human", "humans", "woman", "man", "user", "users", "analyst", "reviewer", "worker"}
        has_review_action = bool(candidate_lower & review_actions)
        has_decision = bool(candidate_lower & decision_terms)
        has_automation = bool(candidate_lower & automation_terms)
        has_person = bool(candidate_lower & people)
        if not (has_review_action and has_decision and has_automation and has_person):
            return min(score, 0.24)
    return score


@dataclass(frozen=True)
class SceneTiming:
    text: str
    start_frame: int
    end_frame: int

    @property
    def start(self) -> float:
        return self.start_frame / FRAME_RATE

    @property
    def end(self) -> float:
        return self.end_frame / FRAME_RATE


def _split_long_sentence(sentence: str, max_words: int) -> list[str]:
    words = sentence.split()
    chunks: list[str] = []
    while len(words) > max_words:
        limit = max_words
        # Prefer punctuation or a simple phrase boundary close to the limit.
        minimum = max(1, max_words - 8)
        for index in range(max_words, minimum - 1, -1):
            token = words[index - 1].rstrip('"\'”’)]}')
            next_word = words[index].lower().lstrip('"\'“‘([{')
            if token.endswith((",", ";", ":")) or next_word in {"and", "but", "because", "while", "then", "so"}:
                limit = index
                break
        chunks.append(" ".join(words[:limit]).strip())
        words = words[limit:]
    if words:
        chunks.append(" ".join(words).strip())
    return chunks


def segment_script(script: str, max_words: int = MAX_SCENE_WORDS) -> list[str]:
    """Split at sentence/phrase boundaries and cap unusually long scenes."""
    normalized = script.strip()
    if not normalized:
        raise ValueError("Please enter a non-empty script.")
    if max_words < 1:
        raise ValueError("max_words must be positive.")
    sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+|\n+", normalized) if part.strip()]
    scenes: list[str] = []
    for sentence in sentences:
        scenes.extend(_split_long_sentence(sentence, max_words))
    if not scenes:
        raise ValueError("Please enter a non-empty script.")
    if any(len(scene.split()) > max_words for scene in scenes):
        raise ValueError("A narration scene exceeds the supported length.")
    return scenes


def allocate_scene_timings(texts: list[str], total_frames: int) -> list[SceneTiming]:
    """Allocate contiguous frame ranges, weighted by words, with no gaps."""
    if not texts:
        raise ValueError("At least one narration scene is required.")
    if total_frames < len(texts):
        raise ValueError("There are more scenes than video frames available.")
    weights = [max(1, len(text.split())) for text in texts]
    total_weight = sum(weights)
    timings: list[SceneTiming] = []
    cursor = 0
    cumulative = 0
    for index, (text, weight) in enumerate(zip(texts, weights)):
        cumulative += weight
        if index == len(texts) - 1:
            end_frame = total_frames
        else:
            target = round(total_frames * cumulative / total_weight)
            end_frame = max(cursor + 1, min(target, total_frames - (len(texts) - index - 1)))
        timings.append(SceneTiming(text=text, start_frame=cursor, end_frame=end_frame))
        cursor = end_frame
    return timings


def allocate_scene_timings_by_duration(texts: list[str], durations: list[float]) -> list[SceneTiming]:
    """Map measured per-scene speech durations to contiguous frame boundaries."""
    if not texts or len(texts) != len(durations):
        raise ValueError("Each narration scene must have one measured audio duration.")
    if any(not isinstance(duration, (int, float)) or duration <= 0 for duration in durations):
        raise ValueError("Every narration segment must have a positive measured duration.")
    total_duration = sum(durations)
    total_frames = max(len(texts), math.ceil(total_duration * FRAME_RATE))
    timings: list[SceneTiming] = []
    cursor = 0
    cumulative_duration = 0.0
    for index, (text, duration) in enumerate(zip(texts, durations)):
        cumulative_duration += duration
        if index == len(texts) - 1:
            end_frame = total_frames
        else:
            target = round(cumulative_duration * FRAME_RATE)
            end_frame = max(cursor + 1, min(target, total_frames - (len(texts) - index - 1)))
        timings.append(SceneTiming(text=text, start_frame=cursor, end_frame=end_frame))
        cursor = end_frame
    return timings
