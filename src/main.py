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

from .audio_analyzer import AudioEvent, analyze_audio, SAMPLE_RATE
from .gemini_service import (
    detect_salient_moments_from_transcript,
    transcribe_and_title_clip,
    generate_social_copy_kit,
    generate_fallback_social_copy_kit,
    clean_no_icons,
)
from .subtitle_generator import (
    get_words_for_clip,
    generate_animated_ass_file,
)


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
        Actor.log.info(f"[Main] Failed to fetch metadata: {e}")
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
            Actor.log.info("[Main] No subtitles found via yt-dlp.")
            return {}, []

        Actor.log.info(f"[Main] Subtitle file extracted: {os.path.basename(sub_file)}")
        if sub_file.endswith(".json3") or sub_file.endswith(".json"):
            return _parse_json3_subs(sub_file)
        elif sub_file.endswith(".vtt"):
            return _parse_vtt_subs(sub_file)

        return {}, []
    except Exception as e:
        Actor.log.info(f"[Main] Subtitle extraction error: {e}")
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
        Actor.log.info(f"[Main] json3 parse error: {e}")
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
        Actor.log.info(f"[Main] VTT parse error: {e}")
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
    max_length: int = 60,
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
    window_duration = max(10.0, min(35.0, float(max_length) - 20.0)) if max_length >= 30 else float(max(5.0, max_length - 4.0))
    step = max(5.0, window_duration / 2.0)

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
# Video file download, audio extraction & clip cutting
# ===========================================================================

