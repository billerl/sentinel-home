# Sentinel Handoff Summary

Use this file as continuity across chats. At the **start** of a new session, ask the assistant to read this file, continue from **Current Status**, and update this file before wrapping up.

---

## Project

- **Name:** Sentinel Home  
- **Repo:** `https://github.com/billerl/sentinel-home.git`  
- **Primary branch:** `main`  
- **Clone path:** `git clone … && cd sentinel-home` (repo folder name `sentinel-home`)

---

## Current Status (October 2026 restructure — supersedes the Qt notes below)

- **Headless engine.** The PySide6 GUI was removed. It only ever ran invisibly under `xvfb-run`.
  `main.py` → `sentinel/engine/engine.py` (`Engine`) → one `CameraPipeline` thread per camera
  profile (`sentinel/engine/camera.py`). The web viewer is the UI; Telegram is the remote control.
- **Deploy:** `sentinel.service` runs `xvfb-run venv/bin/python main.py`. Xvfb is no longer
  needed, but the unit still works with it. To deploy without sudo, SIGTERM the python process;
  systemd restarts it in about 10 s, and SIGTERM finalizes in-progress clips.
- **Web viewer:** listens on `127.0.0.1:8088` only and is reached through the Cloudflare tunnel.
  - `/`: live grid plus the motion timeline and clip player.
  - `/zones`: zone/line editor. It uses `/api/config/cameras`; POSTs need JSON plus the `X-Sentinel-Request: 1` header.
  - Cameras get opaque per-process ids, so RTSP URLs never reach the browser.
- **Recordings:**
  - Files are `recorded_videos/YYYY-MM-DD/<camera-ip>_HHMMSS_ms.avi`, indexed in `recorded_videos/recordings.db` by `sentinel/recordings/store.py`.
  - Retention: 30 days, plus a low-disk safety net that always keeps the last 24 hours.
  - Clips are recorded clean, with no overlays, at the camera's real fps.
  - Browsers get H.264 MP4 converted on demand with the bundled ffmpeg (`imageio-ffmpeg`).
- **Motion quality:**
  - Analysis runs at 320 px width.
  - MOG2 shadows are thresholded away, and frames where more than 45% of the picture changes are ignored.
  - Motion must persist for 3 frames.
  - Zone test: the bottom-centre point is in the zone, or the box overlaps it by at least 30%.
- **Cameras** (IPC-D3E43WD-S): main streams were set to 2560x1440 H.264 at 4 Mbps (not used yet).
  Substreams are 640x360 at 12 fps and are used for live view and detection.
- **HD clips** (`sentinel/recordings/hd_recorder.py`):
  - ffmpeg copies each main stream into 2 s **Matroska** segments in `/dev/shm/sentinel-buffer/<ip>`.
  - MPEG-TS segments from these cameras segfault the bundled static ffmpeg 7.0.2 when read back.
  - Finished clips are joined into `<clip>_hd.mp4` with 5 s pre-roll and 3 s post-roll. `recordings.hd_path` stores it; playback and Telegram prefer it.
- **Object detection:**
  - `sentinel/detection/objects.py` runs YOLOX-tiny ONNX on OpenVINO, CPU only. The user isn't in the `render` group and there's no OpenCL runtime.
  - `sentinel/engine/objects.py` has a single `DetectionWorker` thread and a per-camera `CameraObjectTracker`.
  - Tracker rules: 2 hits needed; distance fallback for matching fast movers; label voting; vehicles must move; motionless people need score ≥ 0.65; learned static spots.
  - It also writes the plain-English `describe()` text.
  - Models live in `models/detector/` (gitignored); download them from the YOLOX GitHub releases.
  - Delivery / vehicle-type recognition: `sentinel/detection/vehicle_type.py` runs the CLIP ViT-B/32 image
    encoder (`models/classifier/clip_vit_b32_vision.onnx`, gitignored, see readme) on each alerting
    vehicle's largest crop and compares it with precomputed text embeddings
    (`sentinel/detection/vehicle_prompts.npz`). After editing `VEHICLE_TYPES`, rebuild them with
    `tools/build_vehicle_prompts.py`. Recognised deliveries add a `delivery` clip label and alert group;
    vehicles are re-checked (up to 3 times) as they get closer, which can send a second "📦" alert.
  - Far vehicles (box < 1/4 of the frame width) are checked on the HD stream instead: a background
    thread (`DetectionWorker.run_later`) waits for the segment holding the sighting to close, decodes its
    keyframe (`HDRecorder.keyframe_near`, ~0.6 CPU-s), re-finds the vehicle with YOLOX near the scaled box
    and classifies the sharp crop; the alert photo becomes an HD close-up. Adds ~3 s to those alerts;
    at most one grab per camera per 2 s, and skipped when two jobs are already queued.
  - `web.show_zones` (live-page "Zones: on/off" button) only hides the drawn outlines.
