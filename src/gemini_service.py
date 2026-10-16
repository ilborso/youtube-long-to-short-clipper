"""
gemini_service.py – Google Gemini API integration for transcript enrichment.

Reads the API key strictly from the GEMINI_API_KEY environment variable.
Provides two main capabilities:
  1. Transcribe a short audio segment when no subtitles are available.
  2. Refine / enrich a detected highlight: adjust narrative boundaries, clean
     up the transcript text, and produce a polished summary.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from apify import Actor


# ---------------------------------------------------------------------------
# Client initialization & environment configuration
# ---------------------------------------------------------------------------

_client = None
_genai_config = None


def _load_env_file() -> None:
    """Load variables from .env file if present and not already set in os.environ."""
    candidates = [
        os.path.join(os.getcwd(), ".env"),
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"),
    ]
    for env_path in candidates:
        if os.path.exists(env_path):
            try:
                with open(env_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#") and "=" in line:
                            k, v = line.split("=", 1)
                            k = k.strip()
                            v = v.strip().strip('"').strip("'")
                            if k and k not in os.environ:
                                os.environ[k] = v
            except Exception:
                pass
            break


def get_model_name() -> str:
    """
    Get the Gemini model name from the MODEL environment variable.
    Defaults to 'gemini-flash-lite-latest'.
    """
    _load_env_file()
    model = os.environ.get("MODEL", "").strip()
    return model if model else "gemini-flash-lite-latest"


def _get_client():
    """Lazy-initialize the Gemini client from the environment variable."""
    global _client, _genai_config

    if _client is not None:
        return _client, _genai_config

    _load_env_file()
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        Actor.log.info(
            "[GeminiService] Note: GEMINI_API_KEY environment variable is not set or empty. "
            "Gemini AI highlights detection will use heuristic fallbacks."
        )
        return None, None

    try:
        from google import genai
        from google.genai import types

        # Suppress AFC warning
        if hasattr(genai, "models") and hasattr(genai.models, "Models"):
            genai.models.Models._logged_afc_warning = True

        _client = genai.Client(api_key=api_key)
        _genai_config = types.GenerateContentConfig(
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            temperature=0.3,
            max_output_tokens=1024,
        )
        Actor.log.info(
            f"[GeminiService] Gemini client initialized successfully (model: {get_model_name()})."
        )
        return _client, _genai_config
    except Exception as e:
        Actor.log.info(f"[GeminiService] Failed to initialize Gemini client: {e}")
        return None, None


# ---------------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------------

REFINE_PROMPT = """\
You are an expert video editor assistant. I detected a potential highlight clip in a YouTube video.

**Video context:**
- Video URL: {video_url}
- Raw detected clip window: {start_time} → {end_time} (about {duration}s)
- Detection triggers: {triggers}
- Raw transcript of segment: {transcript}

**Your task:**
1. Refine the exact `start_time` and `end_time` to form natural narrative boundaries (complete sentences, include setup context).
2. The clip MUST be between 20 and 60 seconds.
3. Include 8-12 seconds of pre-roll before the climax moment for context, and 5-8 seconds post-roll for the reaction/payoff.
4. Clean up and produce a polished, accurate transcript of what is said during the refined window.

**Respond in this EXACT JSON format (no markdown code fences):**
{{
  "refined_start_seconds": <float>,
  "refined_end_seconds": <float>,
  "polished_transcript": "<string>"
}}
"""

SALIENT_TRANSCRIPT_PROMPT = """\
You are an expert video editor and viral short-form content producer.
Your goal is to analyze the timestamped transcription of the YouTube video titled:
"{video_title}" (Duration: {duration_formatted})

Identify the top {max_clips} most salient, engaging, and memorable moments suitable for YouTube Shorts or viral clips.
The maximum duration of each final clip must not exceed {max_length} seconds.

Criteria for Salient Moments:
- High interest, punchline, key insight, dramatic story, shocking revelation, strong humor, or actionable tip.
- Self-contained thought that hooks viewers immediately.
- Clearly identify the core salient interval: `core_start_seconds` and `core_end_seconds`.
- The core moment itself must be roughly 10 to {max_core_length} seconds long so context buffers can be attached without exceeding {max_length}s.
- Make sure none of the selected moments overlap each other.

