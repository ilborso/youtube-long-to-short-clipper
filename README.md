# 🎬 YouTube Highlight Clipper – Apify Actor

> **Automatically detect the most engaging moments in long YouTube videos** (VODs, podcasts, live recordings) and generate clip-ready timestamps optimized for YouTube Shorts.

## What This Actor Does

This Actor analyzes a YouTube video using **multi-signal scoring** to find highlight moments without downloading or re-encoding the full video file:

| Signal | Method | Weight |
|--------|--------|--------|
| 🔊 **Audio Peaks** | Rolling RMS energy, spectral centroid, zero-crossing rate | 60% |
| 💬 **Chat Velocity** | 5-second sliding window message binning, spike detection vs moving average | 40% |
| 🏷️ **Keyword Triggers** | Emote/keyword pattern matching (LMAO, LOL, !clip, OMEGALUL, etc.) | Bonus |
| 🤖 **Gemini Enrichment** | Refine clip boundaries and polish transcripts (optional) | Post-process |

### Why It's Resource-Efficient

- **No video download**: Only the lowest-bitrate audio stream is fetched (~2-5 MB/hour vs ~500 MB/hour for video)
- **Chunked processing**: Audio is analyzed in 5-second windows with 50% overlap — memory stays under 1 GB even for 6+ hour streams
- **Parallel extraction**: Audio, chat, and subtitles are fetched concurrently
- **No heavy ML models**: Uses pure NumPy/SciPy spectral analysis instead of large neural networks

---

## Input Parameters

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `file_video` | String | No | — | Direct URL to a video file (.mp4, .mkv, .mov, etc.) to download and analyze locally. When specified, extracts audio, extracts transcript locally (embedded subtitles or faster-whisper), detects salient moments using Gemini AI, cuts the video into clips, and provides downloadable trimmed files. |
| `video_url` | String | No* | — | Any valid YouTube video URL (`youtube.com/watch?v=...`, `youtu.be/...`). Used when `file_video` is omitted. (*Either `file_video` or `video_url` is required) |
| `max_clips` | Integer | No | `5` | Number of top highlight segments to return (1–50) |
| `max_length` | Integer | No | `60` | Maximum duration in seconds of each selected highlight clip (10–300) |
| `targetFormat` | String | No | `INSTAGRAM_REELS_VERTICAL_9_16_HIGHRES_1080P_MP4` | Target video format, aspect ratio, and resolution for social media clips (MP4). Supports High-Res (1080p) and Low-Res (720p) for TikTok, Instagram Reels/Stories/Feed, YouTube Shorts, or `ORIGINAL_RATIO_MP4`. |
| `burn_subtitles` | Boolean | No | `true` | Burn dynamic animated subtitles (karaoke/Hormozi style with active word highlight) directly into video clips. Maximizes viewer retention on TikTok, Reels, and Shorts. |

### Example Input (Direct Video File with Format & Animated Subtitles)

```json
{
    "file_video": "https://example.com/videos/podcast.mp4",
    "max_clips": 3,
    "max_length": 60,
    "targetFormat": "INSTAGRAM_REELS_VERTICAL_9_16_HIGHRES_1080P_MP4",
    "burn_subtitles": true
}
```

### Example Input (YouTube URL)

```json
{
    "video_url": "https://www.youtube.com/watch?v=jfKfPfyJRdk",
    "max_clips": 5,
    "max_length": 60,
    "targetFormat": "TIKTOK_FEED_VERTICAL_9_16_HIGHRES_1080P_MP4",
    "burn_subtitles": true
}
```

---

## Output Dataset Structure

Each detected highlight is pushed to the Apify dataset with these fields:

| Field | Type | Description |
|-------|------|-------------|
| `rank` | Integer | Rank (1 = highest score) |
| `Title` | String | Catchy title generated for the clip |
| `score` | Float | Composite engagement score (0–100) |
| `viral_hook` | String | First 3-second attention-grabbing on-screen hook with emoji |
| `Caption` | String | Engaging social media description with emojis, storytelling, and call to action |
| `Hashtag` | Array | 5–8 trending and niche-specific hashtags for social posting |
| `Virality_score_description` | String | Explanation of virality potential, retention hooks, and audience triggers |
| `start_time` | String | Clip start in `HH:MM:SS` format |
| `end_time` | String | Clip end in `HH:MM:SS` format |
| `duration_seconds` | Number | Clip duration in seconds (20–60s) |
| `target_format` | String | Selected social media format and resolution profile |
| `detection_reasons` | Array | Trigger explanations (e.g., `["laughter_spike", "chat_velocity_320%"]`) |
| `transcript` | String | Spoken text / dialogue transcribed from the highlight segment |
| `direct_timestamp_url` | String | Direct YouTube timestamp link (`https://youtu.be/...#t=<seconds>`) — returned when `video_url` is used |
| `download_link` | String | Direct download link for the cut video clip from Key-Value Store — returned when `file_video` is used |
| `download_url` | String | Alias for `download_link` (Key-Value Store MP4 URL) |
| `clip_filename` | String | Cut video filename (e.g., `clip_1.mp4`) |
| `subtitles_url` | String | Direct download link for the animated ASS karaoke subtitle file |
| `ffmpeg_command` | String | Ready-to-use command to extract and format this clip using FFmpeg (returned for both modes) |

