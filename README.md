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
| `video_url` | String | ✅ Yes | — | Any valid YouTube video URL (`youtube.com/watch?v=...`, `youtu.be/...`, `/live/...`) |
| `max_clips` | Integer | No | `5` | Number of top highlight segments to return (1–50) |

### Example Input

```json
{
    "video_url": "https://www.youtube.com/watch?v=jfKfPfyJRdk",
    "max_clips": 5
}
```

---

## Output Dataset Structure

Each detected highlight is pushed to the Apify dataset with these fields:

| Field | Type | Description |
|-------|------|-------------|
| `rank` | Integer | Rank (1 = highest score) |
| `score` | Float | Composite engagement score (0–100) |
| `start_time` | String | Clip start in `HH:MM:SS` format |
| `end_time` | String | Clip end in `HH:MM:SS` format |
| `duration_seconds` | Number | Clip duration in seconds (20–60s) |
| `detection_reasons` | Array | Trigger explanations (e.g., `["laughter_spike", "chat_velocity_320%"]`) |
| `transcript` | String | Spoken text during the highlight segment |
| `direct_timestamp_url` | String | Direct YouTube link to the moment (`https://youtu.be/<id>?t=<seconds>`) |
| `ffmpeg_command` | String | Copy-pasteable command to extract this clip losslessly |

### Example Output Record

```json
{
    "rank": 1,
    "score": 87.4,
    "start_time": "01:23:45",
    "end_time": "01:24:30",
    "duration_seconds": 45.0,
    "detection_reasons": [
        "audio_volume_spike_+180%",
        "chat_velocity_320%",
        "high_pitch_scream",
        "emote_lol"
    ],
    "transcript": "Oh my god, did you see that?! That was absolutely incredible! I can't believe that just happened!",
    "direct_timestamp_url": "https://youtu.be/jfKfPfyJRdk?t=5025",
    "ffmpeg_command": "ffmpeg -ss 01:23:45 -to 01:24:30 -i \"$(yt-dlp -g -f 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best' 'https://www.youtube.com/watch?v=jfKfPfyJRdk')\" -c copy clip_1.mp4"
}
```

---

## Configuring the Apify Residential Proxy

To bypass YouTube rate limits and IP blocks, the Actor routes all YouTube network requests through Apify's **RESIDENTIAL** proxy:
```
http://groups-RESIDENTIAL:<APIFY_PROXY_PASSWORD>@proxy.apify.com:8000
```

### Bandwidth Protection (Zero Video Download)
To strictly minimize proxy data transfer and bandwidth costs:
- **No video streams are ever downloaded.** Only the lowest-bitrate compressed audio stream (`-f worstaudio`, strictly `vcodec=none`, ~15–25 MB/hr) is fetched and post-processed locally with FFmpeg to 22 kHz mono WAV.
- **Subtitles and metadata** use `--skip-download` / `skip_download: True` (pure JSON/text, < 100 KB).
- **Chat replay** streams only JSON text messages via YouTube's live chat endpoints.

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

## Configuring the Gemini API Key

The Actor uses Google Gemini to **refine clip boundaries** and **polish transcripts**. The API key is **optional** — the Actor works without it, but enrichment is skipped.

### How to set `GEMINI_API_KEY` in Apify Console:

1. Go to your Actor's page in [Apify Console](https://console.apify.com/)
2. Click **Settings** → **Environment variables**
3. Add a new variable:
   - **Name**: `GEMINI_API_KEY`
   - **Value**: Your Google AI Studio API key (get one free at [aistudio.google.com](https://aistudio.google.com/apikey))
   - **Secret**: ✅ Check this box
4. Click **Save**

> 💡 Without Gemini, clip timestamps and transcripts are based on raw signal detection. With Gemini, boundaries are refined to natural narrative breaks and transcripts are cleaned up.

---

## How the Algorithm Works

### 1. Audio Analysis Pipeline
```
Video URL → yt-dlp (worst audio) → WAV (22kHz mono) → Chunked Analysis
    ├── Rolling RMS (energy/dBFS) → Volume spike detection
    ├── Spectral Centroid → Scream/excitement frequency tracking
    ├── Zero-Crossing Rate → Laughter burst detection
    └── Event Classification → Tags (laughter_spike, high_pitch_scream, etc.)
```

### 2. Chat Analysis Pipeline
```
Video URL → chat_downloader → Sorted Messages → Sliding Windows (5s)
    ├── Message count per window → Velocity calculation
    ├── Moving average baseline (60s) → Spike detection (>250% = spike)
    └── Keyword/emote pattern scanning → Tag generation
```

### 3. Multi-Signal Fusion
```
Audio Score (0-100) × 0.60  ──┐
                               ├──→ Composite Score (0-100)
Chat Score (0-100) × 0.40  ──┘
                               ↓
                        Peak Selection
                        (non-overlapping, sorted by score)
                               ↓
                     Contextual Windowing
                     (8-12s pre-roll, 5-8s post-roll)
                     (capped 20-60s for Shorts format)
                               ↓
                      Gemini Enrichment
                      (refine boundaries, polish transcript)
```

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
