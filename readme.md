# Sentinel Home

Sentinel Home is a self-hosted home security system for RTSP cameras. It runs headless on a
small Linux PC: it detects motion, records clips, sends Telegram alerts, and serves a web
viewer with a live camera grid, a DVR-style motion timeline, and a zone editor.

## Docs index

| File | Purpose |
|------|---------|
| `readme.md` | This file: features, behavior, quick start |
| `installation-guide.md` | Step-by-step install, Piper, Telegram, troubleshooting |
| `project-structure.md` | Repository layout and how the pieces fit |
| `handoff_summary.md` | Maintainer / agent handoff notes |

## Key features

- **Motion detection:** OpenCV MOG2 background subtraction, with filtering for false
  alarms. Shadows are ignored, whole-frame light changes (clouds, IR switching, headlights) are
  ignored, and motion must persist over several frames. An optional night bug filter is included.
- **Zones and entry lines:** draw them per camera in the web viewer (`/zones`). Motion counts
  when the bottom of the moving object is inside a zone, or when it crosses a line in the chosen
  direction.
- **Object detection:** people, vehicles and animals are recognised with YOLOX on OpenVINO
  (free, runs locally on the CPU, only while something moves). Alerts read naturally, for
  example *"🚗 A red car pulled into the Driveway (Garage)"*, and come with a snapshot. Parked cars
  don't alert.
- **Recording:** motion-triggered clips, organized by day and camera, indexed for fast
  searching, and cleaned up automatically after 30 days (configurable), with a low-disk safety net.
  A **1440p copy with 5 s pre-roll** is cut from each camera's main stream (ffmpeg stream copy
  from a RAM ring buffer, about 2% CPU per camera).
- **Web viewer:** live MJPEG grid, a motion timeline per camera with a built-in clip player,
  and the zone/line editor. It uses a bcrypt login with lockout, never exposes camera
  credentials, and is meant to be reached through a Cloudflare Tunnel or Tailscale.
- **Telegram:** alerts with a snapshot and clip. Commands: **status / clip / pause / resume /
  help**, **videos** *(time range, e.g. "videos 2pm-4pm people")*, **access** *(how to reach
  Sentinel remotely)*, plus **enable/disable** phrases for recording, motion alerts, clips, the
  night bug filter, and auto-reconnect.
- **Resilience:** each camera runs in its own thread and reconnects on its own with backoff.
  On shutdown (SIGTERM), clips in progress are finalized.
- **Audio alerts (optional):** Piper neural TTS or pyttsx3. This is skipped entirely when the
  volume is 0. Sounds can play on the PC's speakers or in the open Remote Desktop session.
- **Voice assistant (optional, "Hey Jarvis"):** wake word (openWakeWord) and speech recognition
  (faster-whisper `base.en`) run on the PC's CPU; answers are spoken with Kokoro ("Heart" voice,
  streamed sentence by sentence) or Piper. Follow-up questions without the wake word; settings
  change only after a spoken "yes". With the opt-in Claude fallback it also answers general questions.
- **Spotify (optional, Premium):** "play some jazz", pause, skip, volume, "what's playing?" by voice,
  on the household's own Spotify devices (TV app, phones, Echo); a now-playing bar on the live page.
  PKCE sign-in with your own free developer app's Client ID (redirect `http://127.0.0.1:8888/callback`).
- **Structured logging:** JSON lines in `sentinel_events.log`, with passwords redacted, rotated
  at 10 MB.

## Requirements

- Python **3.10+**, Linux
- RTSP cameras (sub-stream recommended for detection)
- **Telegram (optional):** outbound HTTPS to `api.telegram.org`

## Quick install

```bash
git clone https://github.com/billerl/sentinel-home.git
cd sentinel-home
python3 -m venv venv
venv/bin/pip install -r requirements.txt
venv/bin/pip install -r requirements-piper.txt   # optional
# object detection model (Apache-2.0)
mkdir -p models/detector && curl -L -o models/detector/yolox_tiny.onnx \
  https://github.com/Megvii-BaseDetection/YOLOX/releases/download/0.1.1rc0/yolox_tiny.onnx
# optional: delivery-van / vehicle-type recognition (CLIP ViT-B/32, MIT)
mkdir -p models/classifier && curl -L -o models/classifier/clip_vit_b32_vision.onnx \
  https://huggingface.co/Xenova/clip-vit-base-patch32/resolve/main/onnx/vision_model.onnx
# optional: natural voice (Kokoro, Apache-2.0)
mkdir -p models/kokoro && for f in kokoro-v1.0.onnx voices-v1.0.bin; do curl -L -o models/kokoro/$f \
  https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/$f; done
# optional: voice assistant speech recognition (faster-whisper base.en, MIT)
venv/bin/python -c "from huggingface_hub import snapshot_download as d; d('Systran/faster-whisper-base.en', local_dir='models/whisper/base.en')"
venv/bin/python -m sentinel.web.set_password     # web viewer login
venv/bin/python main.py
```

Cameras are configured in `settings.json` → `camera_profiles` (one entry per RTSP source, with
`source` and `display`). In production Sentinel runs as the `sentinel.service` systemd unit.

## Web viewer

- `/`: the live grid. Click a camera for full screen. The timeline under the grid shows a red
  mark for every motion clip; click one to play it. Use **◀ ▶** to change days and
  **24h / 6h / 1h** to zoom.
- `/zones`: pick a camera, then use **+ Zone** (click the corners and close on the yellow first
  point) or **+ Line** (two clicks; the arrow shows the trigger direction). Drag the dots to
  adjust a shape; double-click a dot to remove it. Sensitivity, recording, and the night bug
  filter are set per camera. **Save** applies immediately; no restart is needed.

The server listens on `web.bind_host` (default `127.0.0.1`). Use a Cloudflare Tunnel or
Tailscale for remote access; see `installation-guide.md`.

## Detection and trigger behavior

| Configuration | Motion triggers when |
|----------------|----------------------|
| **No zones, no lines** | Any qualifying motion |
| **Zones only** | The object's ground point (bottom-centre) is inside a zone, or ≥30% of its box overlaps one |
| **Lines only** | A tracked object crosses an entry line in the allowed direction |
| **Zones + lines** | Either of the above |

A qualifying motion must be larger than the camera's minimum area and persist for 3 consecutive
frames. Frames where most of the picture changes at once are treated as lighting changes.

## Settings file

`settings.json` (gitignored, owner-only permissions) holds camera profiles, Telegram settings, web
settings and retention:

| Key | Default | Meaning |
|-----|---------|---------|
| `recording_retention_enabled` | `true` | Delete old clips automatically |
| `recording_retention_days` | `30` | Keep clips this many days |
| `recording_min_free_percent` | `10` | If free disk space drops below this, delete the oldest clips (the last 24 hours are always kept) |

It contains secrets (camera passwords, bot token): don't share it.

## Tests

```bash
venv/bin/python -m unittest discover -s tests
```

Full layout: **`project-structure.md`**.

## License

MIT (see `LICENSE` if present in the repository).