- **Alerts:**
  - `alert_mode` is `objects` by default: a Telegram photo plus a natural sentence, then the clip.
  - The per-camera/per-type cooldown is `object_alert_cooldown`.
  - The user's `telegram_motion_alerts` was **off**; it was left off.
- **Telegram commands added:** `access` (remote-access guide) and `videos <when> [people|vehicles|animals] [camera]` (`sentinel/engine/queries.py`).
- **Web:** `/settings` covers status, cameras, detection, Telegram, recording, audio, remote access, password and help.
- **Replay tool idea:** run the detector and tracker over a sample of a day's clips to check accuracy before changing thresholds.
- **Known issues for the user:**
  - The Rear Door camera (.11) looks at carport rafters, with an orange object against the lens.
  - The Garage camera clock is about 33 min slow (enable NTP on the cameras; needs admin).
  - The Cinnamon desktop in the xrdp session idles at about 300% CPU.
- **Next ideas:** delivery-vehicle recognition (CLIP zero-shot on vehicle crops), running detection on HD frames, a web events list with thumbnails, and an LLM/VLM chat ("did anyone come by?").

## Older status (Qt desktop era, kept for history)

- **Desktop app** (PySide6 + OpenCV) — multi-session: each camera has its own `MotionDetector` and profile in `settings.json` → `camera_profiles`.
- **RTSP** — discovery, sub-stream preference, per-host logins, reconnect timer when streams drop.
- **Motion** — MOG2, contours, night-bug filter, motion-triggered recording, Telegram clip request path respects **recording enabled**.
- **Zone / line gating (fixed logic, Apr 2026)**  
  - **No zones, no lines:** any qualifying motion can trigger.  
  - **Zones only:** centroid must be inside a polygon (**no longer bypassed** by empty entry-lines returning “true”).  
  - **Lines only:** line-crossing logic only (entry lines empty no longer makes `_line_crossed_for_track` return true).  
  - **Both:** zone **OR** line crossing.  
  - Line crossing uses **segment intersection** with the drawn segment.  
- **Vertical camera view** — `MotionDetector` applies `cam_orientation` **inside** `detect_motion()` so zones/lines match the UI; duplicate rotate removed from `MainWindow.update_feed`.
- **Embedded MultiView** — `QStackedWidget` so grid gets full feed area; grid tiles **letterbox** (`KeepAspectRatio`); **single-click** tile = active camera for controls; **double-click** = single view; overlay mode draws **each** camera’s zones on its own tile; `_ensure_grid_tiles` avoids rebuilding the grid every frame.
- **Camera dropdown** — connected sessions stay listed after discovery returns empty; `_ensure_camera_selectable` on connect; reconnect starts when all sessions drop.
- **Audio** — Piper preferred; **WAV amplitude scaled** from volume slider before `aplay`; pyttsx3 fallback.
- **Telegram** — `urllib` HTTPS to `api.telegram.org` (needs **internet egress** from the host, not the camera LAN).
- **Logging** — `sentinel/logging_utils.py` → append JSONL to `sentinel_events.log` (gitignored `*.log`): connect, read errors, stream drop, reconnect, recording toggle, volume, etc.
- **Web viewer (May 2026)** — Optional Flask server in `sentinel/web/`: `FrameBroker` receives the same display frames as the UI from `MainWindow.update_feed` (no second RTSP decode). Routes: `/login` (bcrypt + session cookie + IP lockout/backoff), `/`, `/api/cameras`, `/stream?id=…` MJPEG (query param — camera ids are often RTSP URLs with slashes), `/snapshot.jpg?id=…`. Defaults (merged): `web.bind_host=127.0.0.1`, `web.port=8088`; **`0.0.0.0`** common for LAN phones; refuse bind inside `web.camera_subnet_block`. `python -m sentinel.web.set_password` sets credentials. Logs: `web_login_*`, `web_session_revoked`, `web_server_start`. Deps: **Flask**, **bcrypt** in `requirements.txt`. Lazy package `__getattr__` in `sentinel/web/__init__.py`. **Cloudflare Tunnel:** see checklist section below + **`installation-guide.md`**.
- **GPU / VAAPI** — user chose to **hold off** on hardware decode (see plan `cpu_vs_gpu_decode` in Cursor plans if present); stay on OpenCV CPU decode until revisited.

