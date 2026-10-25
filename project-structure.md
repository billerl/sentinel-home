# Sentinel Home Project Structure

```
sentinel-home/                         # repository root
├── main.py                            # Headless entry point: starts the Engine, handles SIGTERM
├── setup.py                           # setuptools package metadata
├── requirements.txt                   # Core Python dependencies
├── requirements-piper.txt             # Optional Piper TTS (`pip install -r ...`)
├── readme.md                          # User documentation
├── installation-guide.md              # Installation + troubleshooting
├── project-structure.md               # This file
├── handoff_summary.md                 # Cross-session continuity for contributors
├── tools/build_vehicle_prompts.py     # Rebuilds the CLIP vehicle-type text embeddings
├── tools/replay_detections.py        # Replays recorded clips through detection (tune thresholds)
├── tools/dev_harness.py              # Login-free test copy of the web UI on :18091 (dev only)
├── CLAUDE.md                          # Working notes for Claude Code sessions
├── models/                            # Piper `.onnx` + `.onnx.json` voice models (local, not all in git)
├── tests/                             # Unit tests (`venv/bin/python -m unittest discover -s tests`)
├── captured_images/                   # Saved snapshots (gitignored)
├── recorded_videos/                   # Clips by day + recordings.db index (gitignored)
├── settings.json                      # Runtime settings incl. secrets (gitignored, mode 600)
├── sentinel_events.log                # Structured JSONL event log, rotated at 10 MB (gitignored)
└── sentinel/                          # Application package
    ├── constants.py                   # Defaults (paths, FPS bounds, recording)
    ├── logging_utils.py               # JSONL logger with credential redaction + rotation
    ├── engine/
    │   ├── engine.py                  # Engine: cameras, Telegram control/alerts, audio, web, config API
    │   ├── camera.py                  # CameraPipeline: one thread per camera (capture, motion,
    │   │                              #   recording, clip requests, reconnect with backoff)
    │   ├── objects.py                 # Detection worker, tracking, plain-English descriptions
    │   ├── household.py               # Family cars: photo references, recognition, who's home
    │   ├── weather.py                 # Weather tile data (Open-Meteo, server-side, cached 10 min)
    │   ├── questions.py               # Telegram questions ("did anyone come by?", "is Steph home?")
    │   ├── queries.py                 # "videos <when>" parsing
    │   └── admin.py                   # Settings schema, status, access message helpers
    ├── detection/
    │   ├── motion_detector.py         # Capture + MOG2 motion analysis, zones/lines, clip writing
    │   ├── nuisance.py                # Mutes spots where motion never stops (web on the lens, branches)
    │   ├── objects.py                 # YOLOX object detector (OpenVINO)
    │   ├── vehicle_type.py            # CLIP zero-shot vehicle type / delivery-company recognition
    │   ├── vehicle_prompts.npz        # Precomputed CLIP text embeddings (tools/build_vehicle_prompts.py)
    │   └── network_discovery.py       # RTSP discovery / resolve / endpoints
    ├── recordings/
    │   └── store.py                   # Clip index (SQLite), retention, MP4 transcoding for browsers
    ├── web/                           # Web viewer = the user interface
    │   ├── server.py                  # Flask routes: login, live grid, MJPEG, timeline, zone editor API
    │   ├── auth.py                    # Bcrypt login, sessions (saved as hashes in .web_sessions.json),
    │   │                              #   lockout / backoff, QR sign-in pairing for TVs
    │   ├── frame_broker.py            # Latest frame per camera; opaque ids; lazy JPEG encoding
    │   ├── bind_util.py               # Refuse binding on camera-only subnets
    │   ├── set_password.py            # CLI: `python -m sentinel.web.set_password`
    │   ├── static/                    # Icons + web app manifest
    │   └── templates/                 # login (+ QR), pair (phone approves a TV), grid (live + weather +
    │                                  #   timeline + player), zones, settings, vehicles
    ├── notifications/
    │   └── telegram_notifier.py       # HTTPS Bot API, commands, clips
    ├── audio/
    │   └── alert_system.py            # Piper TTS + `aplay`, pyttsx3 fallback (skipped when volume is 0)
    └── settings/
        └── manager.py                 # `settings.json` load/save (locked, atomic, owner-only)
```

## How it fits together

- **`main.py`** builds an `Engine` and blocks until SIGTERM/SIGINT, then stops it so in-progress
  clips are finalized and settings saved.
- **`Engine`** reads `camera_profiles` from settings and starts one **`CameraPipeline`** per camera.
  Pipelines report motion, finished clips and up/down status back through callbacks; the engine
  turns those into Telegram alerts, audio, and web updates. The engine also answers Telegram
  commands and the web viewer's camera-settings API.
- **`CameraPipeline`** owns its camera: it reconnects on its own (with backoff), so one camera
  dropping never affects the others. It publishes frames to the `FrameBroker` for the web viewer.
- **`MotionDetector`** does the per-frame work: MOG2 background subtraction with shadow removal,
  a whole-frame lighting-change guard, a multi-frame persistence requirement, zone tests on the
  object's ground point (or overlap), entry-line crossing, and clean (un-annotated) clip writing.
- **`RecordingStore`** names and indexes clips, applies retention (30 days by default, plus a
  low-disk safety net), and converts clips to H.264 MP4 on demand for browser playback.
- **Web viewer** (served on 127.0.0.1, exposed through the Cloudflare tunnel): live grid,
  motion timeline with clip player, and the zone/line editor at `/zones`.

There is no desktop GUI: Sentinel runs as the `sentinel.service` systemd unit.