### Example Output Record

```json
{
    "rank": 1,
    "Title": "Epic Game Winning Shot",
    "score": 92.4,
    "viral_hook": "You won't believe this final play... 🤯",
    "Caption": "Down to the final 3 seconds! Watch what happens next 🔥👀\n\nDrop your reaction in the comments! 👇",
    "Hashtag": [
        "#shorts",
        "#reels",
        "#tiktok",
        "#viral",
        "#fyp",
        "#GameWinner"
    ],
    "Virality_score_description": "Score 92.4/100: High emotional volume spike with strong climax payoff, rapid chat velocity, and instant comment debate potential.",
    "start_time": "01:23:45",
    "end_time": "01:24:30",
    "duration_seconds": 45.0,
    "target_format": "INSTAGRAM_REELS_VERTICAL_9_16_HIGHRES_1080P_MP4",
    "detection_reasons": [
        "audio_volume_spike_+180%",
        "chat_velocity_320%",
        "high_pitch_scream",
        "emote_lol"
    ],
    "transcript": "Oh my god, did you see that?! That was absolutely incredible! I can't believe that just happened!",
    "direct_timestamp_url": "https://youtu.be/jfKfPfyJRdk?t=5025",
    "download_link": "https://api.apify.com/v2/key-value-stores/.../records/clip_1.mp4",
    "download_url": "https://api.apify.com/v2/key-value-stores/.../records/clip_1.mp4",
    "subtitles_url": "https://api.apify.com/v2/key-value-stores/.../records/clip_1.ass",
    "clip_filename": "clip_1.mp4",
    "ffmpeg_command": "ffmpeg -ss 01:23:45 -to 01:24:30 -i \"$(yt-dlp -g -f 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best' 'https://www.youtube.com/watch?v=jfKfPfyJRdk')\" -vf \"scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920:'trunc((in_w-1080)/4)*2':'trunc((in_h-1920)/4)*2',setsar=1,ass='clip_1.ass'\" -c:v libx264 -pix_fmt yuv420p -preset fast -crf 22 -c:a aac -b:a 128k -movflags +faststart clip_1.mp4"
}
```

---

## Configuring the Apify Residential Proxy

To bypass YouTube rate limits and IP blocks, the Actor routes all YouTube network requests through Apify's **RESIDENTIAL** proxy:
```
http://groups-RESIDENTIAL:<APIFY_PROXY_PASSWORD>@proxy.apify.com:8000
```

### Bandwidth Protection (Zero Media Download)
To strictly eliminate proxy data transfer and bandwidth costs:
- **No video or audio streams are ever downloaded.** The Actor operates in **pure transcript mode**.
- **Subtitles and metadata only:** The Actor uses `yt-dlp` with `--skip-download` to fetch only the timestamped subtitle file (< 350 KB total text).
- **Zero media re-encoding:** Memory usage stays under 200 MB and run duration is reduced to just a few seconds.

### How to set `APIFY_PROXY_PASSWORD`:

