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
# Client initialization
# ---------------------------------------------------------------------------

_client = None
_genai_config = None


def _get_client():
    """Lazy-initialize the Gemini client from the environment variable."""
    global _client, _genai_config

    if _client is not None:
        return _client, _genai_config

    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        Actor.log.warning(
            "[GeminiService] GEMINI_API_KEY environment variable is not set or empty. "
            "Gemini enrichment will be skipped."
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
        Actor.log.info("[GeminiService] Gemini client initialized successfully.")
        return _client, _genai_config
    except Exception as e:
        Actor.log.error(f"[GeminiService] Failed to initialize Gemini client: {e}")
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

TRANSCRIBE_PROMPT = """\
Transcribe the following audio segment from a YouTube video.
The segment is approximately {duration} seconds long, starting at {start_time}.

Provide ONLY the transcription text, no timestamps or speaker labels.
Keep it verbatim and accurate.
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
        response = await asyncio.to_thread(
            client.models.generate_content,
            model="gemini-2.0-flash",
            contents=prompt,
            config=config,
        )

        if not response or not response.text:
            Actor.log.warning("[GeminiService] Empty response from Gemini.")
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
        Actor.log.warning(f"[GeminiService] Could not parse Gemini JSON response: {e}")
    except Exception as e:
        Actor.log.warning(f"[GeminiService] Gemini API error: {e}")

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
