"""
subtitle_generator.py – Generate animated (karaoke / Hormozi style) ASS subtitles for social clips.

Produces styled Advanced SubStation Alpha (.ass) files with:
- Bold uppercase text with thick black outline for maximum readability.
- Word-by-word animated color pop (karaoke highlight in bright yellow) as each word is spoken.
- Safe-zone positioning for vertical formats (TikTok/Reels/Shorts) to avoid UI overlay obstructions.
"""

from __future__ import annotations

import os
import re
from typing import Any


def format_ass_time(seconds: float) -> str:
    """Format seconds into ASS timestamp format: H:MM:SS.cs"""
    seconds = max(0.0, float(seconds))
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    cs = int(round((seconds - int(seconds)) * 100))
    if cs >= 100:
        cs = 99
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def get_words_for_clip(
    all_words: list[dict[str, Any]],
    subtitle_segments: list[dict[str, Any]],
    clip_start_sec: float,
    clip_end_sec: float,
) -> list[dict[str, Any]]:
    """
    Extract word timings relative to clip start (0.0).
    If word-level timings are provided in all_words, filters them.
    Otherwise, interpolates words from subtitle_segments.
    """
    clip_words: list[dict[str, Any]] = []

    # 1. Use word-level timestamps if available (e.g. from faster-whisper)
    if all_words:
        for w in all_words:
            w_start = float(w.get("start", 0))
            w_end = float(w.get("end", 0))
            if w_end > clip_start_sec and w_start < clip_end_sec:
                rel_start = max(0.0, round(w_start - clip_start_sec, 2))
                rel_end = max(rel_start + 0.1, round(w_end - clip_start_sec, 2))
                w_text = str(w.get("word", "")).strip()
                if w_text:
                    clip_words.append({
                        "word": w_text,
                        "start": rel_start,
                        "end": rel_end,
                    })
        if clip_words:
            return clip_words

    # 2. Fallback: Interpolate words from subtitle segments (e.g. YouTube VTT/JSON3)
    for seg in subtitle_segments:
        s_start = float(seg.get("start", 0))
        s_end = float(seg.get("end", s_start + float(seg.get("duration", 2.0))))
        if s_end <= clip_start_sec or s_start >= clip_end_sec:
            continue

        raw_text = str(seg.get("text", "")).strip()
        words = raw_text.split()
        if not words:
            continue

        clamped_start = max(clip_start_sec, s_start)
        clamped_end = min(clip_end_sec, s_end)
        seg_duration = max(0.4, clamped_end - clamped_start)
        word_dur = seg_duration / len(words)

        for i, word in enumerate(words):
            w_start = clamped_start + (i * word_dur)
            w_end = w_start + word_dur
            rel_start = max(0.0, round(w_start - clip_start_sec, 2))
            rel_end = max(rel_start + 0.1, round(w_end - clip_start_sec, 2))
            clip_words.append({
                "word": word,
                "start": rel_start,
                "end": rel_end,
            })

    return clip_words


SUBTITLE_STYLE_PROFILES: dict[str, dict[str, Any]] = {
    "HORMOZI_YELLOW": {
        "primary": "&H00FFFFFF",
        "secondary": "&H0000FFFF",
        "outline": "&H00000000",
        "back": "&H80000000",
        "outline_w": 4,
        "shadow_w": 2,
        "highlight_tag": "&H00FFFF&",
        "normal_tag": "&HFFFFFF&",
        "active_extra": "",
        "normal_extra": "",
    },
    "MRBEAST_BOUNCE": {
        "primary": "&H00FFFFFF",
        "secondary": "&H0000FF00",
        "outline": "&H00000000",
        "back": "&H80000000",
        "outline_w": 5,
        "shadow_w": 3,
        "highlight_tag": "&H00FF00&",
        "normal_tag": "&HFFFFFF&",
        "active_extra": r"\fscx115\fscy115",
        "normal_extra": r"\fscx100\fscy100",
    },
    "CLEAN_MINIMAL": {
        "primary": "&H00F0F0F0",
        "secondary": "&H0000D0FF",
        "outline": "&H60000000",
        "back": "&H40000000",
        "outline_w": 2,
        "shadow_w": 1,
        "highlight_tag": "&H00D0FF&",
        "normal_tag": "&HF0F0F0&",
        "active_extra": "",
        "normal_extra": "",
    },
    "NEON_CYAN": {
        "primary": "&H00FFFFFF",
        "secondary": "&H00FFFF00",
        "outline": "&H00800040",
        "back": "&H80400020",
        "outline_w": 3,
        "shadow_w": 2,
        "highlight_tag": "&HFFFF00&",
        "normal_tag": "&HFFFFFF&",
        "active_extra": "",
        "normal_extra": "",
    },
}


