"""
main.py – Apify Actor entrypoint for YouTube Highlight Clipper.

Orchestrates the complete pipeline:
  1. Validate input (video_url, max_clips)
  2. Extract video metadata & subtitles via yt-dlp
  3. Download low-bitrate audio and run audio analysis (parallel)
  4. Fetch live chat replay and run chat analysis (parallel)
  5. Fuse multi-signal scores into composite peaks
  6. Select top non-overlapping highlights with contextual windowing
  7. Enrich with Gemini (refine boundaries, polish transcripts)
  8. Push final results to Apify dataset
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.parse import parse_qs, urlparse

import numpy as np
from apify import Actor

from .audio_analyzer import AudioEvent, extract_and_analyze
from .chat_analyzer import ChatEvent, extract_and_analyze_chat
from .gemini_service import enrich_highlights


# ===========================================================================
# Utility helpers
# ===========================================================================

def extract_video_id(url: str) -> str | None:
    """Extract video ID from various YouTube URL formats."""
    try:
        parsed = urlparse(url)
        if parsed.hostname in ("youtu.be",):
            return parsed.path.lstrip("/").split("?")[0] or None
        if parsed.hostname in ("www.youtube.com", "youtube.com", "m.youtube.com"):
            if parsed.path == "/watch":
                return parse_qs(parsed.query).get("v", [None])[0]
            if parsed.path.startswith("/shorts/"):
                return parsed.path.split("/shorts/")[1].split("?")[0] or None
            if parsed.path.startswith("/live/"):
                return parsed.path.split("/live/")[1].split("?")[0] or None
    except Exception:
        pass
    return None


def seconds_to_hhmmss(seconds: float) -> str:
    """Convert seconds to HH:MM:SS format."""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def hhmmss_to_seconds(hhmmss: str) -> float:
    """Convert HH:MM:SS to seconds."""
    parts = hhmmss.split(":")
    if len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    if len(parts) == 2:
        return int(parts[0]) * 60 + int(parts[1])
    return float(parts[0])


# ===========================================================================
# Proxy & environment configuration
# ===========================================================================

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


def get_apify_proxy_url() -> str | None:
    """
    Get the Apify RESIDENTIAL proxy URL if APIFY_PROXY_PASSWORD is set.
    Format: http://groups-RESIDENTIAL:<password>@proxy.apify.com:8000
    """
    _load_env_file()
    password = os.environ.get("APIFY_PROXY_PASSWORD", "").strip()
    if not password:
        return None
    return f"http://groups-RESIDENTIAL:{password}@proxy.apify.com:8000"


# ===========================================================================
# Metadata & subtitle extraction
# ===========================================================================

def fetch_video_metadata(video_url: str, proxy_url: str | None = None) -> dict[str, Any]:
    """Fetch video metadata (title, duration, etc.) via yt-dlp without downloading."""
    try:
        from yt_dlp import YoutubeDL

        ydl_opts: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "no_playlist": True,
            "no_check_certificate": True,
        }
        if proxy_url:
            ydl_opts["proxy"] = proxy_url

        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(video_url, download=False)

        if not info:
            return {}

        return {
            "video_id": info.get("id", ""),
            "title": info.get("title", ""),
            "duration": info.get("duration", 0),
            "channel": info.get("uploader", ""),
            "is_live": info.get("is_live", False),
            "was_live": info.get("was_live", False),
        }
    except Exception as e:
        Actor.log.warning(f"[Main] Failed to fetch metadata: {e}")
        return {}


def fetch_subtitles(video_url: str, video_id: str, proxy_url: str | None = None) -> dict[float, str]:
    """
    Extract auto-subtitles via yt-dlp as a dict of {start_seconds: text}.
    Returns empty dict if none available.
    """
    try:
        tmp_dir = tempfile.mkdtemp(prefix="yt_subs_")
        output_template = os.path.join(tmp_dir, "subs")

        cmd = [
            "yt-dlp",
            "--no-playlist",
            "--no-check-certificate",
            "--skip-download",
            "--write-auto-sub",
            "--sub-lang", "en",
            "--sub-format", "json3",
            "-o", output_template,
        ]
        if proxy_url:
            cmd.extend(["--proxy", proxy_url])
        cmd.append(video_url)

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)

        # Find the subtitle file
        sub_file = None
        for f in os.listdir(tmp_dir):
            if f.endswith(".json3") or f.endswith(".json"):
                sub_file = os.path.join(tmp_dir, f)
                break

        if not sub_file:
            # Try VTT format as fallback
            cmd_vtt = [
                "yt-dlp",
                "--no-playlist",
                "--no-check-certificate",
                "--skip-download",
                "--write-auto-sub",
                "--sub-lang", "en",
                "--sub-format", "vtt",
                "-o", output_template,
            ]
            if proxy_url:
                cmd_vtt.extend(["--proxy", proxy_url])
            cmd_vtt.append(video_url)
            result = subprocess.run(cmd_vtt, capture_output=True, text=True, timeout=120)
            for f in os.listdir(tmp_dir):
                if f.endswith(".vtt"):
                    sub_file = os.path.join(tmp_dir, f)
                    break

        if not sub_file:
            Actor.log.info("[Main] No subtitles found via yt-dlp.")
            return {}

        # Parse json3 subtitle format
        if sub_file.endswith(".json3") or sub_file.endswith(".json"):
            return _parse_json3_subs(sub_file)
        elif sub_file.endswith(".vtt"):
            return _parse_vtt_subs(sub_file)

        return {}
    except Exception as e:
        Actor.log.warning(f"[Main] Subtitle extraction error: {e}")
        return {}


def _parse_json3_subs(path: str) -> dict[float, str]:
    """Parse YouTube json3 subtitle format."""
    subs: dict[float, str] = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        events = data.get("events", [])
        for event in events:
            start_ms = event.get("tStartMs", 0)
            segs = event.get("segs", [])
            text = "".join(seg.get("utf8", "") for seg in segs).strip()
            if text and text != "\n":
                subs[start_ms / 1000.0] = text
    except Exception as e:
        Actor.log.warning(f"[Main] json3 parse error: {e}")
    return subs


def _parse_vtt_subs(path: str) -> dict[float, str]:
    """Parse VTT subtitle format (basic)."""
    subs: dict[float, str] = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()

        # Simple VTT parser
        blocks = content.split("\n\n")
        for block in blocks:
            lines = block.strip().split("\n")
            for i, line in enumerate(lines):
                if "-->" in line:
                    # Parse start time
                    start_str = line.split("-->")[0].strip()
                    parts = start_str.replace(",", ".").split(":")
                    if len(parts) == 3:
                        secs = (
                            int(parts[0]) * 3600
                            + int(parts[1]) * 60
                            + float(parts[2])
                        )
                    elif len(parts) == 2:
                        secs = int(parts[0]) * 60 + float(parts[1])
                    else:
                        continue
                    text = " ".join(lines[i + 1:]).strip()
                    # Remove VTT tags
                    text = re.sub(r"<[^>]+>", "", text)
                    if text:
                        subs[secs] = text
    except Exception as e:
        Actor.log.warning(f"[Main] VTT parse error: {e}")
    return subs


def get_transcript_for_range(
    subtitles: dict[float, str], start_sec: float, end_sec: float
) -> str:
    """Extract transcript text for a given time range from subtitle dict."""
    texts = []
    for t, text in sorted(subtitles.items()):
        if start_sec <= t <= end_sec:
            texts.append(text)
    return " ".join(texts).strip()


# ===========================================================================
# Multi-signal fusion
# ===========================================================================

def fuse_signals(
    audio_events: list[AudioEvent],
    chat_events: list[ChatEvent],
    video_duration: float,
) -> list[dict[str, Any]]:
    """
    Fuse audio and chat signals into a unified timeline of scored windows.

    Audio contributes up to 60 % of the composite score.
    Chat contributes up to 40 % of the composite score.
    """
    # Build a time-indexed map of scores
    RESOLUTION = 2.5  # seconds per bin (matches hop sizes)
    n_bins = int(video_duration / RESOLUTION) + 1

    audio_scores = np.zeros(n_bins, dtype=np.float64)
    chat_scores = np.zeros(n_bins, dtype=np.float64)
    audio_tags: dict[int, list[str]] = {}
    chat_tags: dict[int, list[str]] = {}

    # Map audio events to bins
    for ae in audio_events:
        idx = int(ae.time_sec / RESOLUTION)
        if 0 <= idx < n_bins:
            audio_scores[idx] = max(audio_scores[idx], ae.score)
            if ae.tags:
                audio_tags.setdefault(idx, []).extend(ae.tags)

    # Map chat events to bins
    for ce in chat_events:
        idx = int(ce.time_sec / RESOLUTION)
        if 0 <= idx < n_bins:
            chat_scores[idx] = max(chat_scores[idx], ce.score)
            if ce.keyword_hits:
                chat_tags.setdefault(idx, []).extend(ce.keyword_hits)

    # Normalize each signal to 0-1 range
    audio_max = audio_scores.max() if audio_scores.max() > 0 else 1.0
    chat_max = chat_scores.max() if chat_scores.max() > 0 else 1.0

    audio_norm = audio_scores / audio_max
    chat_norm = chat_scores / chat_max

    # Weighted fusion
    AUDIO_WEIGHT = 0.60
    CHAT_WEIGHT = 0.40

    composite = audio_norm * AUDIO_WEIGHT + chat_norm * CHAT_WEIGHT

    # If no chat data, audio gets 100 % weight
    if not chat_events:
        composite = audio_norm

    # Scale to 0-100
    composite_max = composite.max() if composite.max() > 0 else 1.0
    composite = (composite / composite_max) * 100

    # Build fused timeline
    fused: list[dict[str, Any]] = []
    for i in range(n_bins):
        if composite[i] > 10:  # minimum threshold
            tags = list(set(audio_tags.get(i, []) + chat_tags.get(i, [])))
            fused.append({
                "time_sec": round(i * RESOLUTION, 2),
                "score": round(float(composite[i]), 2),
                "audio_score": round(float(audio_scores[i]), 2),
                "chat_score": round(float(chat_scores[i]), 2),
                "tags": tags,
            })

    Actor.log.info(
        f"[Fusion] Produced {len(fused)} candidate bins "
        f"(audio bins: {len(audio_events)}, chat bins: {len(chat_events)})"
    )
    return fused


# ===========================================================================
# Peak selection with contextual windowing
# ===========================================================================

def select_top_peaks(
    fused_bins: list[dict[str, Any]],
    max_clips: int,
    video_duration: float,
    min_clip_sec: float = 20.0,
    max_clip_sec: float = 60.0,
    pre_roll_sec: float = 10.0,
    post_roll_sec: float = 6.0,
) -> list[dict[str, Any]]:
    """
    Select top non-overlapping peaks from fused timeline.
    Applies contextual windowing (pre-roll + post-roll) around each peak.
    """
    if not fused_bins:
        return []

    # Sort by score descending
    sorted_bins = sorted(fused_bins, key=lambda b: b["score"], reverse=True)

    selected: list[dict[str, Any]] = []
    used_ranges: list[tuple[float, float]] = []

    for bin_data in sorted_bins:
        if len(selected) >= max_clips:
            break

        peak_time = bin_data["time_sec"]

        # Apply pre-roll and post-roll
        start = max(0, peak_time - pre_roll_sec)
        end = min(video_duration, peak_time + post_roll_sec)

        # Ensure minimum duration
        clip_duration = end - start
        if clip_duration < min_clip_sec:
            # Extend symmetrically
            deficit = min_clip_sec - clip_duration
            start = max(0, start - deficit / 2)
            end = min(video_duration, end + deficit / 2)
            clip_duration = end - start

        # Cap at max duration
        if clip_duration > max_clip_sec:
            # Center around peak
            half = max_clip_sec / 2
            start = max(0, peak_time - half * 0.6)  # bias toward pre-roll
            end = min(video_duration, start + max_clip_sec)
            start = max(0, end - max_clip_sec)

        clip_duration = end - start

        # Check overlap with already selected clips
        overlap = False
        for used_start, used_end in used_ranges:
            if start < used_end and end > used_start:
                overlap = True
                break

        if overlap:
            continue

        used_ranges.append((start, end))
        selected.append({
            "peak_time": peak_time,
            "start_sec": round(start, 2),
            "end_sec": round(end, 2),
            "duration_sec": round(clip_duration, 2),
            "score": bin_data["score"],
            "tags": bin_data["tags"],
        })

    # Re-sort by score
    selected.sort(key=lambda s: s["score"], reverse=True)

    Actor.log.info(
        f"[PeakSelection] Selected {len(selected)} non-overlapping highlights"
    )
    return selected


# ===========================================================================
# Output formatting
# ===========================================================================

def format_output(
    highlights: list[dict[str, Any]],
    video_url: str,
    video_id: str,
    subtitles: dict[float, str],
) -> list[dict[str, Any]]:
    """
    Format selected highlights into the final output schema.
    """
    results: list[dict[str, Any]] = []

    for rank, h in enumerate(highlights, start=1):
        start_sec = h.get("_refined_start", h["start_sec"])
        end_sec = h.get("_refined_end", h["end_sec"])
        duration = round(end_sec - start_sec, 2)

        # Get transcript from subtitles
        transcript = h.get("transcript", "")
        if not transcript:
            transcript = get_transcript_for_range(subtitles, start_sec, end_sec)

        start_hhmmss = seconds_to_hhmmss(start_sec)
        end_hhmmss = seconds_to_hhmmss(end_sec)

        # Build direct timestamp URL
        timestamp_url = f"https://youtu.be/{video_id}?t={int(start_sec)}"

        # Build ffmpeg command
        ffmpeg_cmd = (
            f'ffmpeg -ss {start_hhmmss} -to {end_hhmmss} '
            f'-i "$(yt-dlp -g -f \'bestvideo[ext=mp4]+bestaudio[ext=m4a]'
            f'/best[ext=mp4]/best\' \'{video_url}\')" '
            f'-c copy clip_{rank}.mp4'
        )

        results.append({
            "rank": rank,
            "score": round(h["score"], 1),
            "start_time": start_hhmmss,
            "end_time": end_hhmmss,
            "duration_seconds": duration,
            "detection_reasons": h.get("tags", []),
            "transcript": transcript,
            "direct_timestamp_url": timestamp_url,
            "ffmpeg_command": ffmpeg_cmd,
        })

    return results


# ===========================================================================
# Main Actor entrypoint
# ===========================================================================

async def main() -> None:
    """Apify Actor main entrypoint."""
    loop = asyncio.get_event_loop()
    loop.set_default_executor(ThreadPoolExecutor(max_workers=8))

    async with Actor:
        # ── 1. Parse & validate input ─────────────────────────────────────
        actor_input = await Actor.get_input() or {}
        Actor.log.info(f"[Main] Received input: {json.dumps(actor_input, default=str)}")

        video_url = actor_input.get("video_url", "").strip()
        max_clips = int(actor_input.get("max_clips", 5))
        max_clips = max(1, min(max_clips, 50))

        if not video_url:
            raise ValueError('Input "video_url" is required.')

        video_id = extract_video_id(video_url)
        if not video_id:
            raise ValueError(f'Could not extract video ID from URL: {video_url}')

        # Normalise URL
        canonical_url = f"https://www.youtube.com/watch?v={video_id}"
        Actor.log.info(f"[Main] Processing video: {video_id} (max_clips={max_clips})")

        await Actor.set_status_message(f"Starting analysis of {video_id}...")

        # Configure Apify Residential Proxy if APIFY_PROXY_PASSWORD is set
        proxy_url = get_apify_proxy_url()
        if proxy_url:
            masked_url = re.sub(r":([^@]+)@", ":****@", proxy_url)
            Actor.log.info(f"[Main] Using Apify Residential Proxy: {masked_url}")
            os.environ["HTTP_PROXY"] = proxy_url
            os.environ["HTTPS_PROXY"] = proxy_url
        else:
            Actor.log.info(
                "[Main] APIFY_PROXY_PASSWORD not set. Running with direct connection (no proxy)."
            )

        # ── 2. Fetch metadata ────────────────────────────────────────────
        Actor.log.info("[Main] Step 1/6: Fetching video metadata...")
        metadata = await asyncio.to_thread(fetch_video_metadata, canonical_url, proxy_url)
        video_duration = float(metadata.get("duration", 0))
        video_title = metadata.get("title", video_id)

        if video_duration <= 0:
            Actor.log.warning("[Main] Could not determine video duration, defaulting to 3600s")
            video_duration = 3600.0

        Actor.log.info(
            f"[Main] Video: '{video_title}' | Duration: {seconds_to_hhmmss(video_duration)} "
            f"| Was live: {metadata.get('was_live', False)}"
        )

        await Actor.set_status_message(
            f"Analyzing '{video_title}' ({seconds_to_hhmmss(video_duration)})..."
        )

        # ── 3. Parallel extraction: audio + chat + subtitles ─────────────
        Actor.log.info("[Main] Step 2/6: Extracting audio, chat, and subtitles in parallel...")

        audio_task = asyncio.to_thread(extract_and_analyze, canonical_url, None, proxy_url)
        chat_task = asyncio.to_thread(
            extract_and_analyze_chat, canonical_url, video_duration, proxy_url
        )
        subs_task = asyncio.to_thread(fetch_subtitles, canonical_url, video_id, proxy_url)

        audio_events, chat_events, subtitles = await asyncio.gather(
            audio_task, chat_task, subs_task
        )

        Actor.log.info(
            f"[Main] Extraction complete: "
            f"{len(audio_events)} audio windows, "
            f"{len(chat_events)} chat windows, "
            f"{len(subtitles)} subtitle entries"
        )

        # ── 4. Multi-signal fusion ───────────────────────────────────────
        Actor.log.info("[Main] Step 3/6: Fusing multi-signal scores...")
        await Actor.set_status_message("Fusing audio + chat signals...")

        fused_bins = fuse_signals(audio_events, chat_events, video_duration)

        # ── 5. Peak selection ────────────────────────────────────────────
        Actor.log.info("[Main] Step 4/6: Selecting top non-overlapping peaks...")
        highlights = select_top_peaks(fused_bins, max_clips, video_duration)

        if not highlights:
            Actor.log.warning("[Main] No highlights detected. Pushing empty dataset.")
            await Actor.push_data([])
            await Actor.set_status_message("No highlights detected.")
            return

        # Attach raw transcript & internal tracking fields for Gemini
        for h in highlights:
            h["transcript"] = get_transcript_for_range(
                subtitles, h["start_sec"], h["end_sec"]
            )
            h["_raw_start"] = h["start_sec"]
            h["_raw_end"] = h["end_sec"]
            h["detection_reasons"] = h.get("tags", [])

        # ── 6. Gemini enrichment ─────────────────────────────────────────
        Actor.log.info("[Main] Step 5/6: Enriching highlights with Gemini...")
        await Actor.set_status_message(
            f"Enriching {len(highlights)} highlights with Gemini AI..."
        )
        await enrich_highlights(canonical_url, highlights)

        # ── 7. Format & push output ──────────────────────────────────────
        Actor.log.info("[Main] Step 6/6: Formatting output and pushing to dataset...")
        results = format_output(highlights, canonical_url, video_id, subtitles)

        await Actor.push_data(results)

        # Also save to key-value store
        await Actor.set_value("OUTPUT", results, content_type="application/json")

        Actor.log.info(
            f"[Main] ✅ Done! Pushed {len(results)} highlight clips to dataset."
        )
        await Actor.set_status_message(
            f"Completed: {len(results)} highlights found in '{video_title}'"
        )

        # Log summary
        for r in results:
            Actor.log.info(
                f"  #{r['rank']} [Score: {r['score']}] "
                f"{r['start_time']} → {r['end_time']} "
                f"({r['duration_seconds']}s) "
                f"Triggers: {', '.join(r['detection_reasons'][:3])}"
            )
