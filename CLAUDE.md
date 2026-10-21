# Sentinel Home — notes for Claude

Self-hosted home security for RTSP cameras: motion + object detection, recordings with a
timeline, natural-language Telegram alerts and questions, all in a web UI. Everything must stay
**free and local** (no paid APIs, no cloud AI). Start with `readme.md` (features, install) and
`handoff_summary.md` (architecture and design decisions); `project-structure.md` maps the files.

## This machine (production)
- Runs as systemd `sentinel.service`: `xvfb-run venv/bin/python main.py` from this folder.
  The venv is `./venv`. Settings live in `settings.json` (contains camera and Telegram
  credentials: never print, commit or paste them; logs are redacted by `logging_utils.redact`).
- **Deploy** = send SIGTERM to the python process; systemd restarts it in ~10 s (no sudo):
  `kill -TERM $(pgrep -f "^$PWD/venv/bin/python main.py")`, then check `sentinel_events.log`
  for `engine_started`, `object_detector_ready` (`device: GPU`) and three `camera_connected`.
  Recording pauses for those seconds only; keep it that way (no long-running restarts).
- Web UI on 127.0.0.1:8088, public only via the Cloudflare tunnel (sentinel.honeycrick.com).
- Detection runs on the Intel UHD 630 via OpenVINO (`inference_device: auto`). The user is in the
  `render` group; a shell started before that change needs `sg render -c "..."` to see the GPU.
  Compiled models are cached in `models/.ov_cache` (first GPU compile ~20 s per model).
- Models are not in git: `models/detector/yolox_tiny.onnx`, `models/classifier/clip_vit_b32_vision.onnx`
  (download commands in readme). Piper voice models in `models/` are tracked.
- Git: portable git — `source ../set-git-path.sh` first. No global identity is set; commit with
  `git -c user.name="Luke Biller" -c user.email="biller.luke@gmail.com" commit ...`.
  **Ask before every push** (remote: github.com/billerl/sentinel-home).

## Tests and checks
- Unit tests: `PYTHONPATH=.:tests venv/bin/python -m unittest discover -s tests` (must all pass
  before deploying). Every test module imports `_test_env` first so tests never write to the
  production `sentinel_events.log`; keep that for new test files.
- Detection changes: replay real footage before and after — `venv/bin/python tools/replay_detections.py <YYYY-MM-DD> [max_clips]`.
  Thresholds in `sentinel/engine/objects.py` were tuned this way; don't change them from intuition.
- UI changes: `venv/bin/python tools/dev_harness.py [seconds]` serves a login-free copy on
  127.0.0.1:18091 (drive it with headless Firefox/Selenium). Check phone widths (~390 px portrait,
  ~844x390 landscape) as well as desktop; the live page has a separate phone layout.
- Vehicle-type prompts (`VEHICLE_TYPES` in `sentinel/detection/vehicle_type.py`) need
  `tools/build_vehicle_prompts.py` afterwards; re-check against household cars (no false deliveries).

## Pitfalls learned the hard way
- Never run a second engine without `SENTINEL_BUFFER_ROOT` (the harness sets it): two engines share
  `/dev/shm/sentinel-buffer` and wipe each other's HD buffers.
- The cameras accept only a few RTSP sessions; extra ones get "500 Internal Server Error". After
  any test that opens camera streams, check for leftover ffmpeg: `ps -eo pid,ppid,args | grep ffmpeg`
  (the live ones are children of the service's python process).
- Don't `pgrep -f`/`pkill -f` a pattern that also appears in your own shell command line (it
  matches the shell itself and kills it, or makes wait-loops never finish). Anchor the pattern
  (`^/home/.../python main.py`) or use PIDs.
- The bundled ffmpeg (imageio-ffmpeg) crashes reading MPEG-TS from these cameras; HD segments are Matroska.
- HD segments are only written when they close (~2 s); anything reading them must wait (see
  `HDRecorder.keyframe_near`).
- Telegram polling (`getUpdates`) is shared: a second running bot instance steals the user's messages.
- `recorded_videos/household.db` and `.household/` hold the user's own labels (which photo is whose car).
  Never delete or rebuild sightings without checking `vehicle_id`/`ignored` first — the user may be
  labelling on the Vehicles page while you work.
- Night = infrared: use `is_greyscale` (per-pixel channel spread), not saturation. CLIP tells cars apart
  well by day but poorly in IR; family-car identity at night comes from parking-spot stickiness.
- In the web editors, never grow JS arrays by setting `.length` — the holes are sent as `null` (this
  once named a zone "None"). Server-side cleaners must treat non-strings as "".

## Conventions
- Code style: match the surrounding code; short docstrings that say *why*; no new dependencies
  without a good reason (and they must be free/open-source).
- User-facing text (web Help page in `settings.html`, Telegram replies) is plain English for a
  non-developer; update the Help section and `_commands_help_text` when adding user features.
- Keep `handoff_summary.md` and `project-structure.md` current when adding modules or behaviour.