def generate_animated_ass_file(
    words: list[dict[str, Any]],
    output_ass_path: str,
    width: int = 1080,
    height: int = 1920,
    words_per_group: int = 4,
    style_preset: str = "HORMOZI_YELLOW",
    position: str = "CENTER",
    hook_text: str | None = None,
) -> str:
    """
    Generate an Advanced SubStation Alpha (.ass) file with animated word-by-word karaoke highlighting.
    Supports style presets (Hormozi, MrBeast, Minimal, Neon), vertical positions, and opening hook headline.
    """
    if not words and not hook_text:
        os.makedirs(os.path.dirname(output_ass_path), exist_ok=True)
        with open(output_ass_path, "w", encoding="utf-8") as f:
            f.write("[Script Info]\nScriptType: v4.00+\n[Events]\n")
        return output_ass_path

    # Resolve style preset
    preset_key = (style_preset or "HORMOZI_YELLOW").strip().upper()
    style_cfg = SUBTITLE_STYLE_PROFILES.get(preset_key, SUBTITLE_STYLE_PROFILES["HORMOZI_YELLOW"])

    normal_color_ass = style_cfg["primary"]
    highlight_color_ass = style_cfg["secondary"]
    outline_color_ass = style_cfg["outline"]
    back_color_ass = style_cfg["back"]
    outline_w = style_cfg["outline_w"]
    shadow_w = style_cfg["shadow_w"]
    highlight_tag = style_cfg["highlight_tag"]
    normal_tag = style_cfg["normal_tag"]
    active_extra = style_cfg["active_extra"]
    normal_extra = style_cfg["normal_extra"]

    # Compute font size & vertical margin depending on resolution, orientation & position
    is_vertical = height > width
    norm_pos = (position or "CENTER").strip().upper()

    if is_vertical:
        font_size = int(width * 0.055)  # e.g. ~59px on 1080w, ~39px on 720w
        if norm_pos == "TOP":
            alignment = 8  # Top Center
            margin_v = int(height * 0.16)
        elif norm_pos == "BOTTOM":
            alignment = 2  # Bottom Center
            margin_v = int(height * 0.16)
        else:  # CENTER
            alignment = 2
            margin_v = int(height * 0.44)
    else:
        font_size = int(height * 0.065)
        if norm_pos == "TOP":
            alignment = 8
            margin_v = int(height * 0.14)
        elif norm_pos == "BOTTOM":
            alignment = 2
            margin_v = int(height * 0.14)
        else:  # CENTER
            alignment = 2
            margin_v = int(height * 0.38)

    margin_lr = int(width * 0.08)  # 8% left/right margins

    # Header and Styles
    ass_lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Default,Arial,{font_size},{normal_color_ass},{highlight_color_ass},{outline_color_ass},{back_color_ass},-1,0,0,0,100,100,1,0,1,{outline_w},{shadow_w},{alignment},{margin_lr},{margin_lr},{margin_v},1",
    ]

    has_hook = bool(hook_text and hook_text.strip())
    if has_hook:
        hook_font_size = int(width * 0.048) if is_vertical else int(height * 0.052)
        hook_margin_v = int(height * 0.07) if is_vertical else int(height * 0.09)
        ass_lines.append(
            f"Style: HookHeader,Arial,{hook_font_size},&H0000FFFF,&H00FFFFFF,&H00000000,&H90000000,-1,0,0,0,100,100,1,0,1,4,2,8,25,25,{hook_margin_v},1"
        )

    ass_lines.extend([
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ])

    # If hook header is present, display it prominently for the opening 4 seconds
    if has_hook:
        clean_hook = str(hook_text).strip().upper().replace("\n", " ").replace("\r", "")
        hook_start = format_ass_time(0.0)
        hook_end = format_ass_time(4.0)
        ass_lines.append(
            f"Dialogue: 1,{hook_start},{hook_end},HookHeader,,0,0,0,,{{\\b1}}{clean_hook}{{\\b0}}"
        )

    # Group words into chunks of `words_per_group`
    groups: list[list[dict[str, Any]]] = []
    current_group: list[dict[str, Any]] = []

    for w in words:
        current_group.append(w)
        clean_word = w["word"].strip()
        has_punctuation = any(clean_word.endswith(p) for p in [".", "!", "?", ","])
        if len(current_group) >= words_per_group or (len(current_group) >= 2 and has_punctuation):
            groups.append(current_group)
            current_group = []

    if current_group:
        groups.append(current_group)

    # For each group, create timed dialogue lines where the active word lights up
    for group in groups:
        group_start = group[0]["start"]
        group_end = group[-1]["end"]
        if group_end <= group_start:
            group_end = group_start + 0.5

        for idx, active_word in enumerate(group):
            w_start = active_word["start"]
            w_end = active_word["end"]
            if w_end <= w_start:
                w_end = w_start + 0.3

            # Timing for this word's highlight
            line_start = max(group_start, w_start)
            line_end = max(line_start + 0.1, min(group_end, w_end))

            parts = []
            for j, item in enumerate(group):
                word_clean = item["word"].upper()
                if j == idx:
                    # Highlight active word with preset tag and optional bounce
                    parts.append(f"{{{active_extra}\\c{highlight_tag}}}{word_clean}{{{normal_extra}\\c{normal_tag}}}")
                else:
                    parts.append(word_clean)

            line_text = " ".join(parts)
            start_str = format_ass_time(line_start)
            end_str = format_ass_time(line_end)

            ass_lines.append(
                f"Dialogue: 0,{start_str},{end_str},Default,,0,0,0,,{line_text}"
            )

    os.makedirs(os.path.dirname(output_ass_path), exist_ok=True)
    with open(output_ass_path, "w", encoding="utf-8") as f:
        f.write("\n".join(ass_lines) + "\n")

    return output_ass_path