def download_video_file(file_url: str, output_dir: str) -> str:
    """
    Download a video file from an HTTP/HTTPS URL into output_dir.
    Tries chunked HTTP streaming first, then yt-dlp, and curl as final fallback.
    """
    clean_url = file_url.split("?")[0].split("#")[0]
    ext = os.path.splitext(clean_url)[1].lower()
    if not ext or len(ext) > 5 or ext not in [".mp4", ".mkv", ".mov", ".webm", ".avi", ".flv", ".ts"]:
        ext = ".mp4"

    target_path = os.path.join(output_dir, f"source_video{ext}")
    Actor.log.info(f"[Main] Downloading video file from URL: {file_url[:90]}...")

    # Strategy 1: Python urllib with chunked streaming
    try:
        import urllib.request
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
        }
        req = urllib.request.Request(file_url, headers=headers)
        with urllib.request.urlopen(req, timeout=60) as resp, open(target_path, "wb") as out_file:
            total_bytes = int(resp.headers.get("Content-Length", 0))
            downloaded = 0
            last_log = 0
            chunk_size = 1024 * 1024  # 1 MB

            while True:
                chunk = resp.read(chunk_size)
                if not chunk:
                    break
                out_file.write(chunk)
                downloaded += len(chunk)
                if downloaded - last_log >= 25 * 1024 * 1024:
                    mb = downloaded / (1024 * 1024)
                    if total_bytes > 0:
                        pct = (downloaded / total_bytes) * 100
                        Actor.log.info(f"[Main] Downloaded {mb:.1f} MB ({pct:.1f}%)...")
                    else:
                        Actor.log.info(f"[Main] Downloaded {mb:.1f} MB...")
                    last_log = downloaded

        if os.path.exists(target_path) and os.path.getsize(target_path) > 1000:
            final_mb = os.path.getsize(target_path) / (1024 * 1024)
            Actor.log.info(f"[Main] Video file downloaded successfully: {final_mb:.1f} MB.")
            return target_path
    except Exception as e:
        Actor.log.info(f"[Main] Direct HTTP streaming attempt failed ({e}), trying yt-dlp download...")

    # Strategy 2: yt-dlp
    try:
        cmd = [
            "yt-dlp",
            "--no-playlist",
            "--no-check-certificate",
            "-o", target_path,
            file_url,
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if res.returncode == 0:
            if os.path.exists(target_path):
                return target_path
            for f in os.listdir(output_dir):
                fp = os.path.join(output_dir, f)
                if os.path.isfile(fp) and os.path.getsize(fp) > 1000:
                    return fp
        Actor.log.info(f"[Main] yt-dlp download failed: {res.stderr[-300:]}")
    except Exception as e:
        Actor.log.info(f"[Main] yt-dlp download error: {e}")

    # Strategy 3: curl
    try:
        cmd = ["curl", "-fSL", "-o", target_path, file_url]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if res.returncode == 0 and os.path.exists(target_path) and os.path.getsize(target_path) > 1000:
            return target_path
    except Exception as e:
        Actor.log.info(f"[Main] curl download error: {e}")

    raise RuntimeError(f"Could not download video file from: {file_url}")


def get_local_video_info(video_path: str) -> dict[str, Any]:
    """Get metadata for local video file using ffprobe or ffmpeg."""
    try:
        cmd = [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration,size,bit_rate:stream=codec_type,width,height",
            "-of", "json",
            video_path,
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if res.returncode == 0:
            data = json.loads(res.stdout)
            fmt = data.get("format", {})
            duration = float(fmt.get("duration", 0))
            if duration > 0:
                return {
                    "duration": duration,
                    "title": os.path.splitext(os.path.basename(video_path))[0],
                    "size": int(fmt.get("size", os.path.getsize(video_path))),
                }
    except Exception as e:
        Actor.log.info(f"[Main] ffprobe metadata inspection failed: {e}")

    # Fallback to ffmpeg -i inspection
    try:
        cmd = ["ffmpeg", "-i", video_path]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", res.stderr)
        if m:
            h, m_, s = float(m.group(1)), float(m.group(2)), float(m.group(3))
            duration = h * 3600 + m_ * 60 + s
            return {
                "duration": duration,
                "title": os.path.splitext(os.path.basename(video_path))[0],
                "size": os.path.getsize(video_path),
            }
    except Exception as e:
        Actor.log.info(f"[Main] ffmpeg fallback metadata inspection failed: {e}")

    return {
        "duration": 0.0,
        "title": os.path.splitext(os.path.basename(video_path))[0],
        "size": os.path.getsize(video_path),
    }


def extract_audio_from_local_video(video_path: str, output_wav: str) -> str:
    """Extract mono 22050 Hz WAV from local video for audio analysis."""
    cmd = [
        "ffmpeg", "-y",
        "-i", video_path,
        "-vn",
        "-ac", "1",
        "-ar", str(SAMPLE_RATE),
        "-c:a", "pcm_s16le",
        output_wav,
    ]
    Actor.log.info(f"[Main] Extracting audio stream from '{os.path.basename(video_path)}'...")
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if res.returncode != 0:
        raise RuntimeError(f"ffmpeg audio extraction failed: {res.stderr[-500:]}")
    return output_wav


def extract_subtitles_from_local_video(
    video_path: str, output_vtt: str
) -> tuple[dict[float, str], list[dict[str, Any]]]:
    """Attempt to extract embedded subtitles from local video file using ffmpeg."""
    try:
        cmd = [
            "ffmpeg", "-y",
            "-i", video_path,
            "-map", "0:s:0",
            "-c:s", "webvtt",
            output_vtt,
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if res.returncode == 0 and os.path.exists(output_vtt) and os.path.getsize(output_vtt) > 10:
            with open(output_vtt, "r", encoding="utf-8", errors="ignore") as f:
                vtt_text = f.read()
            return parse_vtt(vtt_text)
    except Exception:
        pass
    return {}, []


def transcribe_audio_locally(
    audio_path: str,
) -> tuple[dict[float, str], list[dict[str, Any]], list[dict[str, Any]]]:
    """
    Transcribe audio file locally using faster-whisper on CPU.
    Returns (subtitles_dict, subtitle_segments, all_words) with word-level timestamps.
    """
    try:
        from faster_whisper import WhisperModel
        Actor.log.info("[Main] Transcribing audio locally with faster-whisper (model: tiny, CPU int8, word timestamps)...")
        model = WhisperModel("tiny", device="cpu", compute_type="int8")
        segments_gen, info = model.transcribe(audio_path, beam_size=1, word_timestamps=True)

        subtitles_dict: dict[float, str] = {}
        subtitle_segments: list[dict[str, Any]] = []
        all_words: list[dict[str, Any]] = []

        for seg in segments_gen:
            text = seg.text.strip()
            if text:
                subtitles_dict[round(seg.start, 2)] = text
                subtitle_segments.append({
                    "start": round(seg.start, 2),
                    "end": round(seg.end, 2),
                    "duration": round(seg.end - seg.start, 2),
                    "text": text,
                })
                if hasattr(seg, "words") and seg.words:
                    for w in seg.words:
                        w_text = w.word.strip()
                        if w_text:
                            all_words.append({
                                "start": round(w.start, 2),
                                "end": round(w.end, 2),
                                "word": w_text,
                            })

        Actor.log.info(
            f"[Main] Local transcription completed: {len(subtitle_segments)} segments, "
            f"{len(all_words)} word timestamps (language: {info.language}, prob: {info.language_probability:.2f})"
        )
        return subtitles_dict, subtitle_segments, all_words
    except ImportError:
        Actor.log.info(
            "[Main] faster-whisper not installed. Local speech-to-text skipped."
        )
    except Exception as e:
        Actor.log.info(f"[Main] Local audio transcription error: {e}")

    return {}, [], []


def detect_salient_moments_from_audio_events(
    events: list[AudioEvent],
    max_clips: int,
    video_duration: float,
    max_length: int = 60,
) -> list[dict[str, Any]]:
    """
    Detect salient highlight moments from audio events (volume spikes, laughter, screams, high energy).
    """
    if not events:
        return []

    core_span = max(10.0, min(30.0, float(max_length) - 20.0)) if max_length >= 30 else float(max(5.0, max_length - 4.0))

    candidates: list[dict[str, Any]] = []
    for ev in events:
        if ev.score <= 0 and not ev.tags:
            continue

        peak_t = ev.time_sec
        core_start = max(0.0, peak_t - (core_span * 0.35))
        core_end = min(video_duration, core_start + core_span)
        core_start = max(0.0, core_end - core_span)

        reasons = list(ev.tags) if ev.tags else ["high_energy_audio"]
        score = min(99.0, max(60.0, ev.score if ev.score > 10 else 75.0))
        if ev.tags:
            score = min(99.0, score + 10.0)

        tag_summary = ", ".join(reasons)
        headline = f"Audio Spike ({tag_summary}) at {seconds_to_hhmmss(peak_t)}"

        candidates.append({
            "core_start_seconds": round(core_start, 2),
            "core_end_seconds": round(core_end, 2),
            "peak_seconds": round(peak_t, 2),
            "score": round(score, 1),
            "headline": headline,
            "detection_reasons": reasons,
            "summary": f"Detected {tag_summary} with energy {ev.rms_db} dBFS at {seconds_to_hhmmss(peak_t)}.",
        })

    candidates.sort(key=lambda x: x["score"], reverse=True)

    selected: list[dict[str, Any]] = []
    min_distance = max(20.0, core_span + 10.0)

    for c in candidates:
        if len(selected) >= max_clips:
            break
        overlaps = False
        for s in selected:
            if abs(c["core_start_seconds"] - s["core_start_seconds"]) < min_distance:
                overlaps = True
                break
        if not overlaps:
            selected.append(c)

    # Fallback to energy ranking if not enough tagged events
    if len(selected) < max_clips and events:
        sorted_by_energy = sorted(events, key=lambda e: e.rms_db, reverse=True)
        for ev in sorted_by_energy:
            if len(selected) >= max_clips:
                break
            peak_t = ev.time_sec
            core_start = max(0.0, peak_t - (core_span * 0.35))
            core_end = min(video_duration, core_start + core_span)
            core_start = max(0.0, core_end - core_span)
            overlaps = any(abs(core_start - s["core_start_seconds"]) < min_distance for s in selected)
            if not overlaps:
                selected.append({
                    "core_start_seconds": round(core_start, 2),
                    "core_end_seconds": round(core_end, 2),
                    "peak_seconds": round(peak_t, 2),
                    "score": round(min(90.0, max(65.0, 50.0 + ev.rms_db + 40.0)), 1),
                    "headline": f"Audio Energy Peak at {seconds_to_hhmmss(peak_t)}",
                    "detection_reasons": ["audio_peak_energy"],
                    "summary": f"Peak audio energy of {ev.rms_db} dBFS at {seconds_to_hhmmss(peak_t)}.",
                })

    selected.sort(key=lambda x: x["core_start_seconds"])
    return selected[:max_clips]


# ===========================================================================
# Target video format profiles for social media clipping
# ===========================================================================

TARGET_FORMAT_PROFILES: dict[str, dict[str, Any]] = {
    # TikTok
    "TIKTOK_FEED_VERTICAL_9_16_HIGHRES_1080P_MP4": {
        "width": 1080, "height": 1920, "aspect": "9:16", "name": "TikTok Feed (1080x1920 FHD)"
    },
    "TIKTOK_FEED_VERTICAL_9_16_LOWRES_720P_MP4": {
        "width": 720, "height": 1280, "aspect": "9:16", "name": "TikTok Feed (720x1280 HD)"
    },
    "TIKTOK_LANDSCAPE_16_9_HIGHRES_1080P_MP4": {
        "width": 1920, "height": 1080, "aspect": "16:9", "name": "TikTok Landscape (1920x1080 FHD)"
    },
    "TIKTOK_LANDSCAPE_16_9_LOWRES_720P_MP4": {
        "width": 1280, "height": 720, "aspect": "16:9", "name": "TikTok Landscape (1280x720 HD)"
    },
    "TIKTOK_SQUARE_1_1_HIGHRES_1080P_MP4": {
        "width": 1080, "height": 1080, "aspect": "1:1", "name": "TikTok Square (1080x1080 FHD)"
    },
    "TIKTOK_SQUARE_1_1_LOWRES_720P_MP4": {
        "width": 720, "height": 720, "aspect": "1:1", "name": "TikTok Square (720x720 HD)"
    },
    # Instagram
    "INSTAGRAM_REELS_VERTICAL_9_16_HIGHRES_1080P_MP4": {
        "width": 1080, "height": 1920, "aspect": "9:16", "name": "Instagram Reels (1080x1920 FHD)"
    },
    "INSTAGRAM_REELS_VERTICAL_9_16_LOWRES_720P_MP4": {
        "width": 720, "height": 1280, "aspect": "9:16", "name": "Instagram Reels (720x1280 HD)"
    },
    "INSTAGRAM_STORIES_VERTICAL_9_16_HIGHRES_1080P_MP4": {
        "width": 1080, "height": 1920, "aspect": "9:16", "name": "Instagram Stories (1080x1920 FHD)"
    },
    "INSTAGRAM_STORIES_VERTICAL_9_16_LOWRES_720P_MP4": {
        "width": 720, "height": 1280, "aspect": "9:16", "name": "Instagram Stories (720x1280 HD)"
    },
    "INSTAGRAM_FEED_PORTRAIT_4_5_HIGHRES_1080P_MP4": {
        "width": 1080, "height": 1350, "aspect": "4:5", "name": "Instagram Feed Portrait (1080x1350 FHD)"
    },
    "INSTAGRAM_FEED_PORTRAIT_4_5_LOWRES_720P_MP4": {
        "width": 720, "height": 900, "aspect": "4:5", "name": "Instagram Feed Portrait (720x900 HD)"
    },
    "INSTAGRAM_FEED_SQUARE_1_1_HIGHRES_1080P_MP4": {
        "width": 1080, "height": 1080, "aspect": "1:1", "name": "Instagram Feed Square (1080x1080 FHD)"
    },
    "INSTAGRAM_FEED_SQUARE_1_1_LOWRES_720P_MP4": {
        "width": 720, "height": 720, "aspect": "1:1", "name": "Instagram Feed Square (720x720 HD)"
    },
    "INSTAGRAM_FEED_LANDSCAPE_16_9_HIGHRES_1080P_MP4": {
        "width": 1920, "height": 1080, "aspect": "16:9", "name": "Instagram Feed Landscape (1920x1080 FHD)"
    },
    "INSTAGRAM_FEED_LANDSCAPE_16_9_LOWRES_720P_MP4": {
        "width": 1280, "height": 720, "aspect": "16:9", "name": "Instagram Feed Landscape (1280x720 HD)"
    },
    # YouTube Shorts
    "YOUTUBE_SHORTS_VERTICAL_9_16_HIGHRES_1080P_MP4": {
        "width": 1080, "height": 1920, "aspect": "9:16", "name": "YouTube Shorts (1080x1920 FHD)"
    },
    "YOUTUBE_SHORTS_VERTICAL_9_16_LOWRES_720P_MP4": {
        "width": 720, "height": 1280, "aspect": "9:16", "name": "YouTube Shorts (720x1280 HD)"
    },
    # Original Aspect Ratio (no crop/scale)
    "ORIGINAL_RATIO_MP4": {
        "width": None, "height": None, "aspect": "original", "name": "Original Aspect Ratio"
    },
}

DEFAULT_TARGET_FORMAT = "INSTAGRAM_REELS_VERTICAL_9_16_HIGHRES_1080P_MP4"


def get_video_filter_for_format(target_format: str | None) -> str | None:
    """
    Return the ffmpeg video filter (-vf) to scale and center-crop to target format.
    Calculates scale keeping aspect ratio, performs center crop with even offsets
    for yuv420p chromatic subsampling, and forces square sample aspect ratio (SAR=1).
    """
    if not target_format:
        return None
    profile = TARGET_FORMAT_PROFILES.get(target_format)
    if not profile or not profile.get("width") or not profile.get("height"):
        return None
    w = profile["width"]
    h = profile["height"]
    return (
        f"scale={w}:{h}:force_original_aspect_ratio=increase,"
        f"crop={w}:{h}:'trunc((in_w-{w})/4)*2':'trunc((in_h-{h})/4)*2',"
        f"setsar=1"
    )


def cut_local_video_clip(
    video_path: str,
    start_time: str,
    end_time: str,
    output_path: str,
    target_format: str | None = None,
    ass_subtitle_path: str | None = None,
) -> str:
    """
    Accurately cut a video segment using ffmpeg and re-encode to the chosen social media target format.
    Optionally burns in animated ASS karaoke subtitles.
    Ensures clean cuts at keyframes, proper aspect ratio / resolution cropping, yuv420p color format,
    and faststart flags for instant web/social playback.
    """
    vf = get_video_filter_for_format(target_format)

    # Prepare subtitle filter candidates if ASS file exists
    sub_filters = []
    if ass_subtitle_path and os.path.exists(ass_subtitle_path) and os.path.getsize(ass_subtitle_path) > 30:
        clean_ass = ass_subtitle_path.replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
        sub_filters = [f"ass='{clean_ass}'", f"subtitles='{clean_ass}'"]

    candidates = []
    for sf in sub_filters:
        combined = f"{vf},{sf}" if vf else sf
        candidates.append((combined, f"format + {sf[:3]} subtitles"))
    if vf:
        candidates.append((vf, "format only (no subtitles)"))
    candidates.append((None, "direct stream copy"))

    success = False
    for filter_str, desc in candidates:
        if filter_str is not None:
            cmd = [
                "ffmpeg", "-y",
                "-ss", str(start_time),
                "-to", str(end_time),
                "-i", video_path,
                "-vf", filter_str,
                "-c:v", "libx264",
                "-preset", "veryfast",
                "-crf", "22",
                "-pix_fmt", "yuv420p",
                "-c:a", "aac",
                "-b:a", "128k",
                "-movflags", "+faststart",
                "-avoid_negative_ts", "make_zero",
                output_path,
            ]
        else:
            cmd = [
                "ffmpeg", "-y",
                "-ss", str(start_time),
                "-to", str(end_time),
                "-i", video_path,
                "-c", "copy",
                "-avoid_negative_ts", "make_zero",
                output_path,
            ]

        Actor.log.info(f"[Main] Running ffmpeg cut ({desc})...")
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if res.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 1000:
            success = True
            break
        else:
            err = res.stderr[-200:] if res.stderr else "non-zero exit code"
            Actor.log.info(f"[Main] ffmpeg cut failed ({desc}): {err}")

    if not success:
        raise RuntimeError("ffmpeg video cut failed on all candidate strategies.")

    return output_path


def extract_clip_audio_bytes(clip_path: str) -> tuple[bytes, str]:
    """Extract mono 16kHz WAV from a clip for Gemini transcription."""
    tmp_wav = clip_path + ".wav"
    try:
        cmd = [
            "ffmpeg", "-y",
            "-i", clip_path,
            "-vn",
            "-ac", "1",
            "-ar", "16000",
            "-c:a", "pcm_s16le",
            tmp_wav,
        ]
        res = subprocess.run(cmd, capture_output=True, timeout=60)
        if res.returncode == 0 and os.path.exists(tmp_wav) and os.path.getsize(tmp_wav) > 100:
            with open(tmp_wav, "rb") as f:
                return f.read(), "audio/wav"
        else:
            err = res.stderr[-200:] if res.stderr else "non-zero return code"
            Actor.log.info(f"[Main] ffmpeg wav extraction info: {err}")
    except Exception as e:
        Actor.log.info(f"[Main] Note: Could not extract audio bytes for clip: {e}")
    finally:
        if os.path.exists(tmp_wav):
            try:
                os.remove(tmp_wav)
            except OSError:
                pass

    # Fallback: pass the clip's MP4 directly as video/mp4 if under 25MB
    try:
        if os.path.exists(clip_path) and os.path.getsize(clip_path) < 25 * 1024 * 1024:
            with open(clip_path, "rb") as f:
                return f.read(), "video/mp4"
    except Exception:
        pass

    return b"", ""


# ===========================================================================
# Main Actor entrypoint
# ===========================================================================

async def main() -> None:
    """Apify Actor main entrypoint."""
    loop = asyncio.get_event_loop()
    loop.set_default_executor(ThreadPoolExecutor(max_workers=8))

    async with Actor:
        # =====================================================================
        # PHASE 1: INITIALIZATION & CONFIGURATION
        # =====================================================================
        actor_input = await Actor.get_input() or {}
        Actor.log.info("=" * 72)
        Actor.log.info("[Main] === PHASE 1: INITIALIZATION & CONFIGURATION ===")
        Actor.log.info("=" * 72)
        Actor.log.info(f"[Main] Raw input received: {json.dumps(actor_input, default=str)}")

        file_video = actor_input.get("file_video", "").strip()
        video_url = actor_input.get("video_url", "").strip()
        max_clips = int(actor_input.get("max_clips", 5))
        max_clips = max(1, min(max_clips, 50))
        max_length = int(actor_input.get("max_length", 60))
        max_length = max(10, min(max_length, 600))
        target_format = (
            actor_input.get("targetFormat")
            or actor_input.get("target_format")
            or DEFAULT_TARGET_FORMAT
        ).strip()
        if target_format not in TARGET_FORMAT_PROFILES:
            Actor.log.info(
                f"[Main] Note: Unknown targetFormat '{target_format}', defaulting to '{DEFAULT_TARGET_FORMAT}'."
            )
            target_format = DEFAULT_TARGET_FORMAT

        format_profile = TARGET_FORMAT_PROFILES[target_format]
        format_name = format_profile["name"]
        burn_subtitles = bool(actor_input.get("burn_subtitles", True))

        has_gemini = bool(os.environ.get("GEMINI_API_KEY", "").strip())
        gemini_status = "Available (Active)" if has_gemini else "Not set (Using algorithmic/audio fallbacks)"

        Actor.log.info(f"[Main] Configured Pipeline Settings:")
        Actor.log.info(f"[Main]   • Video Input Mode: {'Direct Video File URL' if file_video else 'YouTube Video URL'}")
        Actor.log.info(f"[Main]   • Input Target: {file_video or video_url}")
        Actor.log.info(f"[Main]   • Max Highlights (Clips): {max_clips}")
        Actor.log.info(f"[Main]   • Max Clip Length: {max_length} seconds")
        Actor.log.info(
            f"[Main]   • Target Video Format: {target_format} "
            f"({format_name} - {format_profile.get('width', 'auto')}x{format_profile.get('height', 'auto')}, {format_profile.get('aspect', 'original')})"
        )
        Actor.log.info(f"[Main]   • Burn Animated Subtitles: {burn_subtitles} (Karaoke ASS style)")
        Actor.log.info(f"[Main]   • Gemini AI Status: {gemini_status}")

        # Ad-hoc mutual exclusivity validation
        if file_video and video_url:
            Actor.log.info(
                "[Main] Validation error: 'file_video' and 'video_url' are mutually exclusive. Both were provided."
            )
            raise ValueError(
                'Parameters "file_video" and "video_url" are mutually exclusive. '
                'Please provide either "file_video" for a direct video file OR "video_url" for a YouTube video, but not both.'
            )

        if not file_video and not video_url:
            Actor.log.info(
                "[Main] Validation error: Neither 'file_video' nor 'video_url' was provided in the input."
            )
            raise ValueError(
                'Either "file_video" (direct video URL) or "video_url" (YouTube URL) must be provided in the input.'
            )

        Actor.log.info("[Main] Mutual exclusivity validated: exactly one video input source provided.")

        # Ensure internal Apify SDK calls and Google Gemini bypass any global proxy
        os.environ.pop("HTTP_PROXY", None)
        os.environ.pop("HTTPS_PROXY", None)
        os.environ.pop("http_proxy", None)
        os.environ.pop("https_proxy", None)
        os.environ["NO_PROXY"] = "localhost,127.0.0.1,api.apify.com,apify.com,*.apify.com,generativelanguage.googleapis.com"
        os.environ["no_proxy"] = "localhost,127.0.0.1,api.apify.com,apify.com,*.apify.com,generativelanguage.googleapis.com"

        work_dir: str | None = None
        local_video_path: str | None = None
        subtitles_dict: dict[float, str] = {}
        subtitle_segments: list[dict[str, Any]] = []
        all_words: list[dict[str, Any]] = []
        is_local_file = bool(file_video)

        try:
            # ─────────────────────────────────────────────────────────────
            # BRANCH A: Direct Video File URL (`file_video`)
            # ─────────────────────────────────────────────────────────────
            if is_local_file:
                Actor.log.info("=" * 72)
                Actor.log.info("[Main] === PHASE 2: VIDEO DOWNLOAD & METADATA INGESTION ===")
                Actor.log.info("=" * 72)
                Actor.log.info(f"[Main] Starting download of remote video: {file_video}")
                await Actor.set_status_message("Downloading video file to local disk...")
                work_dir = tempfile.mkdtemp(prefix="video_work_")

                local_video_path = await asyncio.to_thread(download_video_file, file_video, work_dir)
                Actor.log.info(f"[Main] Download complete. Inspecting video metadata for: {local_video_path}")
                info = get_local_video_info(local_video_path)
                video_duration = float(info.get("duration", 0))
                video_title = info.get("title", "Video File")
                if video_duration <= 0:
                    Actor.log.info("[Main] Note: Could not determine video duration from headers, defaulting to 3600s.")
                    video_duration = 3600.0

                video_size_mb = round(info.get("size", 0) / (1024 * 1024), 2)
                Actor.log.info(f"[Main] Video Metadata Ingested:")
                Actor.log.info(f"[Main]   • Title: '{video_title}'")
                Actor.log.info(f"[Main]   • Duration: {seconds_to_hhmmss(video_duration)} ({video_duration:.1f}s)")
                Actor.log.info(f"[Main]   • File Size: {video_size_mb} MB")

                # Step 2: Extract audio stream from local video
                Actor.log.info("=" * 72)
                Actor.log.info("[Main] === PHASE 3: AUDIO EXTRACTION ===")
                Actor.log.info("=" * 72)
                await Actor.set_status_message("Extracting audio stream from video...")
                wav_path = os.path.join(work_dir, "extracted_audio.wav")
                Actor.log.info(f"[Main] Extracting mono 16-bit PCM WAV (sample rate: {SAMPLE_RATE} Hz) to: {wav_path}")
                await asyncio.to_thread(extract_audio_from_local_video, local_video_path, wav_path)
                wav_size_mb = round(os.path.getsize(wav_path) / (1024 * 1024), 2) if os.path.exists(wav_path) else 0.0
                Actor.log.info(f"[Main] Audio extracted successfully: {wav_size_mb} MB WAV stream.")

                # Step 3: Transcription & Subtitle processing
                Actor.log.info("=" * 72)
                Actor.log.info("[Main] === PHASE 4: TRANSCRIPTION & SUBTITLE EXTRACTION ===")
                Actor.log.info("=" * 72)
                vtt_path = os.path.join(work_dir, "embedded_subs.vtt")
                Actor.log.info("[Main] Checking video container for embedded subtitle streams...")
                subtitles_dict, subtitle_segments = extract_subtitles_from_local_video(
                    local_video_path, vtt_path
                )
                if subtitle_segments:
                    Actor.log.info(f"[Main] Found and extracted {len(subtitle_segments)} embedded subtitle segments.")
                else:
                    Actor.log.info("[Main] No embedded subtitles found. Proceeding with local speech recognition (faster-whisper)...")
                    await Actor.set_status_message("Transcribing audio locally with faster-whisper...")
                    subtitles_dict, subtitle_segments, all_words = await asyncio.to_thread(
                        transcribe_audio_locally, wav_path
                    )
                    Actor.log.info(
                        f"[Main] Speech-to-text finished: {len(subtitle_segments)} segments, "
                        f"{len(all_words)} word-level timestamps extracted."
                    )

                # Step 4: Salient Moments Detection
                Actor.log.info("=" * 72)
                Actor.log.info("[Main] === PHASE 5: SALIENT MOMENT DETECTION ===")
                Actor.log.info("=" * 72)
                moments: list[dict[str, Any]] = []
                if subtitle_segments and has_gemini:
                    await Actor.set_status_message("Identifying salient moments using Gemini AI...")
                    formatted_transcript = format_transcript_for_prompt(subtitle_segments)
                    Actor.log.info(
                        f"[Main] Sending transcript ({len(subtitle_segments)} segments, {len(formatted_transcript)} chars) "
                        f"to Gemini AI to detect top {max_clips} highlights..."
                    )
                    moments = await detect_salient_moments_from_transcript(
                        video_title=video_title,
                        video_duration=video_duration,
                        transcript_text_with_timestamps=formatted_transcript,
                        max_clips=max_clips,
                        max_length=max_length,
                    )
                    Actor.log.info(f"[Main] Gemini AI returned {len(moments)} candidate highlight moments.")

                # Fallback to audio event spikes if Gemini is not configured or returned no moments
                if not moments:
                    Actor.log.info("[Main] Running audio energy analysis fallback (volume spikes, laughter, excitement)...")
                    await Actor.set_status_message("Analyzing audio for energy spikes and salient moments...")
                    audio_events = await asyncio.to_thread(analyze_audio, wav_path)
                    Actor.log.info(f"[Main] Audio analysis detected {len(audio_events)} high-energy events.")
                    moments = detect_salient_moments_from_audio_events(
                        audio_events, max_clips, video_duration, max_length=max_length
                    )
                    Actor.log.info(f"[Main] Audio energy analysis produced {len(moments)} candidate moments.")
                    if not moments and subtitle_segments:
                        Actor.log.info("[Main] Running heuristic transcript keyword analysis fallback...")
                        moments = detect_salient_moments_heuristically(
                            subtitle_segments, max_clips, video_duration, max_length=max_length
                        )
                        Actor.log.info(f"[Main] Heuristic analysis produced {len(moments)} candidate moments.")

            # ─────────────────────────────────────────────────────────────
            # BRANCH B: YouTube URL (`video_url`)
            # ─────────────────────────────────────────────────────────────
            else:
                Actor.log.info("=" * 72)
                Actor.log.info("[Main] === PHASE 2: YOUTUBE METADATA INGESTION ===")
                Actor.log.info("=" * 72)
                video_id = extract_video_id(video_url)
                if not video_id:
                    raise ValueError(f'Could not extract video ID from URL: {video_url}')

                canonical_url = f"https://www.youtube.com/watch?v={video_id}"
                Actor.log.info(f"[Main] Extracted YouTube Video ID: {video_id}")
                Actor.log.info(f"[Main] Canonical Video URL: {canonical_url}")
                await Actor.set_status_message(f"Starting analysis of YouTube video {video_id}...")

                # Configure Apify Residential Proxy if APIFY_PROXY_PASSWORD is set
                session_id = f"yt_{video_id[:12]}_{int(time.time())}"
                proxy_url = get_apify_proxy_url(session_id=session_id)
                if proxy_url:
                    masked_url = re.sub(r":([^@]+)@", ":****@", proxy_url)
                    Actor.log.info(f"[Main] Using Apify Residential Proxy (sticky session): {masked_url}")
                else:
                    Actor.log.info(
                        "[Main] APIFY_PROXY_PASSWORD not set. Running with direct connection (no proxy)."
                    )

                # Fetch metadata
                Actor.log.info("[Main] Querying YouTube metadata via yt-dlp...")
                metadata = await asyncio.to_thread(fetch_video_metadata, canonical_url, proxy_url)
                video_duration = float(metadata.get("duration", 0))
                video_title = metadata.get("title", video_id)
                pref_lang = metadata.get("pref_lang")

                if video_duration <= 0:
                    Actor.log.info("[Main] Note: Could not determine video duration, defaulting to 3600s.")
                    video_duration = 3600.0

                Actor.log.info(f"[Main] YouTube Video Metadata Ingested:")
                Actor.log.info(f"[Main]   • Title: '{video_title}'")
                Actor.log.info(f"[Main]   • Duration: {seconds_to_hhmmss(video_duration)} ({video_duration:.1f}s)")
                Actor.log.info(f"[Main]   • Channel/Uploader: '{metadata.get('channel', 'Unknown')}'")
                Actor.log.info(f"[Main]   • Preferred Subtitle Language: {pref_lang or 'auto'}")

                # Extract transcription ONLY (no audio download)
                Actor.log.info("=" * 72)
                Actor.log.info("[Main] === PHASE 3 & 4: YOUTUBE TRANSCRIPT EXTRACTION ===")
                Actor.log.info("=" * 72)
                Actor.log.info(f"[Main] Extracting video transcript/subtitles (no heavy video/audio download required)...")
                await Actor.set_status_message("Extracting video transcript...")

                subtitles_dict, subtitle_segments = await asyncio.to_thread(
                    fetch_subtitles, canonical_url, video_id, proxy_url, pref_lang
                )

                if not subtitle_segments:
                    Actor.log.info("[Main] Note: No subtitles or captions found for this video. Pushing empty dataset.")
                    await Actor.push_data([])
                    await Actor.set_status_message("No subtitles found for this video.")
                    return

                Actor.log.info(
                    f"[Main] Transcript successfully extracted: {len(subtitle_segments)} dialogue segments."
                )

                # Detect salient moments from transcript
                Actor.log.info("=" * 72)
                Actor.log.info("[Main] === PHASE 5: SALIENT MOMENT DETECTION ===")
                Actor.log.info("=" * 72)
                await Actor.set_status_message("Analyzing transcript for salient moments...")

                formatted_transcript = format_transcript_for_prompt(subtitle_segments)
                Actor.log.info(
                    f"[Main] Analyzing transcript ({len(formatted_transcript)} characters) "
                    f"to detect top {max_clips} moments (max_length={max_length}s)..."
                )

                moments = await detect_salient_moments_from_transcript(
                    video_title=video_title,
                    video_duration=video_duration,
                    transcript_text_with_timestamps=formatted_transcript,
                    max_clips=max_clips,
                    max_length=max_length,
                )

                if not moments:
                    Actor.log.info("[Main] Gemini returned no moments or is unconfigured. Running heuristic transcript analyzer...")
                    moments = detect_salient_moments_heuristically(
                        subtitle_segments, max_clips, video_duration, max_length=max_length
                    )
                Actor.log.info(f"[Main] Phase 5 complete: {len(moments)} candidate moments detected.")

            # ─────────────────────────────────────────────────────────────
            # Common Output Pipeline
            # ─────────────────────────────────────────────────────────────
            if not moments:
                Actor.log.info("[Main] Note: No salient moments detected. Pushing empty dataset.")
                await Actor.push_data([])
                await Actor.set_status_message("No salient moments detected.")
                return

            Actor.log.info("=" * 72)
            Actor.log.info(f"[Main] === PHASE 6: CLIP PROCESSING & ENCODING ({len(moments)} moments) ===")
            Actor.log.info("=" * 72)
            Actor.log.info(
                f"[Main] Applying context buffer (+10s padding, max clip length: {max_length}s) "
                f"and preparing clips in format '{target_format}'..."
            )

            results: list[dict[str, Any]] = []
            kvs = await Actor.open_key_value_store()

            for rank, m in enumerate(moments, start=1):
                core_start = float(m["core_start_seconds"])
                core_end = float(m["core_end_seconds"])
                if core_end <= core_start:
                    core_end = core_start + 1.0

                if (core_end - core_start) > max_length:
                    core_end = core_start + max_length

                # Context buffer: 10 seconds before and 10 seconds after
                raw_start = max(0.0, core_start - 10.0)
                raw_end = min(video_duration, core_end + 10.0)
                total_duration = raw_end - raw_start

                # Enforce max_length constraint
                if total_duration > max_length:
                    excess = total_duration - max_length
                    pre_trim = min(core_start - raw_start, excess / 2.0)
                    post_trim = min(raw_end - core_end, excess - pre_trim)
                    remaining = excess - (pre_trim + post_trim)
                    if remaining > 0:
                        pre_trim += min(core_start - (raw_start + pre_trim), remaining)
                        remaining = excess - (pre_trim + post_trim)
                    if remaining > 0:
                        post_trim += remaining

                    start_sec = raw_start + pre_trim
                    end_sec = raw_end - post_trim
                else:
                    start_sec = raw_start
                    end_sec = raw_end

                start_sec = max(0.0, start_sec)
                end_sec = min(video_duration, max(start_sec + 1.0, end_sec))
                if (end_sec - start_sec) > max_length:
                    end_sec = start_sec + max_length

                duration = round(end_sec - start_sec, 2)
                start_hhmmss = seconds_to_hhmmss(start_sec)
                end_hhmmss = seconds_to_hhmmss(end_sec)

                clip_transcript = get_transcript_for_range(subtitles_dict, start_sec, end_sec)

                detection_reasons = list(m.get("detection_reasons", []))
                headline = m.get("headline", "")
                if headline and headline not in detection_reasons:
                    detection_reasons.insert(0, headline)
                detection_reasons.append("+10s_context_buffer")

                clip_title = headline or f"Highlight #{rank}"
                score_val = round(float(m.get("score", 85.0)), 1)

                Actor.log.info("-" * 60)
                Actor.log.info(f"[Main] >>> Processing Clip #{rank}/{len(moments)}: '{clip_title}' [Score: {score_val}/100]")
                Actor.log.info(
                    f"[Main]   1. Timing & Context: Core [{seconds_to_hhmmss(core_start)} → {seconds_to_hhmmss(core_end)}] "
                    f"→ Expanded [{start_hhmmss} → {end_hhmmss}] ({duration}s)"
                )

                # Generate or enrich Social Media Copy Kit via Gemini
                copy_kit = await generate_social_copy_kit(
                    title=clip_title,
                    transcript=clip_transcript,
                    score=float(m.get("score", 85.0)),
                    detection_reasons=detection_reasons,
                )

                viral_hook = clean_no_icons(m.get("viral_hook") or copy_kit.get("viral_hook", ""))
                caption = clean_no_icons(m.get("caption") or copy_kit.get("Caption", ""))
                hashtags = m.get("hashtags") or copy_kit.get("Hashtag", [])
                virality_score_desc = clean_no_icons(m.get("virality_score_description") or copy_kit.get("Virality_score_description", ""))

                Actor.log.info(f"[Main]   2. Social Media Copy Kit (No Emojis/Icons):")
                Actor.log.info(f"[Main]      • Viral Hook: \"{viral_hook}\"")
                Actor.log.info(f"[Main]      • Caption: \"{caption[:80]}...\"")
                Actor.log.info(f"[Main]      • Hashtags: {' '.join(hashtags)}")
                Actor.log.info(f"[Main]      • Virality Rationale: \"{virality_score_desc}\"")

                item: dict[str, Any] = {
                    "rank": rank,
                    "score": score_val,
                    "start_time": start_hhmmss,
                    "end_time": end_hhmmss,
                    "duration_seconds": duration,
                    "target_format": target_format,
                    "viral_hook": viral_hook,
                    "Caption": caption,
                    "Hashtag": hashtags,
                    "Virality_score_description": virality_score_desc,
                    "detection_reasons": detection_reasons,
                }

                vf = get_video_filter_for_format(target_format)

                ass_path: str | None = None
                if is_local_file and local_video_path and work_dir and burn_subtitles:
                    clip_words = get_words_for_clip(all_words, subtitle_segments, start_sec, end_sec)
                    if clip_words:
                        ass_filename = f"clip_{rank}.ass"
                        ass_path = os.path.join(work_dir, ass_filename)
                        target_info = TARGET_FORMAT_PROFILES.get(target_format, {})
                        res_w = target_info.get("width") or 1080
                        res_h = target_info.get("height") or 1920
                        generate_animated_ass_file(clip_words, ass_path, width=res_w, height=res_h)
                        Actor.log.info(
                            f"[Main]   3. Animated Subtitles: Created '{ass_filename}' with {len(clip_words)} timed words (karaoke style)."
                        )

                if is_local_file and local_video_path and work_dir:
                    # Cut clip from local video using ffmpeg
                    clip_filename = f"clip_{rank}.mp4"
                    clip_path = os.path.join(work_dir, clip_filename)
                    sub_log = "with animated karaoke subtitles" if ass_path else "without subtitles"
                    Actor.log.info(
                        f"[Main]   4. Video Encoding: Cutting {start_hhmmss} → {end_hhmmss} ({duration}s) | "
                        f"format={target_format} | {sub_log}..."
                    )
                    await asyncio.to_thread(
                        cut_local_video_clip,
                        local_video_path,
                        start_hhmmss,
                        end_hhmmss,
                        clip_path,
                        target_format,
                        ass_path,
                    )

                    clip_size_mb = round(os.path.getsize(clip_path) / (1024 * 1024), 2)
                    Actor.log.info(f"[Main]      • Encoded video file: {clip_filename} ({clip_size_mb} MB)")

                    # Upload trimmed clip to Key-Value Store for instant download
                    with open(clip_path, "rb") as cf:
                        clip_bytes = cf.read()
                    await kvs.set_value(clip_filename, clip_bytes, content_type="video/mp4")
                    download_url = await kvs.get_public_url(clip_filename)
                    Actor.log.info(f"[Main]   5. Storage: Uploaded clip to Key-Value Store: {download_url}")

                    # Optionally store ASS subtitle file in Key-Value Store
                    if ass_path and os.path.exists(ass_path):
                        with open(ass_path, "rb") as af:
                            await kvs.set_value(f"clip_{rank}.ass", af.read(), content_type="text/plain; charset=utf-8")
                        sub_url = await kvs.get_public_url(f"clip_{rank}.ass")
                        item["subtitles_url"] = sub_url
                        Actor.log.info(f"[Main]      • Subtitle ASS file uploaded: {sub_url}")

                    clean_title = clean_no_icons(clip_title)
                    item["Title"] = clean_title
                    item["title"] = clean_title
                    item["transcript"] = clip_transcript
                    item["clip_filename"] = clip_filename
                    item["clip_size_mb"] = clip_size_mb
                    item["download_url"] = download_url
                    item["download_link"] = None
                    item["Download Link"] = None
                    sub_filter_str = f",ass='clip_{rank}.ass'" if (burn_subtitles and ass_path) else ""
                    if vf:
                        item["ffmpeg_command"] = (
                            f'ffmpeg -ss {start_hhmmss} -to {end_hhmmss} -i "{file_video}" '
                            f'-vf "{vf}{sub_filter_str}" -c:v libx264 -pix_fmt yuv420p -preset fast -crf 22 '
                            f'-c:a aac -b:a 128k -movflags +faststart {clip_filename}'
                        )
                    else:
                        item["ffmpeg_command"] = (
                            f'ffmpeg -ss {start_hhmmss} -to {end_hhmmss} -i "{file_video}" -c copy {clip_filename}'
                        )
                else:
                    clean_title = clean_no_icons(clip_title)
                    item["Title"] = clean_title
                    item["title"] = clean_title
                    item["transcript"] = clip_transcript
                    timestamp_url = f"https://youtu.be/{video_id}?t={int(start_sec)}"
                    item["download_url"] = None
                    item["download_link"] = timestamp_url
                    item["Download Link"] = timestamp_url
                    item["direct_timestamp_url"] = timestamp_url
                    sub_filter_str = f",ass='clip_{rank}.ass'" if burn_subtitles else ""
                    if vf:
                        ffmpeg_cmd = (
                            f'ffmpeg -ss {start_hhmmss} -to {end_hhmmss} '
                            f'-i "$(yt-dlp -g -f \'bestvideo[ext=mp4]+bestaudio[ext=m4a]'
                            f'/best[ext=mp4]/best\' \'{canonical_url}\')" '
                            f'-vf "{vf}{sub_filter_str}" -c:v libx264 -pix_fmt yuv420p -preset fast -crf 22 '
                            f'-c:a aac -b:a 128k -movflags +faststart clip_{rank}.mp4'
                        )
                    else:
                        ffmpeg_cmd = (
                            f'ffmpeg -ss {start_hhmmss} -to {end_hhmmss} '
                            f'-i "$(yt-dlp -g -f \'bestvideo[ext=mp4]+bestaudio[ext=m4a]'
                            f'/best[ext=mp4]/best\' \'{canonical_url}\')" '
                            f'-c copy clip_{rank}.mp4'
                        )
                    item["ffmpeg_command"] = ffmpeg_cmd
                    Actor.log.info(f"[Main]   3. Clip Reference: YouTube link: {timestamp_url}")

                results.append(item)

            # Push results to dataset and Key-Value Store
            Actor.log.info("=" * 72)
            Actor.log.info(f"[Main] === PHASE 7: DATASET STORAGE & EXECUTION SUMMARY ===")
            Actor.log.info("=" * 72)
            Actor.log.info(f"[Main] Pushing {len(results)} clips to Apify default dataset...")
            try:
                await Actor.push_data(results)
            except Exception as push_err:
                Actor.log.info(f"[Main] Error pushing data to dataset: {push_err}")
                if hasattr(push_err, "data") and push_err.data:
                    Actor.log.info(f"[Main] Detailed validation error data: {push_err.data}")
                raise
            Actor.log.info(f"[Main] Writing 'OUTPUT' record to Key-Value Store...")
            await Actor.set_value("OUTPUT", results, content_type="application/json")

            Actor.log.info(
                f"[Main] ✅ Pipeline complete! Successfully stored {len(results)} clips."
            )
            await Actor.set_status_message(
                f"Completed: {len(results)} highlights generated for '{video_title}'"
            )

            Actor.log.info("[Main] Generated Clips Overview:")
            for r in results:
                dl_target = r.get("download_link") or r.get("download_url")
                dl_info = f" | Download: {dl_target}" if dl_target else ""
                ts_info = f" | Link: {r.get('direct_timestamp_url')}" if r.get("direct_timestamp_url") else ""
                hook_info = f" | Hook: \"{r.get('viral_hook')}\"" if r.get("viral_hook") else ""
                title_info = f" \"{r.get('Title')}\"" if r.get("Title") else ""
                Actor.log.info(
                    f"[Main]   #{r['rank']}{title_info} [Score: {r['score']}/100] "
                    f"[{r['start_time']} → {r['end_time']} ({r['duration_seconds']}s)] "
                    f"Format: {r['target_format']}{dl_info}{ts_info}{hook_info}"
                )

            Actor.log.info("=" * 72)
            Actor.log.info("[Main] === PIPELINE EXECUTION COMPLETED SUCCESSFULLY ===")
            Actor.log.info("=" * 72)

        finally:
            # Clean up temporary work directory if created
            if work_dir and os.path.exists(work_dir):
                for f in os.listdir(work_dir):
                    try:
                        os.remove(os.path.join(work_dir, f))
                    except OSError:
                        pass
                try:
                    os.rmdir(work_dir)
                except OSError:
                    pass
