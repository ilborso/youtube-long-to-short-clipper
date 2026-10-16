"""
audio_analyzer.py – Audio stream extraction and multi-signal analysis.

Downloads ONLY the lowest-bitrate audio stream via yt-dlp, then performs:
  • Rolling RMS (energy / dBFS) for volume spike detection
  • Spectral centroid / pitch tracking for scream/excitement detection
  • Zero-crossing rate for distinguishing voiced/unvoiced excitement bursts
  • Lightweight event classification using spectral shape heuristics
    (laughter = periodic high-ZCR bursts, screams = sustained high-pitch energy)

All processing is streamed in fixed-size chunks so memory stays well under 1 GB
even for multi-hour VODs.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from apify import Actor


# ---------------------------------------------------------------------------
# Configuration constants
# ---------------------------------------------------------------------------
SAMPLE_RATE = 22_050          # mono, 22 kHz – enough for speech/screams
CHUNK_SECONDS = 5             # analysis window size in seconds
CHUNK_SAMPLES = SAMPLE_RATE * CHUNK_SECONDS
HOP_SECONDS = 2.5            # overlap between windows (50 %)
HOP_SAMPLES = int(SAMPLE_RATE * HOP_SECONDS)

# Spectral centroid thresholds (Hz)
SCREAM_CENTROID_HZ = 3_000   # screams/shrieks centre above this
LAUGHTER_ZCR_THRESHOLD = 0.12  # laughter has fast zero-crossings

# Minimum spike magnitude relative to moving average
SPIKE_FACTOR = 2.0           # 2× the rolling mean = spike


@dataclass
class AudioEvent:
    """A single audio event detected at a point in the stream."""
    time_sec: float
    rms_db: float
    centroid_hz: float
    zcr: float
    tags: list[str] = field(default_factory=list)
    score: float = 0.0  # 0 – 100 partial score for this window


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def _rms(samples: np.ndarray) -> float:
    """Root-mean-square energy."""
    val = np.sqrt(np.mean(samples.astype(np.float64) ** 2))
    return float(val) if val > 0 else 1e-10


def _db(rms_val: float) -> float:
    """Convert linear RMS to dBFS (full-scale = 1.0 for float audio)."""
    return 20 * np.log10(max(rms_val, 1e-10))


def _spectral_centroid(samples: np.ndarray, sr: int) -> float:
    """Weighted mean frequency of the magnitude spectrum."""
    n = len(samples)
    magnitude = np.abs(np.fft.rfft(samples))
    freqs = np.fft.rfftfreq(n, d=1.0 / sr)
    total = magnitude.sum()
    if total < 1e-10:
        return 0.0
    return float(np.sum(freqs * magnitude) / total)


def _zero_crossing_rate(samples: np.ndarray) -> float:
    """Fraction of successive samples that cross zero."""
    signs = np.sign(samples)
    crossings = np.sum(np.abs(np.diff(signs)) > 0)
    return float(crossings / max(len(samples) - 1, 1))


# ---------------------------------------------------------------------------
# Audio extraction via yt-dlp
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Helpers for proxy rotation & cookies
# ---------------------------------------------------------------------------

def _rotate_proxy_session(proxy_url: str | None, retry_num: int) -> str | None:
    """Rotate the session ID in the Apify Residential Proxy URL to get a fresh IP."""
    if not proxy_url:
        return None
    session_tag = f"session-retry{retry_num}_{int(time.time())}"
    if "session-" in proxy_url:
        return re.sub(r"session-[^,:]+", session_tag, proxy_url)
    if "groups-RESIDENTIAL" in proxy_url:
        return proxy_url.replace("groups-RESIDENTIAL", f"groups-RESIDENTIAL,{session_tag}")
    return proxy_url


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


# ---------------------------------------------------------------------------
# Audio extraction via yt-dlp
# ---------------------------------------------------------------------------

def download_audio_stream(
    video_url: str, output_dir: str, proxy_url: str | None = None
) -> str:
    """
    Download the lowest-bitrate audio-only stream as a WAV file.
    Uses yt-dlp + ffmpeg post-processing to get mono 22 kHz WAV.
    Guarantees NO video stream is downloaded to minimize proxy bandwidth usage.
    Implements retries with proxy session rotation and alternative player clients
    to bypass 429 rate limits and bot challenges.
    Returns the path to the output file.
    """
    output_path = os.path.join(output_dir, "audio.wav")

    # Client strategies for bypassing YouTube bot checks:
    # android client bypasses desktop bot wall and doesn't require web cookies
    client_strategies = [
        ["android", "mweb", "web"],
        ["mweb", "web"],
        ["ios", "web"],
    ]

    last_error = ""
    current_proxy = proxy_url

    for attempt in range(1, 4):
        # Clean up any partial output from previous attempt
        for f in os.listdir(output_dir):
            if f.endswith(".wav") or f.endswith(".part") or f.endswith(".ytdl"):
                try:
                    os.remove(os.path.join(output_dir, f))
                except OSError:
                    pass

        # If retrying, rotate proxy session to get a fresh clean residential IP
        if attempt > 1 and current_proxy:
            current_proxy = _rotate_proxy_session(proxy_url, attempt)
            Actor.log.info(
                f"[AudioAnalyzer] Attempt {attempt}/3: Retrying with rotated proxy session..."
            )
            time.sleep(2 * attempt)

        player_clients = ",".join(client_strategies[attempt - 1])

        cmd = [
            "yt-dlp",
            "--no-playlist",
            "--no-check-certificate",
            # Select worst (smallest) audio-only stream for proxy bandwidth savings (strictly vcodec=none)
            "-f", "ba[abr<=64]/worstaudio/ba/wa",
            # Post-process to mono WAV at target sample rate
            "--postprocessor-args",
            f"ffmpeg:-ac 1 -ar {SAMPLE_RATE}",
            "-x",  # extract audio only
            "--audio-format", "wav",
            "--extractor-args", f"youtube:player_client={player_clients}",
            "-o", output_path,
        ]

        # Add cookies if available
        cmd.extend(_get_cookie_args())

        # Add proxy
        if current_proxy:
            cmd.extend(["--proxy", current_proxy])

        cmd.append(video_url)

        Actor.log.info(
            f"[AudioAnalyzer] Downloading audio stream (attempt {attempt}/3, clients={player_clients})..."
        )

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=600,
        )

        if result.returncode == 0:
            # yt-dlp may append .wav again – find the actual file
            if os.path.exists(output_path):
                return output_path

            double_ext = output_path + ".wav"
            if os.path.exists(double_ext):
                os.rename(double_ext, output_path)
                return output_path

            for f in os.listdir(output_dir):
                if f.endswith(".wav"):
                    return os.path.join(output_dir, f)

        last_error = result.stderr[-1000:] if result.stderr else f"Exit code {result.returncode}"
        Actor.log.info(
            f"[AudioAnalyzer] yt-dlp attempt {attempt}/3 failed (exit {result.returncode}): {last_error[:300]}"
        )

    Actor.log.info(f"[AudioAnalyzer] All 3 download attempts failed. Stderr: {last_error}")
    raise RuntimeError(f"yt-dlp audio download failed after 3 attempts: {last_error}")


# ---------------------------------------------------------------------------
# Core analysis pipeline
# ---------------------------------------------------------------------------

def _load_wav_raw(path: str) -> np.ndarray:
    """
    Load a WAV file into a numpy array using scipy or Python's wave module.
    Returns float64 samples normalised to [-1, 1].
    """
    try:
        from scipy.io import wavfile
        sr, data = wavfile.read(path)
    except Exception:
        import wave
        with wave.open(path, "rb") as wf:
            sr = wf.getframerate()
            n_channels = wf.getnchannels()
            sampwidth = wf.getsampwidth()
            n_frames = wf.getnframes()
            frames = wf.readframes(n_frames)
            if sampwidth == 2:
                data = np.frombuffer(frames, dtype=np.int16)
            elif sampwidth == 4:
                data = np.frombuffer(frames, dtype=np.int32)
            else:
                data = np.frombuffer(frames, dtype=np.uint8).astype(np.float64) - 128
            if n_channels > 1:
                data = data.reshape(-1, n_channels)

    # Convert to float64
    if data.dtype == np.int16:
        data = data.astype(np.float64) / 32768.0
    elif data.dtype == np.int32:
        data = data.astype(np.float64) / 2147483648.0
    elif data.dtype == np.float32:
        data = data.astype(np.float64)

    # If stereo, downmix
    if data.ndim > 1:
        data = data.mean(axis=1)

    Actor.log.info(
        f"[AudioAnalyzer] Loaded WAV: {len(data)} samples, "
        f"{len(data) / SAMPLE_RATE:.1f}s, sr_file={sr}"
    )
    return data


def analyze_audio(wav_path: str) -> list[AudioEvent]:
    """
    Run the full audio analysis pipeline on a WAV file.
    Returns a list of AudioEvent objects, one per analysis window.
    """
    samples = _load_wav_raw(wav_path)
    total_samples = len(samples)
    total_seconds = total_samples / SAMPLE_RATE

    Actor.log.info(
        f"[AudioAnalyzer] Analyzing {total_seconds:.1f}s of audio "
        f"({total_samples} samples, {CHUNK_SECONDS}s windows, {HOP_SECONDS}s hop)"
    )

    events: list[AudioEvent] = []
    rms_history: list[float] = []

    pos = 0
    while pos + CHUNK_SAMPLES <= total_samples:
        chunk = samples[pos: pos + CHUNK_SAMPLES]
        time_sec = pos / SAMPLE_RATE

        rms_val = _rms(chunk)
        rms_db = _db(rms_val)
        centroid = _spectral_centroid(chunk, SAMPLE_RATE)
        zcr = _zero_crossing_rate(chunk)

        rms_history.append(rms_val)
        tags: list[str] = []

        # --- Spike detection vs rolling baseline ---
        if len(rms_history) > 10:
            baseline = np.mean(rms_history[-20:]) if len(rms_history) >= 20 else np.mean(rms_history)
            if rms_val > baseline * SPIKE_FACTOR and rms_db > -35:
                spike_pct = int((rms_val / max(baseline, 1e-10) - 1) * 100)
                tags.append(f"audio_volume_spike_+{spike_pct}%")

        # --- Scream / high-pitch detection ---
        if centroid > SCREAM_CENTROID_HZ and rms_db > -30:
            tags.append("high_pitch_scream")

        # --- Laughter detection (periodic bursts of high ZCR + moderate energy) ---
        if zcr > LAUGHTER_ZCR_THRESHOLD and rms_db > -40 and centroid < SCREAM_CENTROID_HZ:
            tags.append("laughter_spike")

        # --- General excitement (high energy + high centroid, not classified above) ---
        if rms_db > -20 and centroid > 2000 and not tags:
            tags.append("high_energy_moment")

        # --- Compute partial score ---
        # Normalise components to 0-1 range, then scale to 0-50 (audio portion)
        rms_norm = min(max((_db(rms_val) + 50) / 50, 0), 1)   # -50dB → 0, 0dB → 1
        centroid_norm = min(centroid / 5000, 1)
        zcr_norm = min(zcr / 0.2, 1)
        tag_bonus = min(len(tags) * 0.15, 0.3)

        raw_score = (
            rms_norm * 0.50
            + centroid_norm * 0.25
            + zcr_norm * 0.10
            + tag_bonus
        )
        score = round(min(raw_score * 100, 100), 2)

        event = AudioEvent(
            time_sec=round(time_sec, 2),
            rms_db=round(rms_db, 2),
            centroid_hz=round(centroid, 1),
            zcr=round(zcr, 4),
            tags=tags,
            score=score,
        )
        events.append(event)

        pos += HOP_SAMPLES

    Actor.log.info(
        f"[AudioAnalyzer] Analysis complete: {len(events)} windows, "
        f"{sum(1 for e in events if e.tags)} with detected events"
    )
    return events


# ---------------------------------------------------------------------------
# Convenience: full pipeline from URL
# ---------------------------------------------------------------------------

def extract_and_analyze(
    video_url: str,
    work_dir: str | None = None,
    proxy_url: str | None = None,
) -> list[AudioEvent]:
    """
    End-to-end: download audio → analyse → return events.
    Cleans up temporary files after analysis.
    """
    tmp_dir = work_dir or tempfile.mkdtemp(prefix="yt_audio_")

    try:
        wav_path = download_audio_stream(video_url, tmp_dir, proxy_url=proxy_url)
        events = analyze_audio(wav_path)
        return events
    finally:
        # Clean up WAV to free disk space
        for f in os.listdir(tmp_dir):
            fpath = os.path.join(tmp_dir, f)
            try:
                os.remove(fpath)
            except OSError:
                pass
        if not work_dir:
            try:
                os.rmdir(tmp_dir)
            except OSError:
                pass
