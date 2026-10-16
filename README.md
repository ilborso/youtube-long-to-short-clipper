# YouTube Highlight Clipper – Apify Actor

Automatically detect the most engaging moments in videos (direct video files or long YouTube videos such as podcasts, interviews, and streams) and generate ready-to-use clips optimized for YouTube Shorts, Instagram Reels, and TikTok.

## High-Level Workflow

The Actor runs an automated pipeline to ingest, analyze, and produce ready-to-use social clips:
1. **Input Validation**: Accepts either a direct video file (`file_video`) or a YouTube link (`youtube_url`) under strict mutual exclusivity.
2. **Audio & Speech Extraction**: Ingests audio and subtitle streams (via embedded tracks, speech-to-text, or YouTube caption streams) to obtain precise word-level timestamps.
3. **Salient Moment Detection**: Analyzes dialogue flow, retention spikes, and pacing to identify high-potential viral highlights.
4. **Context Framing & Subtitles**: Attaches balanced pre-roll and post-roll context buffers around each highlight and generates dynamic animated karaoke subtitles.
5. **Output Generation**: Trims downloadable MP4 video clips (for `file_video`) or produces direct timestamp links (for `youtube_url`), accompanied by complete social media copy kits (`viral_hook`, `Caption`, `Hashtag`, `Virality_score_description`, `transcript`) and ready-to-use FFmpeg commands.

A visual overview of the end-to-end processing pipeline is shown below (also available via [actor_workflow_diagram.png](resources/actor_workflow_diagram.png)):