Timestamped Transcript:
{transcript_content}

Respond in this EXACT JSON format (no markdown code fences, pure JSON only):
{{
  "highlights": [
    {{
      "core_start_seconds": <float>,
      "core_end_seconds": <float>,
      "score": <float between 70.0 and 99.0>,
      "headline": "<punchy hook/title>",
      "detection_reasons": ["<reason1>", "<reason2>"],
      "summary": "<brief summary>",
      "viral_hook": "<punchy 3-5 word on-screen hook with emoji for first 3 seconds>",
      "caption": "<engaging social media caption with emojis and call to action>",
      "hashtags": ["#shorts", "#reels", "#viral", "<relevant_tag1>", "<relevant_tag2>"],
      "virality_score_description": "<explanation of viral potential, retention hook, and emotional trigger>"
    }}
  ]
}}
"""


# ---------------------------------------------------------------------------

# Public API
# ---------------------------------------------------------------------------

async def refine_highlight(
    video_url: str,
    start_seconds: float,
    end_seconds: float,
    triggers: list[str],
    raw_transcript: str,
) -> dict[str, Any]:
    """
    Use Gemini to refine a highlight clip's boundaries and transcript.

    Returns a dict with keys:
      - refined_start_seconds (float)
      - refined_end_seconds (float)
      - polished_transcript (str)

    If Gemini is unavailable, returns the original values unchanged.
    """
    client, config = _get_client()
    if client is None:
        return {
            "refined_start_seconds": start_seconds,
            "refined_end_seconds": end_seconds,
            "polished_transcript": raw_transcript or "",
        }

    duration = end_seconds - start_seconds

    def _fmt_time(s: float) -> str:
        h = int(s // 3600)
        m = int((s % 3600) // 60)
        sec = int(s % 60)
        return f"{h:02d}:{m:02d}:{sec:02d}"

    prompt = REFINE_PROMPT.format(
        video_url=video_url,
        start_time=_fmt_time(start_seconds),
        end_time=_fmt_time(end_seconds),
        duration=f"{duration:.0f}",
        triggers=", ".join(triggers) if triggers else "audio_spike",
        transcript=raw_transcript or "(no transcript available)",
    )

    try:
        model_name = get_model_name()
        response = await asyncio.to_thread(
            client.models.generate_content,
            model=model_name,
            contents=prompt,
            config=config,
        )

        if not response or not response.text:
            Actor.log.info("[GeminiService] Received empty response from Gemini during boundary refinement.")
            return {
                "refined_start_seconds": start_seconds,
                "refined_end_seconds": end_seconds,
                "polished_transcript": raw_transcript or "",
            }

        # Parse JSON from response
        import json
        text = response.text.strip()

        # Strip markdown code fences if present
        if text.startswith("```"):
            lines = text.split("\n")
            # Remove first and last line (fences)
            lines = [l for l in lines if not l.strip().startswith("```")]
            text = "\n".join(lines)

        result = json.loads(text)

        refined_start = float(result.get("refined_start_seconds", start_seconds))
        refined_end = float(result.get("refined_end_seconds", end_seconds))
        polished = str(result.get("polished_transcript", raw_transcript or ""))

        # Sanity checks
        if refined_end <= refined_start:
            refined_start, refined_end = start_seconds, end_seconds

        clip_duration = refined_end - refined_start
        if clip_duration < 15 or clip_duration > 75:
            # Gemini went too far, keep original
            refined_start, refined_end = start_seconds, end_seconds

        return {
            "refined_start_seconds": refined_start,
            "refined_end_seconds": refined_end,
            "polished_transcript": polished,
        }

    except json.JSONDecodeError as e:
        Actor.log.info(f"[GeminiService] Could not parse Gemini JSON response for boundary refinement: {e}")
    except Exception as e:
        Actor.log.info(f"[GeminiService] Gemini API error during boundary refinement: {e}")

    return {
        "refined_start_seconds": start_seconds,
        "refined_end_seconds": end_seconds,
        "polished_transcript": raw_transcript or "",
    }


async def enrich_highlights(
    video_url: str,
    highlights: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Enrich a batch of highlights in parallel using Gemini.
    Modifies and returns the highlights list in-place.
    """
    client, _ = _get_client()
    if client is None:
        Actor.log.info("[GeminiService] Skipping enrichment (no API key).")
        return highlights

    Actor.log.info(f"[GeminiService] Enriching {len(highlights)} highlights with Gemini...")

    semaphore = asyncio.Semaphore(3)  # limit concurrent Gemini calls

    async def _enrich_one(h: dict[str, Any]) -> None:
        async with semaphore:
            result = await refine_highlight(
                video_url=video_url,
                start_seconds=h["_raw_start"],
                end_seconds=h["_raw_end"],
                triggers=h.get("detection_reasons", []),
                raw_transcript=h.get("transcript", ""),
            )
            h["_refined_start"] = result["refined_start_seconds"]
            h["_refined_end"] = result["refined_end_seconds"]
            h["transcript"] = result["polished_transcript"]

    tasks = [_enrich_one(h) for h in highlights]
    await asyncio.gather(*tasks, return_exceptions=True)

    Actor.log.info("[GeminiService] Enrichment complete.")
    return highlights


