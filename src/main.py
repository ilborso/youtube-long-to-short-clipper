"""
main.py – Apify Actor entrypoint for YouTube Highlight Clipper.

Extracts video transcription (subtitles) via yt-dlp without downloading audio or video.
Uses semantic AI analysis (Google Gemini) and algorithmic transcript heuristics
to detect the most salient highlight moments, adding 10 seconds of pre-roll
and 10 seconds of post-roll context to each selected interval.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.parse import parse_qs, urlparse

from apify import Actor

from .gemini_service import detect_salient_moments_from_transcript


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


def get_apify_proxy_url(session_id: str | None = None) -> str | None:
    """
    Get the Apify RESIDENTIAL proxy URL if APIFY_PROXY_PASSWORD is set.
    Optionally attaches a sticky session ID to maintain the same residential IP across calls.
    Format: http://groups-RESIDENTIAL[,session-<session_id>]:<password>@proxy.apify.com:8000
    """
    _load_env_file()
    password = os.environ.get("APIFY_PROXY_PASSWORD", "").strip()
    if not password:
        return None
    if session_id:
        return f"http://groups-RESIDENTIAL,session-{session_id}:{password}@proxy.apify.com:8000"
    return f"http://groups-RESIDENTIAL:{password}@proxy.apify.com:8000"


def _get_cookie_args() -> list[str]:
    """Return yt-dlp cookie arguments if cookies are configured."""
    candidates = [
        "cookies.txt",
        os.path.join(os.getcwd(), "cookies.txt"),
        "/usr/src/app/cookies.txt",
    ]
    for p in candidates:
        if os.path.exists(p) and os.path.getsize(p) > 10:
            return ["--cookies", p]

    raw_cookies = os.environ.get("YOUTUBE_COOKIES", "").strip()
    if raw_cookies:
        tmp_c = os.path.join(tempfile.gettempdir(), "yt_cookies.txt")
        try:
            with open(tmp_c, "w", encoding="utf-8") as f:
                f.write(raw_cookies)
            return ["--cookies", tmp_c]
        except Exception:
            pass

    b64_cookies = os.environ.get("YOUTUBE_COOKIES_BASE64", "").strip()
    if b64_cookies:
        try:
            import base64
            decoded = base64.b64decode(b64_cookies).decode("utf-8", errors="ignore")
            tmp_c = os.path.join(tempfile.gettempdir(), "yt_cookies.txt")
            with open(tmp_c, "w", encoding="utf-8") as f:
                f.write(decoded)
            return ["--cookies", tmp_c]
        except Exception:
            pass

    return []


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
            "extractor_args": {"youtube": {"player_client": ["android", "mweb", "web"]}},
        }
        cookie_args = _get_cookie_args()
        if cookie_args and len(cookie_args) == 2:
            ydl_opts["cookiefile"] = cookie_args[1]
        if proxy_url:
            ydl_opts["proxy"] = proxy_url

        with YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(video_url, download=False)

        if not info:
            return {}

        # Detect best subtitle language
        available_subs = list((info.get("subtitles") or {}).keys())
        auto_subs = list((info.get("automatic_captions") or {}).keys())
        all_subs = available_subs + auto_subs

        pref_lang = None
        # 1. Prefer original auto-sub language (e.g. it-orig, en-orig)
        for lang in all_subs:
            if lang.endswith("-orig"):
                pref_lang = lang
                break
        # 2. Prefer it, en, es, fr, de
        if not pref_lang:
            for lang in ["it", "en", "es", "fr", "de"]:
                if lang in all_subs:
                    pref_lang = lang
                    break
        # 3. Fallback to first available
        if not pref_lang and all_subs:
            pref_lang = all_subs[0]

        return {
            "video_id": info.get("id", ""),
            "title": info.get("title", ""),
            "duration": info.get("duration", 0),
            "channel": info.get("uploader", ""),
            "is_live": info.get("is_live", False),
            "was_live": info.get("was_live", False),
            "pref_lang": pref_lang,
        }
    except Exception as e:
        Actor.log.warning(f"[Main] Failed to fetch metadata: {e}")
        return {}


def fetch_subtitles(
    video_url: str,
    video_id: str,
    proxy_url: str | None = None,
    pref_lang: str | None = None,
) -> tuple[dict[float, str], list[dict[str, Any]]]:
    """
    Extract auto-subtitles or manual subtitles via yt-dlp.
    Returns:
      (dict of {start_seconds: text}, list of {"start": s, "end": e, "text": t})
    Returns empty dict and list if none available.
    """
    try:
        tmp_dir = tempfile.mkdtemp(prefix="yt_subs_")
        output_template = os.path.join(tmp_dir, "subs")
        cookie_args = _get_cookie_args()

        target_lang = pref_lang or "it-orig,it,en-orig,en"

        cmd = [
            "yt-dlp",
            "--no-playlist",
            "--no-check-certificate",
            "--skip-download",
            "--write-auto-sub",
            "--write-sub",
            "--sub-langs", target_lang,
            "--sub-format", "json3",
            "--extractor-args", "youtube:player_client=android,mweb,web",
            "-o", output_template,
        ]
        cmd.extend(cookie_args)
        if proxy_url:
            cmd.extend(["--proxy", proxy_url])
        cmd.append(video_url)

        Actor.log.info(f"[Main] Fetching subtitles (lang={target_lang})...")
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)

        # Look for downloaded subtitle files
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
                "--write-sub",
                "--sub-langs", target_lang,
                "--sub-format", "vtt",
                "--extractor-args", "youtube:player_client=android,mweb,web",
                "-o", output_template,
            ]
            cmd_vtt.extend(cookie_args)
            if proxy_url:
                cmd_vtt.extend(["--proxy", proxy_url])
            cmd_vtt.append(video_url)
            result = subprocess.run(cmd_vtt, capture_output=True, text=True, timeout=120)
            for f in os.listdir(tmp_dir):
                if f.endswith(".vtt"):
                    sub_file = os.path.join(tmp_dir, f)
                    break

        if not sub_file:
            Actor.log.warning("[Main] No subtitles found via yt-dlp.")
            return {}, []

        Actor.log.info(f"[Main] Subtitle file extracted: {os.path.basename(sub_file)}")
        if sub_file.endswith(".json3") or sub_file.endswith(".json"):
            return _parse_json3_subs(sub_file)
        elif sub_file.endswith(".vtt"):
            return _parse_vtt_subs(sub_file)

        return {}, []
    except Exception as e:
        Actor.log.warning(f"[Main] Subtitle extraction error: {e}")
        return {}, []


def _parse_json3_subs(path: str) -> tuple[dict[float, str], list[dict[str, Any]]]:
    """Parse YouTube json3 subtitle format into dict and segments list."""
    subs: dict[float, str] = {}
    segments: list[dict[str, Any]] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        events = data.get("events", [])
        for event in events:
            start_ms = event.get("tStartMs", 0)
            duration_ms = event.get("dDurationMs", 0)
            segs = event.get("segs", [])
            text = "".join(seg.get("utf8", "") for seg in segs).strip()
            if text and text != "\n":
                start_sec = round(start_ms / 1000.0, 2)
                end_sec = round((start_ms + duration_ms) / 1000.0, 2)
                subs[start_sec] = text
                segments.append({"start": start_sec, "end": end_sec, "text": text})
    except Exception as e:
        Actor.log.warning(f"[Main] json3 parse error: {e}")
    return subs, segments


def _parse_vtt_subs(path: str) -> tuple[dict[float, str], list[dict[str, Any]]]:
    """Parse VTT subtitle format into dict and segments list."""
    subs: dict[float, str] = {}
    segments: list[dict[str, Any]] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()

        blocks = content.split("\n\n")
        for block in blocks:
            lines = block.strip().split("\n")
            for i, line in enumerate(lines):
                if "-->" in line:
                    parts = line.split("-->")
                    start_str = parts[0].strip().replace(",", ".")
                    end_str = parts[1].strip().split()[0].replace(",", ".")

                    def _to_sec(ts: str) -> float:
                        p = ts.split(":")
                        if len(p) == 3:
                            return int(p[0]) * 3600 + int(p[1]) * 60 + float(p[2])
                        elif len(p) == 2:
                            return int(p[0]) * 60 + float(p[1])
                        return 0.0

                    start_sec = round(_to_sec(start_str), 2)
                    end_sec = round(_to_sec(end_str), 2)

                    text = " ".join(lines[i + 1:]).strip()
                    text = re.sub(r"<[^>]+>", "", text)
                    if text:
                        subs[start_sec] = text
                        segments.append({"start": start_sec, "end": end_sec, "text": text})
    except Exception as e:
        Actor.log.warning(f"[Main] VTT parse error: {e}")
    return subs, segments


def get_transcript_for_range(
    subtitles: dict[float, str], start_sec: float, end_sec: float
) -> str:
    """Extract transcript text for a given time range from subtitle dict."""
    texts = []
    for t, text in sorted(subtitles.items()):
        if start_sec <= t <= end_sec:
            texts.append(text)
    return " ".join(texts).strip()


def format_transcript_for_prompt(segments: list[dict[str, Any]], max_chars: int = 45_000) -> str:
    """Format subtitle segments with HH:MM:SS timestamps for AI prompt."""
    lines = []
    for s in segments:
        h = int(s["start"] // 3600)
        m = int((s["start"] % 3600) // 60)
        sec = int(s["start"] % 60)
        time_tag = f"[{h:02d}:{m:02d}:{sec:02d}]"
        lines.append(f"{time_tag} {s['text']}")
    full_text = "\n".join(lines)
    if len(full_text) > max_chars:
        return full_text[:max_chars] + "\n...(transcript continues)"
    return full_text


# ===========================================================================
# Algorithmic transcript analyzer (fallback if Gemini is not configured)
# ===========================================================================

def detect_salient_moments_heuristically(
    segments: list[dict[str, Any]],
    max_clips: int,
    video_duration: float,
) -> list[dict[str, Any]]:
    """
    Algorithmic fallback: Detect salient moments by scoring transcript windows
    based on dialogue density, question/answer markers, and high-impact keywords.
    """
    if not segments:
        return []

    ENGAGEMENT_KEYWORDS = {
        "guarda", "attenzione", "segreto", "consiglio", "importante", "veramente",
        "incredibile", "mai", "sempre", "trucco", "errore", "migliore", "peggiore",
        "funziona", "risultato", "problema", "soluzione", "scopri", "perché",
        "secret", "important", "never", "always", "mistake", "insane", "crazy",
        "result", "solution", "hack", "trick", "watch", "look", "warning"
    }

    candidates: list[dict[str, Any]] = []
    window_duration = 25.0
    step = 10.0

    current_t = 0.0
    max_t = max(s["end"] for s in segments) if segments else video_duration

    while current_t < max_t:
        w_start = current_t
        w_end = min(max_t, w_start + window_duration)
        w_segs = [s for s in segments if s["start"] >= w_start and s["end"] <= w_end]

        if w_segs:
            text = " ".join(s["text"] for s in w_segs)
            words = text.lower().split()
            word_count = len(words)

            density_score = min(40.0, (word_count / window_duration) * 15.0)

            kw_hits = [w for w in words if w.strip(".,!?:;\"'") in ENGAGEMENT_KEYWORDS]
            kw_score = min(30.0, len(kw_hits) * 8.0)

            punc_score = 0.0
            if "?" in text:
                punc_score += 15.0
            if "!" in text:
                punc_score += 15.0
            punc_score = min(30.0, punc_score)

            total_score = min(99.0, max(50.0, 50.0 + (density_score * 0.5) + (kw_score * 0.8) + punc_score))

            reasons = []
            if kw_hits:
                reasons.append(f"keywords_{'_'.join(set(kw_hits[:3]))}")
            if "?" in text:
                reasons.append("question_hook")
            if "!" in text:
                reasons.append("emotional_emphasis")
            if not reasons:
                reasons.append("high_dialogue_pacing")

            candidates.append({
                "core_start_seconds": round(w_start, 2),
                "core_end_seconds": round(w_end, 2),
                "score": round(total_score, 1),
                "headline": f"Highlight at {seconds_to_hhmmss(w_start)}",
                "detection_reasons": reasons,
                "summary": text[:100] + "..." if len(text) > 100 else text,
            })

        current_t += step

    # Non-maximum suppression to avoid overlapping moments
    candidates.sort(key=lambda x: x["score"], reverse=True)
    selected: list[dict[str, Any]] = []

    for c in candidates:
        if len(selected) >= max_clips:
            break
        overlaps = False
        for s in selected:
            if abs(c["core_start_seconds"] - s["core_start_seconds"]) < 30.0:
                overlaps = True
                break
        if not overlaps:
            selected.append(c)

    return selected


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

        canonical_url = f"https://www.youtube.com/watch?v={video_id}"
        Actor.log.info(f"[Main] Processing video: {video_id} (max_clips={max_clips})")

        await Actor.set_status_message(f"Starting analysis of {video_id}...")

        # Configure Apify Residential Proxy if APIFY_PROXY_PASSWORD is set
        session_id = f"yt_{video_id[:12]}_{int(time.time())}"
        proxy_url = get_apify_proxy_url(session_id=session_id)
        if proxy_url:
            masked_url = re.sub(r":([^@]+)@", ":****@", proxy_url)
            Actor.log.info(f"[Main] Using Apify Residential Proxy (sticky session): {masked_url}")
            os.environ["HTTP_PROXY"] = proxy_url
            os.environ["HTTPS_PROXY"] = proxy_url
        else:
            Actor.log.info(
                "[Main] APIFY_PROXY_PASSWORD not set. Running with direct connection (no proxy)."
            )

        # ── 2. Fetch metadata ────────────────────────────────────────────
        Actor.log.info("[Main] Step 1/3: Fetching video metadata...")
        metadata = await asyncio.to_thread(fetch_video_metadata, canonical_url, proxy_url)
        video_duration = float(metadata.get("duration", 0))
        video_title = metadata.get("title", video_id)
        pref_lang = metadata.get("pref_lang")

        if video_duration <= 0:
            Actor.log.warning("[Main] Could not determine video duration, defaulting to 3600s")
            video_duration = 3600.0

        Actor.log.info(
            f"[Main] Video: '{video_title}' | Duration: {seconds_to_hhmmss(video_duration)} "
            f"| Preferred sub language: {pref_lang}"
        )

        await Actor.set_status_message(
            f"Analyzing '{video_title}' ({seconds_to_hhmmss(video_duration)})..."
        )

        # ── 3. Extract transcription ONLY (no audio download) ────────────
        Actor.log.info("[Main] Step 2/3: Extracting video transcript (no audio download)...")
        await Actor.set_status_message("Extracting video transcript...")

        subtitles_dict, subtitle_segments = await asyncio.to_thread(
            fetch_subtitles, canonical_url, video_id, proxy_url, pref_lang
        )

        if not subtitle_segments:
            Actor.log.warning("[Main] No subtitles or captions found for this video. Pushing empty dataset.")
            await Actor.push_data([])
            await Actor.set_status_message("No subtitles found for this video.")
            return

        Actor.log.info(
            f"[Main] Transcript successfully extracted: {len(subtitle_segments)} segments."
        )

        # ── 4. Detect salient moments from transcript ─────────────────────
        Actor.log.info("[Main] Step 3/3: Determining salient moments from transcript...")
        await Actor.set_status_message("Analyzing transcript for salient moments...")

        formatted_transcript = format_transcript_for_prompt(subtitle_segments)

        # Try Gemini AI first
        moments = await detect_salient_moments_from_transcript(
            video_title=video_title,
            video_duration=video_duration,
            transcript_text_with_timestamps=formatted_transcript,
            max_clips=max_clips,
        )

        # If Gemini is unavailable or failed, use heuristic analysis
        if not moments:
            Actor.log.info("[Main] Using heuristic transcript analyzer...")
            moments = detect_salient_moments_heuristically(
                subtitle_segments, max_clips, video_duration
            )

        if not moments:
            Actor.log.warning("[Main] No salient moments detected. Pushing empty dataset.")
            await Actor.push_data([])
            await Actor.set_status_message("No salient moments detected.")
            return

        Actor.log.info(
            f"[Main] Identified {len(moments)} candidate moments. "
            f"Applying +10s pre-roll and +10s post-roll context buffer..."
        )

        # ── 5. Apply +10s / -10s buffer and format output ─────────────────
        results: list[dict[str, Any]] = []

        for rank, m in enumerate(moments, start=1):
            core_start = float(m["core_start_seconds"])
            core_end = float(m["core_end_seconds"])

            # REQUISITO UTENTE: "Agiungi 10 secondi precedenti e i 10 secondi successivi all'intervallo scelto"
            start_sec = max(0.0, core_start - 10.0)
            end_sec = min(video_duration, core_end + 10.0)
            duration = round(end_sec - start_sec, 2)

            start_hhmmss = seconds_to_hhmmss(start_sec)
            end_hhmmss = seconds_to_hhmmss(end_sec)

            # Extract spoken transcript for the buffered window
            clip_transcript = get_transcript_for_range(subtitles_dict, start_sec, end_sec)

            timestamp_url = f"https://youtu.be/{video_id}?t={int(start_sec)}"

            ffmpeg_cmd = (
                f'ffmpeg -ss {start_hhmmss} -to {end_hhmmss} '
                f'-i "$(yt-dlp -g -f \'bestvideo[ext=mp4]+bestaudio[ext=m4a]'
                f'/best[ext=mp4]/best\' \'{canonical_url}\')" '
                f'-c copy clip_{rank}.mp4'
            )

            detection_reasons = list(m.get("detection_reasons", []))
            headline = m.get("headline", "")
            if headline and headline not in detection_reasons:
                detection_reasons.insert(0, headline)
            detection_reasons.append("+10s_context_buffer")

            results.append({
                "rank": rank,
                "score": round(m.get("score", 85.0), 1),
                "start_time": start_hhmmss,
                "end_time": end_hhmmss,
                "duration_seconds": duration,
                "detection_reasons": detection_reasons,
                "transcript": clip_transcript,
                "direct_timestamp_url": timestamp_url,
                "ffmpeg_command": ffmpeg_cmd,
            })

        # Push to dataset and key-value store
        await Actor.push_data(results)
        await Actor.set_value("OUTPUT", results, content_type="application/json")

        Actor.log.info(
            f"[Main] ✅ Done! Pushed {len(results)} highlight clips to dataset."
        )
        await Actor.set_status_message(
            f"Completed: {len(results)} highlights found in '{video_title}'"
        )

        for r in results:
            Actor.log.info(
                f"  #{r['rank']} [Score: {r['score']}] "
                f"{r['start_time']} → {r['end_time']} ({r['duration_seconds']}s) "
                f"Reasons: {', '.join(r['detection_reasons'][:3])}"
            )