![Actor Workflow Diagram](https://raw.githubusercontent.com/ilborso/resources/266c2e480a3f96a1c157d805be88dfac74200635/actor_workflow_diagram.png)

---

## Input Parameters

### Video Input Source Exclusivity Rule

The `file_video` and `youtube_url` parameters are **mutually exclusive**:
- Exactly one of the two parameters must be provided. If both or neither are provided, the Actor stops execution and raises a validation error.
- **When `file_video` is used**: The video file is downloaded, processed, and automatically cut into clips. You get the pre-cut video file ready for direct download via the **`download_url`** field, while **`Download Link`** (`download_link`) is set to null (`null`). A ready-to-use FFmpeg command to extract and encode the clip is also provided.
- **When `youtube_url` is used**: The video file is NOT downloaded. Instead, you get direct links to the exact timestamps of the detected clips via **`Download Link`** (`download_link`, `direct_timestamp_url`), while **`download_url`** remains empty/null (`null`). A ready-to-use FFmpeg command is also provided if you wish to extract and encode the clip locally.
- **Ready-to-use Social Media Copy Kit (included in both modes)**: Regardless of which input option is used (`file_video` or `youtube_url`), every output record always provides complete social media data (`viral_hook`, `Caption`, `Hashtag`, `Virality_score_description`, and `transcript`) ready to be copy-pasted directly to social networks (Instagram Reels, TikTok, YouTube Shorts).

### Input Parameters Table

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `file_video` | String | No* | — | Direct URL to a video file (.mp4, .mov, .mkv, .webm). Mutually exclusive with `youtube_url`. When used, the video is downloaded and trimmed, giving you the pre-cut video file ready for download in `download_url`. |
| `youtube_url` | String | No* | — | URL of a YouTube video (e.g., `https://www.youtube.com/watch?v=...` or `https://youtu.be/...`). Mutually exclusive with `file_video`. The video is not downloaded; instead, you get the direct link to the exact clip timestamp in `Download Link`. |
| `max_clips` | Integer | No | `5` | Maximum number of top highlight clips to extract (1 to 50). |
| `max_length` | Integer | No | `60` | Maximum duration in seconds for each clip (10 to 300 seconds). |
| `targetFormat` | String | No | `INSTAGRAM_REELS_VERTICAL_9_16_HIGHRES_1080P_MP4` | Target format, aspect ratio, and resolution for social media clips. Supports vertical 9:16 profiles (1080p and 720p for Reels, TikTok, Shorts), square 1:1, portrait 4:5, or original format. |
| `burn_subtitles` | Boolean | No | `true` | If enabled (`true`), burns dynamic animated karaoke-style subtitles (with active word-by-word highlight) directly onto the video. |

\* *Exactly one of `file_video` or `youtube_url` must be specified.*

### Example Input: Direct Video File

```json
{
  "file_video": "https://github.com/ilborso/resources/raw/refs/heads/main/Interstella.mp4",
  "max_clips": 2,
  "max_length": 25,
  "targetFormat": "YOUTUBE_SHORTS_VERTICAL_9_16_LOWRES_720P_MP4",
  "burn_subtitles": true
}
```

### Example Input: YouTube Video Link

```json
{
  "youtube_url": "https://www.youtube.com/watch?v=jfKfPfyJRdk",
  "max_clips": 5,
  "max_length": 60,
  "targetFormat": "TIKTOK_FEED_VERTICAL_9_16_HIGHRES_1080P_MP4",
  "burn_subtitles": true
}
```

---

## Output Dataset Structure

Processed results are saved to the default Apify dataset with the following structure. In both modes (`file_video` and `youtube_url`), every record provides complete, ready-to-publish social media data (`viral_hook`, `Caption`, `Hashtag`, `Virality_score_description`, and `transcript`):

### Output Fields Table

| Field | Type | Description |
|---|---|---|
| `rank` | Integer | Rank position of the highlight moment (1 = highest score). |
| `Title` | String | Catchy title generated for the clip (pure text, no icons or emojis). |
| `score` | Float | Composite engagement and virality score from 0 to 100. |
| `viral_hook` | String | Opening 3-second hook for on-screen attention (pure text, no icons or emojis). |
| `Caption` | String | Social media post copy with narrative context and call-to-action (pure text, no icons or emojis). |
| `Hashtag` | Array | List of 5-8 relevant and trending hashtags for social posting. |
| `Virality_score_description` | String | Explanation of the virality score rationale and retention triggers (pure text, no icons or emojis). |
| `start_time` | String | Clip start timestamp formatted as `HH:MM:SS`. |
| `end_time` | String | Clip end timestamp formatted as `HH:MM:SS`. |
| `duration_seconds` | Float | Clip duration in seconds. |
| `target_format` | String | Video format and resolution profile applied to the clip. |
| `detection_reasons` | Array | Signals and factors that triggered the selection of this highlight. |
| `transcript` | String | Spoken dialogue transcribed within the highlight segment. |
| `download_url` | String / Null | Direct download link for the cut MP4 video clip. Populated when input is `file_video`; null (`null`) when input is `youtube_url`. |
| `Download Link` (`download_link`) | String / Null | Direct YouTube link jumping to the highlight timestamp. Populated when input is `youtube_url`; null (`null`) when input is `file_video`. |
| `direct_timestamp_url` | String / Null | Direct YouTube link jumping to the start of the clip (returned when `youtube_url` is used). |
| `clip_filename` | String / Null | Filename of the trimmed clip (e.g. `clip_1.mp4`, returned when `file_video` is used). |
| `subtitles_url` | String / Null | Download link for the animated ASS subtitle file. |
| `ffmpeg_command` | String | Ready-to-use FFmpeg command to extract and encode the clip. |

### Example Output Record (when using `file_video`)

```json
{
  "rank": 1,
  "Title": "The Interstellar Watch You Can Actually Buy",
  "score": 92.0,
  "start_time": "00:00:00",
  "end_time": "00:00:25",
  "duration_seconds": 25.0,
  "target_format": "YOUTUBE_SHORTS_VERTICAL_9_16_LOWRES_720P_MP4",
  "viral_hook": "The Interstellar Movie Watch",
  "Caption": "Did you know you can buy the exact watch from Christopher Nolan's Interstellar? Here is everything you need to know about the Hamilton Khaki Murph 42mm. Would you wear this everyday? Let us know in the comments.",
  "Hashtag": [
    "#shorts",
    "#reels",
    "#viral",
    "#interstellar",
    "#hamiltonwatch",
    "#watchcollecting"
  ],
  "Virality_score_description": "Leverages mass pop culture appeal from a beloved movie like Interstellar, instantly capturing viewer attention within the first second.",
  "detection_reasons": [
    "The Interstellar Watch You Can Actually Buy",
    "Mentions a major blockbuster movie connection immediately",
    "+10s_context_buffer"
  ],
  "transcript": "A month ago, I bought a Hamilton Carkey Murph 42mm as a bit of a treat for myself and I've worn it every single day since...",
  "clip_filename": "clip_1.mp4",
  "clip_size_mb": 1.24,
  "download_url": "https://api.apify.com/v2/key-value-stores/1DMJuZ8OdjTGq8YZW/records/clip_1.mp4?signature=tJvlQNgO8lanHr7Rv1Ua",
  "download_link": null,
  "Download Link": null,
  "subtitles_url": "https://api.apify.com/v2/key-value-stores/1DMJuZ8OdjTGq8YZW/records/clip_1.ass?signature=O0Olrqd0uWio81s4dkkV",
  "ffmpeg_command": "ffmpeg -ss 00:00:00 -to 00:00:25 -i \"https://github.com/ilborso/resources/raw/refs/heads/main/Interstella.mp4\" -vf \"scale=720:1280:force_original_aspect_ratio=increase,crop=720:1280:'trunc((in_w-720)/4)*2':'trunc((in_h-1280)/4)*2',setsar=1,ass='clip_1.ass'\" -c:v libx264 -pix_fmt yuv420p -preset fast -crf 22 -c:a aac -b:a 128k -movflags +faststart clip_1.mp4"
}
```

### Example Output Record (when using `youtube_url`)

```json
{
  "rank": 1,
  "Title": "Epic Game Winning Play",
  "score": 92.4,
  "start_time": "01:23:45",
  "end_time": "01:24:30",
  "duration_seconds": 45.0,
  "target_format": "TIKTOK_FEED_VERTICAL_9_16_HIGHRES_1080P_MP4",
  "viral_hook": "You will not believe this final play...",
  "Caption": "Down to the final 3 seconds! Watch what happens next. Drop your reaction in the comments!",
  "Hashtag": [
    "#shorts",
    "#reels",
    "#tiktok",
    "#viral",
    "#fyp"
  ],
  "Virality_score_description": "High emotional volume spike with strong climax payoff, rapid chat velocity, and instant comment debate potential.",
  "detection_reasons": [
    "audio_volume_spike_+180%",
    "chat_velocity_320%",
    "+10s_context_buffer"
  ],
  "transcript": "Oh my god, did you see that?! That was absolutely incredible! I can't believe that just happened!",
  "download_url": null,
  "download_link": "https://youtu.be/jfKfPfyJRdk?t=5025",
  "Download Link": "https://youtu.be/jfKfPfyJRdk?t=5025",
  "direct_timestamp_url": "https://youtu.be/jfKfPfyJRdk?t=5025",
  "ffmpeg_command": "ffmpeg -ss 01:23:45 -to 01:24:30 -i \"$(yt-dlp -g -f 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best' 'https://www.youtube.com/watch?v=jfKfPfyJRdk')\" -vf \"scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920:'trunc((in_w-1080)/4)*2':'trunc((in_h-1920)/4)*2',setsar=1,ass='clip_1.ass'\" -c:v libx264 -pix_fmt yuv420p -preset fast -crf 22 -c:a aac -b:a 128k -movflags +faststart clip_1.mp4"
}
```