#### 1. In Apify Console
1. Go to your Actor's page in [Apify Console](https://console.apify.com/)
2. Click **Settings** → **Environment variables**
3. Add a new variable:
   - **Name**: `APIFY_PROXY_PASSWORD`
   - **Value**: Your Apify proxy password (found under [Proxy Settings](https://console.apify.com/proxy))
   - **Secret**: ✅ Check this box
4. Click **Save**

#### 2. Locally for Development
You can set it in your local environment or in a `.env` file (copied from `.env.example`):
```env
APIFY_PROXY_PASSWORD=your_proxy_password
```
Or in PowerShell:
```powershell
$env:APIFY_PROXY_PASSWORD="your_proxy_password"
```

---

## Configuring Gemini AI & Model Selection

The Actor uses Google Gemini to **analyze the timestamped transcript** and identify the most engaging, salient, and viral moments.

### Environment Variables:
1. **`GEMINI_API_KEY`**: Your Google AI Studio API key (get one free at [aistudio.google.com](https://aistudio.google.com/apikey)).
2. **`MODEL`** (Optional): The Gemini model to use. Default is `gemini-flash-lite-latest` (e.g. `gemini-flash-lite-latest`, `gemini-2.0-flash`, `gemini-1.5-pro`).

### How to configure in Apify Console:
1. Go to your Actor's page in [Apify Console](https://console.apify.com/)
2. Click **Settings** → **Environment variables**
3. Set `GEMINI_API_KEY` (check **Secret** ✅)
4. Set `MODEL` to your desired Gemini model (e.g. `gemini-flash-lite-latest`)
5. Click **Save**

---

## How the Algorithm Works

### 1. Pure Transcript Extraction (Zero Media Download)
```
Video URL → yt-dlp (--skip-download) → Subtitle Stream (JSON3 / VTT) → Timestamped Segments
```
- Only lightweight subtitle text (< 350 KB) is extracted. No audio or video is downloaded.
- Automatically selects the original spoken language (e.g., `it-orig`, `en-orig`) or primary captions.

### 2. Salient Moment Detection
- **With Gemini AI (`GEMINI_API_KEY` set):**
  Gemini analyzes the complete narrative flow and pinpoints the core climax, punchline, key revelation, or actionable takeaway with exact core start and end timestamps.
- **Without Gemini (Heuristic Fallback):**
  Analyzes dialogue density (WPM pacing), emotional punctuation (`?`, `!`), and high-impact hook keywords.

### 3. Context Expansion (+10s Pre-Roll & +10s Post-Roll)
For every detected salient moment with core interval `[core_start, core_end]`:
- **Pre-Roll:** `start_seconds = max(0, core_start - 10.0)`
- **Post-Roll:** `end_seconds = min(duration, core_end + 10.0)`
- Guarantees natural setup context before the climax and reaction/payoff after it.

---

## Using the `ffmpeg_command`

Each output record includes a ready-to-use `ffmpeg_command`. To extract a clip:

### Prerequisites
- Install [yt-dlp](https://github.com/yt-dlp/yt-dlp): `pip install yt-dlp`
- Install [ffmpeg](https://ffmpeg.org/download.html)

### Run the command

```bash
# The command from the output, e.g.:
ffmpeg -ss 01:23:45 -to 01:24:30 -i "$(yt-dlp -g -f 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best' 'https://www.youtube.com/watch?v=VIDEO_ID')" -c copy clip_1.mp4
```

> **Note for Windows users**: The `$(...)` subshell syntax works in Git Bash, WSL, or PowerShell with modifications. Alternatively, run the `yt-dlp -g` part first to get the stream URL, then pass it to ffmpeg:
>
> ```powershell
> # Step 1: Get stream URL
> $url = yt-dlp -g -f "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best" "https://www.youtube.com/watch?v=VIDEO_ID"
> # Step 2: Extract clip
> ffmpeg -ss 01:23:45 -to 01:24:30 -i "$url" -c copy clip_1.mp4
> ```

---

## Project Structure

```
youtube-long-to-short-clipper/
├── .actor/
│   ├── actor.json              # Apify Actor configuration
│   ├── input_schema.json       # 2-field input schema
│   ├── output_schema.json      # Output schema
│   └── dataset_schema.json     # Dataset field definitions
├── src/
│   ├── __init__.py
│   ├── __main__.py             # Module entrypoint
│   ├── main.py                 # Main orchestrator
│   ├── audio_analyzer.py       # Audio extraction & analysis
│   ├── chat_analyzer.py        # Chat extraction & spike analysis
│   └── gemini_service.py       # Gemini API integration
├── Dockerfile                  # Python 3.11 + ffmpeg + yt-dlp
├── requirements.txt            # Python dependencies
└── README.md                   # This file
```

---

## Technical Details

### Memory Optimization
- Audio is downloaded as the **lowest bitrate** available (`-f worstaudio`)
- Processing uses **fixed-size chunks** (5s × 22,050 Hz = ~110K samples per window)
- Only essential numpy arrays are kept in memory; audio file is deleted after analysis
- Target memory usage: **< 1 GB** for videos up to 6 hours

### Dependencies
| Package | Purpose |
|---------|---------|
| `apify` | Apify SDK for Actor lifecycle, dataset, key-value store |
| `yt-dlp` | YouTube audio/subtitle extraction (no video download) |
| `chat-downloader` | Live chat replay extraction |
| `numpy` | Numerical computation for signal processing |
| `scipy` | WAV file I/O, spectral analysis |
| `pydub` | Audio format utilities |
| `google-genai` | Google Gemini API client |

### System Requirements (in Docker)
- `ffmpeg`: Audio format conversion
- `libsndfile1`: WAV file reading support

---

## Limitations

- **Chat analysis** only works for videos that had live chat enabled during streaming (VODs/live recordings). Regular uploads without live chat will be analyzed using audio signals only.
- **Subtitle extraction** depends on YouTube auto-subtitles or manual captions being available.
- **Gemini enrichment** requires a valid `GEMINI_API_KEY`. Without it, the Actor still works but uses raw signal-based boundaries.
- **Audio-only detection** may miss visual-only highlights (e.g., text on screen, visual gags).

---

## License

ISC