### Runtime log review (Apr 29, 2026 sample)

From `sentinel_events.log` on disk (one file sample; **not** multi-day unless the app ran continuously and the file was not rotated):

- **Errors:** `camera_read_error` (frame read failures) and `camera_stream_dropped` clustered around transient RTSP/H.264 issues on **192.168.0.10** and **192.168.0.11**; **`camera_reconnect_success`** appeared (no `camera_reconnect_failed` in sample).
- **Risk:** each JSON line can include the **full RTSP URL with embedded credentials** — treat `sentinel_events.log` like a secret; consider **redacting URLs** in a future logging change.

---

## Docs (keep in sync when behavior changes)

- `readme.md` — user-facing overview + tables  
- `installation-guide.md` — venv, Piper (`requirements-piper.txt`, `models/`, `aplay`), Telegram network note, **web viewer + Cloudflare Tunnel / Tailscale**, logging, tests  
- `project-structure.md` — paths, new modules (`logging_utils.py`, `sentinel/web/`, `tests/`)  
- `handoff_summary.md` — this file  

---

## Known environment / safety

- **`settings.json`** — gitignored; may contain RTSP passwords and Telegram token — never commit.  
- **`sentinel_events.log`** — gitignored (`*.log`). **May contain RTSP passwords** in `camera_id` / URL fields — restrict file permissions and avoid sharing.  
- **GPU** — not used yet; decode + MOG2 are CPU-bound unless a future GStreamer/VAAPI or OpenCV-CUDA path is added (**deferred by user**).  
- **Git on agent host:** this environment often has **no `git` binary** — the assistant **cannot** run `git fetch`/`push` here. On your machine run:
  ```bash
  cd /path/to/sentinel-home
  git fetch origin && git status -sb
  git log origin/main..HEAD --oneline   # unpushed commits
  git log HEAD..origin/main --oneline   # commits on remote you lack
  ```
  Then `git add -A && git commit … && git push origin main` as needed.

---

## Piper quick reference

| Step | Action |
|------|--------|
| 1 | `pip install -r requirements-piper.txt` |
| 2 | Linux: `sudo apt install -y alsa-utils` → `aplay` |
| 3 | Add `models/<voice>.onnx` [+ `.json`] |
| 4 | Run app; console: `Piper TTS initialised with voice: …` |

Implementation: `sentinel/audio/alert_system.py` (`_apply_volume_to_wav`, `_speak_piper`).

---

## Cloudflare Tunnel (remote HTTPS) — operator checklist

**Full prose + example `config.yml`:** [`installation-guide.md`](installation-guide.md) → section **“Remote HTTPS with Cloudflare Tunnel (typical)”**.

Nothing in this repo installs or configures `cloudflared`; the user (or next chat) does that on the **Sentinel PC**. Summary:

| Step | Action |
|------|--------|
| 1 | **Web viewer OK locally:** Sentinel running, `web.enabled` true, password set (`python -m sentinel.web.set_password`), reachable at `http://127.0.0.1:8088` (or whatever `web.port` is). |
| 2 | **Install `cloudflared`** on the same PC (OS package or `.deb` from Cloudflare’s GitHub releases). |
| 3 | **`cloudflared tunnel login`** — browser; choose the Cloudflare **zone** that holds the domain (e.g. `example.com`). |
| 4 | **`cloudflared tunnel create <name>`** — creates tunnel; note UUID / credentials path under `~/.cloudflared/`. |
| 5 | **`cloudflared tunnel route dns <name> cams.example.com`** — creates DNS pointing at the tunnel (replace hostname). |
| 6 | **`~/.cloudflared/config.yml`** — map `hostname: cams.example.com` → **`service: http://127.0.0.1:8088`** (must match `settings.json` `web.port`; see install guide for full YAML including `http_status:404` fallback). |
| 7 | **Run as service:** `sudo cloudflared service install` then `sudo systemctl enable --now cloudflared` (or distro equivalent). |
| 8 | **Optional hardening:** Cloudflare **Zero Trust → Access** policy on `cams.example.com` (email OTP / Google) *before* Sentinel’s own login. |

**Bind host vs tunnel**