async def detect_salient_moments_from_transcript(
    video_title: str,
    video_duration: float,
    transcript_text_with_timestamps: str,
    max_clips: int = 5,
    max_length: int = 60,
) -> list[dict[str, Any]]:
    """
    Use Gemini AI to analyze the timestamped transcript and return the most salient moments.
    Returns a list of dicts with:
      - core_start_seconds (float)
      - core_end_seconds (float)
      - score (float)
      - headline (str)
      - detection_reasons (list[str])
      - summary (str)
    """
    client, config = _get_client()
    if client is None:
        Actor.log.info("[GeminiService] Gemini client not configured, skipping AI detection.")
        return []

    def _fmt_time(s: float) -> str:
        h = int(s // 3600)
        m = int((s % 3600) // 60)
        sec = int(s % 60)
        return f"{h:02d}:{m:02d}:{sec:02d}"

    max_core_length = max(10, max_length - 20) if max_length >= 30 else max_length
    prompt = SALIENT_TRANSCRIPT_PROMPT.format(
        video_title=video_title,
        duration_formatted=_fmt_time(video_duration),
        max_clips=max_clips,
        max_length=max_length,
        max_core_length=max_core_length,
        transcript_content=transcript_text_with_timestamps,
    )

    try:
        model_name = get_model_name()
        Actor.log.info(
            f"[GeminiService] Analyzing video transcript with Gemini (model: {model_name}) "
            f"to detect top {max_clips} highlights (max_length={max_length}s, prompt chars: {len(prompt)})..."
        )
        response = await asyncio.to_thread(
            client.models.generate_content,
            model=model_name,
            contents=prompt,
            config=config,
        )

        if not response or not response.text:
            Actor.log.info("[GeminiService] Empty response received from Gemini for salient moments.")
            return []

        import json
        import re
        text = response.text.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            lines = [l for l in lines if not l.strip().startswith("```")]
            text = "\n".join(lines).strip()

        data = None
        try:
            data = json.loads(text)
        except Exception:
            m = re.search(r"\{.*\}", text, re.DOTALL)
            if m:
                try:
                    data = json.loads(m.group(0))
                except Exception:
                    pass

        if not isinstance(data, dict):
            data = {}

        raw_highlights = data.get("highlights", [])

        valid_highlights = []
        for h in raw_highlights:
            start = float(h.get("core_start_seconds", 0))
            end = float(h.get("core_end_seconds", 0))
            if end <= start:
                continue
            valid_highlights.append({
                "core_start_seconds": start,
                "core_end_seconds": end,
                "score": float(h.get("score", 85.0)),
                "headline": str(h.get("headline", "")),
                "detection_reasons": list(h.get("detection_reasons", ["salient_moment"])),
                "summary": str(h.get("summary", "")),
                "viral_hook": str(h.get("viral_hook", "")),
                "caption": str(h.get("caption", "")),
                "hashtags": list(h.get("hashtags", [])),
                "virality_score_description": str(h.get("virality_score_description", "")),
            })

        valid_highlights.sort(key=lambda x: x["score"], reverse=True)
        Actor.log.info(f"[GeminiService] Detected {len(valid_highlights)} salient moments from transcript.")
        return valid_highlights[:max_clips]

    except Exception as e:
        Actor.log.info(f"[GeminiService] Failed to detect salient moments via Gemini: {e}")
        return []


async def transcribe_and_title_clip(
    audio_bytes: bytes,
    mime_type: str = "audio/wav",
    fallback_title: str = "Highlight Clip",
    fallback_transcript: str = "",
) -> dict[str, str]:
    """
    Use Gemini AI to transcribe audio from a cut clip and generate a viral Title.
    Returns a dict with 'title' and 'transcript'.
    """
    client, config = _get_client()
    if client is None or not audio_bytes:
        Actor.log.info("[GeminiService] Gemini client unavailable or empty audio bytes for clip transcription.")
        return {"title": fallback_title, "transcript": fallback_transcript}

    import json
    import re
    from google.genai import types

    prompt = """You are an expert video editor and viral short-form content producer.
Listen to this audio clip:
1. Transcribe the spoken dialogue verbatim and accurately. If there is no speech (only music, laughter, or background sound), describe what is heard (e.g. '[Music playing]', '[Laughter and applause]').
2. Generate an engaging, punchy, viral title for this clip (maximum 6-10 words).

Respond in this EXACT JSON format (pure JSON only, no markdown code fences):
{
  "title": "<Catchy Clip Title>",
  "transcript": "<Accurate transcript of dialogue>"
}"""

    contents = [
        types.Part.from_bytes(data=audio_bytes, mime_type=mime_type),
        prompt,
    ]

    try:
        model_name = get_model_name()
        Actor.log.info(
            f"[GeminiService] Transcribing clip audio ({mime_type}, {len(audio_bytes)//1024} KB) "
            f"& generating title with Gemini ({model_name})..."
        )
        response = await asyncio.to_thread(
            client.models.generate_content,
            model=model_name,
            contents=contents,
            config=config,
        )

        if response and response.text:
            text = response.text.strip()
            Actor.log.info(f"[GeminiService] Gemini clip response received: {text[:120]}...")
            if text.startswith("```"):
                lines = text.split("\n")
                lines = [l for l in lines if not l.strip().startswith("```")]
                text = "\n".join(lines).strip()

            data = None
            try:
                data = json.loads(text)
            except Exception:
                m = re.search(r"\{.*\}", text, re.DOTALL)
                if m:
                    try:
                        data = json.loads(m.group(0))
                    except Exception:
                        pass

            if isinstance(data, dict):
                title = str(data.get("title", "")).strip() or fallback_title
                transcript = str(data.get("transcript", "")).strip() or fallback_transcript
                return {"title": title, "transcript": transcript}
            elif text:
                return {"title": fallback_title, "transcript": text}
    except Exception as e:
        Actor.log.info(f"[GeminiService] Clip audio transcription error: {e}")

    return {"title": fallback_title, "transcript": fallback_transcript}


def generate_fallback_social_copy_kit(
    title: str,
    transcript: str,
    score: float = 85.0,
    detection_reasons: list[str] | None = None,
) -> dict[str, Any]:
    """Generate high-quality algorithmic fallback for the Social Media Copy Kit."""
    import re
    reasons_str = ", ".join(detection_reasons or ["high engagement spike"])
    clean_words = [w.capitalize() for w in re.sub(r"[^\w\s]", "", title).split() if len(w) > 2]
    topic_tag = f"#{''.join(clean_words[:2])}" if clean_words else "#Trending"

    clean_hook = title.strip()
    if clean_hook.lower().startswith("highlight at") or not clean_hook:
        clean_hook = "Wait for this moment... 🤯"
    elif not any(c in clean_hook for c in ["!", "?", "🔥", "🤯", "👀"]):
        clean_hook = f"{clean_hook} 😱"

    quote_snippet = transcript.strip()[:140] if transcript else "Watch this clip to the end!"
    return {
        "viral_hook": clean_hook[:80],
        "Caption": (
            f"You won't believe what happened! 👀🔥\n\n"
            f"\"{quote_snippet}...\"\n\n"
            f"What are your thoughts on this? Drop a comment below! 👇"
        ),
        "Hashtag": ["#shorts", "#reels", "#tiktok", "#viral", "#fyp", topic_tag],
        "Virality_score_description": (
            f"Score {score:.1f}/100: High engagement moment triggered by {reasons_str} "
            f"with strong emotional payoff and high comment potential."
        ),
    }


async def generate_social_copy_kit(
    title: str,
    transcript: str,
    score: float = 85.0,
    detection_reasons: list[str] | None = None,
) -> dict[str, Any]:
    """
    Use Gemini AI to generate a complete Social Media Copy Kit for a clip:
    - viral_hook (opening 3-second hook)
    - Caption (social media description with emojis & CTA)
    - Hashtag (list of 5-8 relevant and trending hashtags)
    - Virality_score_description (rationale for the virality score)
    """
    fallback = generate_fallback_social_copy_kit(title, transcript, score, detection_reasons)
    client, config = _get_client()
    if client is None:
        return fallback

    prompt = f"""You are an elite viral social media editor for TikTok, YouTube Shorts, and Instagram Reels.
Given this video highlight clip:
- Title: "{title}"
- Transcript: "{transcript[:600]}"
- Virality Score: {score}/100
- Triggers: {', '.join(detection_reasons or ['salient_moment'])}

Generate a complete Social Media Copy Kit for this clip.
Respond in this EXACT JSON format (no markdown code fences, pure JSON only):
{{
  "viral_hook": "<punchy 3-5 word on-screen hook with emoji for the first 3 seconds>",
  "Caption": "<engaging social media description with emojis, narrative context, and call to action>",
  "Hashtag": ["#shorts", "#reels", "#tiktok", "<3-5 niche tags>"],
  "Virality_score_description": "<concise explanation of viral retention factors and audience emotional triggers>"
}}"""

    try:
        model_name = get_model_name()
        Actor.log.info(
            f"[GeminiService] Generating Social Media Copy Kit via Gemini (model: {model_name}) for '{title[:50]}'..."
        )
        response = await asyncio.to_thread(
            client.models.generate_content,
            model=model_name,
            contents=prompt,
            config=config,
        )
        if response and response.text:
            import json
            import re
            text = response.text.strip()
            if text.startswith("```"):
                lines = text.split("\n")
                lines = [l for l in lines if not l.strip().startswith("```")]
                text = "\n".join(lines).strip()

            data = None
            try:
                data = json.loads(text)
            except Exception:
                m = re.search(r"\{.*\}", text, re.DOTALL)
                if m:
                    try:
                        data = json.loads(m.group(0))
                    except Exception:
                        pass

            if isinstance(data, dict):
                v_hook = str(data.get("viral_hook", "")).strip() or fallback["viral_hook"]
                v_caption = str(data.get("Caption", data.get("caption", ""))).strip() or fallback["Caption"]
                v_tags = list(data.get("Hashtag", data.get("hashtags", []))) or fallback["Hashtag"]
                v_desc = str(data.get("Virality_score_description", data.get("virality_score_description", ""))).strip() or fallback["Virality_score_description"]
                Actor.log.info(
                    f"[GeminiService] Generated Social Media Copy Kit: Hook='{v_hook}' | Hashtags={v_tags}"
                )
                return {
                    "viral_hook": v_hook,
                    "Caption": v_caption,
                    "Hashtag": v_tags,
                    "Virality_score_description": v_desc,
                }
    except Exception as e:
        Actor.log.info(f"[GeminiService] Failed to generate social copy kit: {e}")

    return fallback



