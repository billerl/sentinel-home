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
├── tools/meshtastic_bridge.py         # Runs on the LoRa base (a Pi): pets' collar trackers -> /owntracks,
│                                      #   mesh chat <-> /mesh/bridge
├── CLAUDE.md                          # Working notes for Claude Code sessions
├── firmware/wifi_motion/              # ESP32-S3 sensor (Arduino): CSI -> motion score, GET /status, setup portal, OTA
├── firmware/wifi_motion_beacon/       # ESP32-S3 beacon: fixed-rate 802.11n ESP-NOW frames for the sensor to measure
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
    │   ├── family.py                  # Family: phones/pets/places glue (OwnTracks posts, arrive/leave + yard
    │   │                              #   alerts, 📍 pins, "where is X?", Family/Timeline page actions)
    │   ├── mesh.py                    # MeshChat: text chat over the Meshtastic mesh via the LoRa base station
    │   │                              #   (its check-ins bring/take messages), invites, Telegram alerts (mesh.db)
    │   ├── recipes.py                 # RecipeBox: family recipes from links (schema.org data, else Claude) and
    │   │                              #   cooking videos (yt-dlp + Whisper + Claude), family version beside the
    │   │                              #   original, swaps/twists, what-can-I-make, cooking mode + Jarvis (recipes.db)
    │   ├── backup.py                  # Backup: nightly copy to a USB drive (databases, settings, photos, recent
    │   │                              #   video) and optionally the snapshot to another computer over SSH
    │   ├── news.py                    # HouseNews: unprompted messages (packages, recap, routines, weather)
    │   ├── cars.py                    # Cars: Vehicles page, "whose car?"/"Wrong car?" buttons,
    │   │                              #   driveway arrive/leave events, car statuses
    │   ├── camera.py                  # CameraPipeline: one thread per camera (capture, motion,
    │   │                              #   recording, clip requests, reconnect with backoff)
    │   ├── objects.py                 # Detection worker, tracking, plain-English descriptions
    │   ├── household.py               # Family cars: photo references, recognition, who's home
    │   ├── camera_light.py            # Camera white light on for cars at night (admin login: camera_admin.json)
    │   ├── location.py                # Family phones (OwnTracks): people, places, arrive/leave, "where is X?",
    │   │                              #   every position (points) and each person's own web login
    │   ├── timeline.py                # Family timeline: positions -> stops + drives for one day
    │   ├── weather.py                 # Weather tile data (Open-Meteo, server-side, cached 10 min)
    │   ├── weather_alerts.py          # Severe weather warnings (National Weather Service): voice, Telegram, banner
    │   ├── packages.py                # Package alerts: something left at a door camera after a visit
    │   ├── recap.py                   # Morning recap: the night in one message + a highlight video
    │   ├── spotify.py                 # Spotify Web API: PKCE sign-in, play/pause/skip/volume, now playing, voice phrases
    │   ├── assistant.py               # Opt-in: Telegram messages Sentinel doesn't understand -> `claude -p`
    │   ├── reminders.py               # "Remind me ..." -> Telegram message at that time (reminders.db)
    │   ├── switches.py                # Smart plugs/switches (Kasa, local protocol): Live page panel, voice, Telegram
    │   ├── samsung_tv.py              # Samsung TVs as a switch (Wake-on-LAN + power key), apps, volume, mute
    │   ├── remotes.py                 # Learned IR/RF remote buttons sent by a BroadLink RM4 (Remotes page, voice, Telegram)
    │   ├── heater.py                  # Heater thermostat: steps a remote-only heater's setting toward a target
    │   ├── fire_tv.py                 # Fire TVs over ADB: switch, remote, apps, "put the cameras on the TV"
    │   ├── tv_mirror.py               # the remote's live picture of a Fire TV's screen (screenrecord -> JPEGs)
    │   ├── tasmota.py                 # plugs running Tasmota firmware as switches (local HTTP; network sweep)
    │   ├── wifi_motion.py             # Wi-Fi motion sensors (ESP32 CSI boards): find, poll, judge motion, daily CSVs
    │   ├── house.py                   # House page: the 3D drawing of the home (house.json) + where each device sits
    │   ├── routines.py                # "When X, only if Y, then Z": check/describe, sunrise/sunset maths, runner (routines.db), Wi-Fi sensor rules + 24 h replay
    │   ├── questions.py               # Telegram questions ("did anyone come by?", "is Kate home?")
    │   ├── commands.py                # Command registry: what both Telegram and voice understand, registered
    │   │                              #   once each (Engine._build_commands); the enable/disable parser
    │   ├── queries.py                 # "videos <when>" parsing
    │   └── admin.py                   # Settings schema, status, access message helpers
    ├── detection/
    │   ├── motion_detector.py         # Capture + MOG2 motion analysis, zones/lines, clip writing
    │   ├── nuisance.py                # Mutes spots where motion never stops (web on the lens, branches)
    │   ├── objects.py                 # YOLOX object detector (OpenVINO)
    │   ├── vehicle_type.py            # CLIP zero-shot vehicle type / delivery-company recognition
    │   ├── package_check.py           # CLIP "is this a package?" for things left at the door (+ package_prompts.npz)
    │   ├── car_fingerprint.py         # DINOv2 fingerprints: which of the family's cars is this?
    │   ├── vehicle_prompts.npz        # Precomputed CLIP text embeddings (tools/build_vehicle_prompts.py)
    │   └── network_discovery.py       # RTSP discovery / resolve / endpoints
    ├── recordings/
    │   └── store.py                   # Clip index (SQLite), retention, MP4 transcoding for browsers
    ├── web/                           # Web viewer = the user interface
    │   ├── server.py                  # Flask app: settings, sign-in setup, listeners; calls each routes/ module
    │   ├── context.py                 # WebContext: what every area's routes share (session check, write guard)
    │   ├── routes/                    # Pages + API, one module per area, each with register(app, ctx):
    │   │                              #   signin, live, cameras, recordings, family, vehicles, switches (+ TV
    │   │                              #   remote), spotify, sensors, house, routines, settings, mesh, recipes
    │   ├── auth.py                    # Bcrypt login, sessions (saved as hashes in .web_sessions.json),
    │   │                              #   lockout / backoff, QR sign-in pairing for TVs
    │   ├── frame_broker.py            # Latest frame per camera; opaque ids; lazy JPEG encoding
    │   ├── bind_util.py               # Refuse binding on camera-only subnets
    │   ├── set_password.py            # CLI: `python -m sentinel.web.set_password`
    │   ├── static/                    # Icons + web app manifest; leaflet/ = Leaflet 1.9.4 (BSD-2, map on the Timeline),
    │   │                              #   three/ = three.js r128 + OrbitControls (MIT, the House page),
    │   │                              #   grid.js + grid.css = the live page's script and styles (?v=boot id)
    │   └── templates/                 # login (+ QR), pair (phone approves a TV), home (`/`: weather, who's home,
    │                                  #   page tiles, switches), grid (`/live`: cameras + weather/clock +
    │                                  #   family lights + switches + timeline + player), zones, settings, vehicles,
    │                                  #   sensors (Wi-Fi motion lines) + sensors_history (movement by day),
    │                                  #   routines (when X then Y editor), house (3D map of the home, live),
    │                                  #   people (Family page), join (setup link opened on a family phone),
    │                                  #   timeline (Family timeline), account (a person picks their own password),
    │                                  #   mesh (Mesh chat: conversations, invite QR codes, radios heard),
    │                                  #   recipes (recipe box, one recipe, cooking mode),
    │                                  #   remotes (learned IR/RF buttons: teach, press, rename),
    │                                  #   lost (Lost dog: live collar map, search on/off, beep)
    ├── notifications/
    │   └── telegram_notifier.py       # HTTPS Bot API, commands, clips
    ├── audio/
    │   ├── alert_system.py            # Piper TTS + `aplay` (or the Remote Desktop session via `paplay`),
    │   │                              #   pyttsx3 fallback (skipped when volume is 0), chimes
    │   ├── alsa-sentinel.conf         # dmix: the line-out shared by voice/alerts and the Spotify speaker
    │   ├── devices.py                 # which sound card listens/speaks (PC jacks or a USB speakerphone); music's card
    │   ├── spotify_player.py          # this PC as a Spotify speaker: runs bin/spotifyd, its sign-in relay
    │   └── voice.py                   # "Hey Jarvis": arecord -> band-pass -> openWakeWord -> faster-whisper
    │                                  #   (+ an optional trial wake word that only logs scores)
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
