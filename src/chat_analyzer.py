"""
chat_analyzer.py – Synchronized live-chat extraction and spike analysis.

Uses `chat_downloader` to pull the live-chat replay (if available) from a
YouTube video or VOD, then:
  • Bins messages into 5-second sliding windows
  • Computes per-window message velocity
  • Detects velocity spikes exceeding a configurable factor of the moving average
  • Scans for emote / keyword patterns (LMAO, LOL, OMEGALUL, !clip, etc.)
  • Produces a list of ChatEvent objects aligned to the video timeline
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from apify import Actor


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BIN_SECONDS = 5             # sliding window width
HOP_SECONDS = 2.5           # hop between windows (50 % overlap)
MOVING_AVG_WINDOW = 24      # number of bins for baseline moving average (≈ 60 s)
SPIKE_FACTOR = 2.5           # velocity must exceed 2.5× baseline to be a spike

# Emote & keyword patterns (case-insensitive)
KEYWORD_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("emote_lmao",       re.compile(r"\b(lmao|lmfao|rofl)\b", re.IGNORECASE)),
    ("emote_lol",        re.compile(r"\b(lol|lul|kekw)\b", re.IGNORECASE)),
    ("emote_omegalul",   re.compile(r"\b(omegalul|pepega|monka[sw]?)\b", re.IGNORECASE)),
    ("emote_hype",       re.compile(r"\b(pog|pogchamp|poggers|pogu|lessgo|let'?\s*go)\b", re.IGNORECASE)),
    ("reaction_wtf",     re.compile(r"\b(wtf|wth|bruh)\b", re.IGNORECASE)),
    ("reaction_win",     re.compile(r"\b(gg|ez|w{1,3}|dub)\b", re.IGNORECASE)),
    ("clip_request",     re.compile(r"!clip\b", re.IGNORECASE)),
    ("reaction_scream",  re.compile(r"(a{3,}h{0,}|o{3,}h{0,}|AAAA)", re.IGNORECASE)),
    ("reaction_laugh",   re.compile(r"(ha){3,}|😂|🤣|💀", re.IGNORECASE)),
]


@dataclass
class ChatEvent:
    """A single chat-analysis window."""
    time_sec: float
    message_count: int
    velocity_pct: float       # velocity as % of baseline (100 = baseline)
    keyword_hits: list[str] = field(default_factory=list)
    score: float = 0.0        # 0 – 100 partial score


# ---------------------------------------------------------------------------
# Chat download
# ---------------------------------------------------------------------------

def fetch_chat_messages(video_url: str, proxy_url: str | None = None) -> list[dict[str, Any]]:
    """
    Download live-chat replay messages using chat_downloader.
    Returns a list of dicts with at minimum 'time_in_seconds' and 'message'.
    Returns an empty list if no chat is available.
    """
    try:
        from chat_downloader import ChatDownloader

        Actor.log.info("[ChatAnalyzer] Fetching live chat replay...")
        kwargs: dict[str, Any] = {}
        if proxy_url:
            kwargs["proxy"] = proxy_url
        downloader = ChatDownloader(**kwargs)
        chat = downloader.get_chat(video_url, message_types=["text_message"])

        messages: list[dict[str, Any]] = []
        count = 0
        for msg in chat:
            t = msg.get("time_in_seconds")
            text = msg.get("message", "")
            if t is not None and text:
                messages.append({"time": float(t), "text": str(text)})
                count += 1
                # Log progress every 5000 messages
                if count % 5000 == 0:
                    Actor.log.info(f"[ChatAnalyzer] Downloaded {count} chat messages so far...")

        Actor.log.info(f"[ChatAnalyzer] Total chat messages downloaded: {len(messages)}")
        return messages

    except Exception as e:
        Actor.log.warning(f"[ChatAnalyzer] Could not fetch chat (may not be a live VOD): {e}")
        return []


# ---------------------------------------------------------------------------
# Analysis pipeline
# ---------------------------------------------------------------------------

def _scan_keywords(texts: list[str]) -> list[str]:
    """Return de-duplicated list of keyword tags found in a batch of messages."""
    hits: set[str] = set()
    combined = " ".join(texts)
    for tag, pattern in KEYWORD_PATTERNS:
        if pattern.search(combined):
            hits.add(tag)
    return sorted(hits)


def analyze_chat(messages: list[dict[str, Any]], video_duration_sec: float | None = None) -> list[ChatEvent]:
    """
    Bin chat messages into sliding windows and detect velocity spikes + keywords.
    Returns a chronologically ordered list of ChatEvent objects.
    """
    if not messages:
        Actor.log.info("[ChatAnalyzer] No chat messages to analyze.")
        return []

    # Sort by time
    messages = sorted(messages, key=lambda m: m["time"])

    # Determine video span from chat timestamps
    min_t = messages[0]["time"]
    max_t = messages[-1]["time"]
    if video_duration_sec:
        max_t = max(max_t, video_duration_sec)

    Actor.log.info(
        f"[ChatAnalyzer] Analyzing {len(messages)} messages "
        f"spanning {min_t:.0f}s – {max_t:.0f}s"
    )

    # Build time bins
    events: list[ChatEvent] = []
    velocities: list[int] = []  # message counts per window (for moving avg)

    t = max(0.0, min_t)
    msg_idx = 0  # pointer into sorted messages

    while t + BIN_SECONDS <= max_t + BIN_SECONDS:
        window_end = t + BIN_SECONDS

        # Collect messages in this window
        window_texts: list[str] = []
        scan_idx = msg_idx
        while scan_idx < len(messages) and messages[scan_idx]["time"] < window_end:
            if messages[scan_idx]["time"] >= t:
                window_texts.append(messages[scan_idx]["text"])
            scan_idx += 1

        msg_count = len(window_texts)
        velocities.append(msg_count)

        # Compute baseline
        if len(velocities) > MOVING_AVG_WINDOW:
            baseline = float(np.mean(velocities[-MOVING_AVG_WINDOW:]))
        else:
            baseline = float(np.mean(velocities)) if velocities else 1.0
        baseline = max(baseline, 0.5)  # avoid div-by-zero

        velocity_pct = round((msg_count / baseline) * 100, 1)

        # Keywords
        keyword_hits = _scan_keywords(window_texts) if window_texts else []

        # Score: combine velocity spike + keyword presence
        vel_norm = min(max((velocity_pct - 100) / 300, 0), 1)  # 100 %→0, 400 %→1
        kw_bonus = min(len(keyword_hits) * 0.12, 0.4)
        count_bonus = min(msg_count / 50, 0.3)  # raw count bonus

        raw_score = vel_norm * 0.55 + kw_bonus + count_bonus
        score = round(min(raw_score * 100, 100), 2)

        # Build tags for significant events
        tags = list(keyword_hits)
        if velocity_pct > SPIKE_FACTOR * 100:
            tags.insert(0, f"chat_velocity_{int(velocity_pct)}%")

        event = ChatEvent(
            time_sec=round(t, 2),
            message_count=msg_count,
            velocity_pct=velocity_pct,
            keyword_hits=tags,
            score=score,
        )
        events.append(event)

        # Advance pointer past messages before next window start
        next_t = t + HOP_SECONDS
        while msg_idx < len(messages) and messages[msg_idx]["time"] < next_t:
            msg_idx += 1
        t = next_t

    spikes = sum(1 for e in events if e.velocity_pct > SPIKE_FACTOR * 100)
    Actor.log.info(
        f"[ChatAnalyzer] Analysis complete: {len(events)} windows, "
        f"{spikes} velocity spikes detected"
    )
    return events


# ---------------------------------------------------------------------------
# Convenience: full pipeline from URL
# ---------------------------------------------------------------------------

def extract_and_analyze_chat(
    video_url: str,
    video_duration_sec: float | None = None,
    proxy_url: str | None = None,
) -> list[ChatEvent]:
    """End-to-end: fetch chat → analyse → return events."""
    messages = fetch_chat_messages(video_url, proxy_url=proxy_url)
    return analyze_chat(messages, video_duration_sec)