- **Tunnel-only (recommended for remote):** `web.bind_host` can stay **`127.0.0.1`** — `cloudflared` connects to localhost; cameras stay isolated; no extra LAN exposure.
- **Same Wi‑Fi phones without tunnel:** set `web.bind_host` to **`0.0.0.0`** or the PC’s LAN IP (not inside `web.camera_subnet_block`). Firewall may need **`8088/tcp`** allowed.

**Not done automatically**

- DNS hostname choice (`cams.*` vs other subdomain).
- Cloudflare Access policies.
- Troubleshooting: `journalctl -u cloudflared`, Tunnel **Metrics** in Zero Trust dashboard, verify ingress URL matches loopback + port.

---

## Suggested next-chat prompts

- “Read `handoff_summary.md`. Add GStreamer + VAAPI RTSP decode behind a feature flag and document deps in `installation-guide.md`.”  
- “Read `handoff_summary.md`. Add a UI button to tail the last N lines of `sentinel_events.log`.”  
- “Read `handoff_summary.md`. Redact RTSP credentials in `sentinel_events.log` (log host + path only).”  
- “Read `handoff_summary.md`. Add web viewer recording playback or CSRF tokens on logout POST.”  
- “Read `handoff_summary.md`. Help debug Cloudflare Tunnel → Sentinel web viewer (ingress, Access, MJPEG).”

---

## Next session checklist

- [ ] Read this file first.  
- [ ] Confirm user goal.  
- [ ] Run smoke checks before large refactors.  
- [ ] Update this file before ending: changes, validation, blockers, next steps.

---

## Session update template

### Date/Time  
### Goal worked on  
### Changes made  
### Validation performed  
### Open issues / risks  
### Next recommended step  

---

## Latest session update

### Date/Time

- Monday, May 4, 2026 (UTC-4) — optional HTTP web viewer + auth + docs

### Goal worked on

- Live multi-camera **web page** (MJPEG grid) with **login**, **bcrypt** password, **session cookies**, **IP lockout** with exponential backoff, **bind-host guard** so the server never listens on the camera-only subnet, **Cloudflare Tunnel** / Tailscale documentation. Isolated camera LAN unchanged.

### Changes made

- New package **`sentinel/web/`**: `frame_broker.py`, `auth.py`, `bind_util.py`, `server.py`, `set_password.py`, `templates/login.html`, `templates/grid.html`; lazy **`__getattr__`** in `sentinel/web/__init__.py` so lightweight imports skip NumPy.  
- **`sentinel/app.py`**: when `web.enabled` and `web.auth.password_hash` set, start `FrameBroker` + `WebServer` thread; pass broker into `MainWindow`.  
- **`sentinel/ui/main_window.py`**: `update_feed` publishes to broker (`web.published_frame` raw vs overlay path); `disconnect_camera` / stream-drop / empty sessions **unpublish** / **clear**.  
- **`sentinel/settings/manager.py`**: `WEB_VIEWER_DEFAULTS`, `ensure_web_defaults()`, `save_settings(..., force=)` for CLI. **`sentinel/__init__.py`** added (package marker).  
- **`requirements.txt`**: `Flask>=3.0.0`, `bcrypt>=4.0.0`.  
- **Tests:** `tests/test_frame_broker.py`, `tests/test_web_auth.py`, `tests/test_web_bind.py`.  
- **Docs:** `readme.md`, `installation-guide.md` (web + Tunnel + Tailscale), `project-structure.md`, this handoff.  
- **Handoff:** Added **“Cloudflare Tunnel — operator checklist”** section (table + bind-host guidance); install guide remains canonical for YAML/commands.

### Validation performed

- **`tests.test_web_bind`**: passes with system Python (no venv).  
- **Full suite** (`unittest discover`): not run in agent — image lacked `python3-venv` / preinstalled NumPy; user should run `pip install -r requirements.txt` in a venv and `python3 -m unittest discover -s tests -p 'test_*.py'`.

### Open issues / risks

- **Logout** is POST without CSRF token (home / tunnel use case; harden if exposed broadly).  
- **`web_login_*` events** may include `username_attempted` — low sensitivity; still local log.  
- Cloudflare free tier **video proxying** policy: keep `max_fps` / `jpeg_quality` modest or prefer Tailscale.

### Next recommended step

- **Cloudflare Tunnel:** follow the **“Cloudflare Tunnel (remote HTTPS) — operator checklist”** table above; paste-ready **`config.yml`** and commands live in **`installation-guide.md`** (Tunnel subsection). Prefer **`web.bind_host: "127.0.0.1"`** when using tunnel-only remote access.  
- Optional: run full unit tests locally; commit and push (agent host may lack `git`).
