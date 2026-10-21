# Sentinel Home - Motion Detection

Sentinel Home is a **Python + PySide6** desktop app for local camera monitoring: motion detection, optional zones/lines, recording, speech alerts, and Telegram notifications.

## Docs index

| File | Purpose |
|------|---------|
| `readme.md` | This file — features, behavior, quick start |
| `installation-guide.md` | Step-by-step install, Piper, Telegram, troubleshooting |
| `project-structure.md` | Repository layout and module roles |
| `handoff_summary.md` | Maintainer / agent handoff notes |

## Key features

- **Motion detection** — OpenCV MOG2 background subtraction.  
- **Cameras** — USB (V4L2) and RTSP; discovery with optional **sub-stream** preference; per-host credentials; auto-reconnect.  
- **Zones and entry lines** — Normalized coordinates (0–1) stored per camera profile; gating modes (see below).  
- **Recording** — Motion-triggered clips (duration from constants); optional Telegram **clip** command; global + per-profile **recording enabled** toggle.  
- **Multi-camera** — Several RTSP/USB sessions at once; **embedded grid** in the main window.  
- **Display** — Single view or grid; **overlay** vs **raw**; **vertical** camera view (detection matches drawn zones).  
- **Audio alerts** — **Piper** neural TTS when `piper-tts` + `models/*.onnx` are present; **`aplay`** on Linux; **pyttsx3** fallback; **volume** and **speech speed** sliders (Piper volume scales WAV before playback).  
- **Telegram** — HTTPS to `api.telegram.org`; multiple chat IDs; cooldown; optional motion clip uploads; **pause / resume / status / clip / help**; **enable/disable** phrases for recording, motion alerts, clips, night-bug filter, and auto-reconnect (see `installation-guide.md`).  
- **Night bug filter** — Scheduled suppression of small fast edge-near motion.  
- **Structured logging** — JSON lines appended to `sentinel_events.log` (local, gitignored via `*.log`).  
- **Web viewer (optional)** — Live multi-camera MJPEG grid over HTTP with login, bcrypt password, session cookies, and IP-based lockout after failed attempts. Binds to `127.0.0.1` by default (safe with Cloudflare Tunnel). See **`installation-guide.md`**.  
- **UI** — Collapsible sections, fullscreen (`F11` / `Esc`), persisted window state.

## Requirements

- Python **3.10+** recommended  
- **Linux** (primary), macOS, or Windows  
- Cameras: local and/or **RTSP**  
- **Piper (optional):** `alsa-utils` (`aplay`) on Linux — see `installation-guide.md`  
- **Telegram (optional):** host must reach the **public internet** for the Bot API (not the same as an isolated camera VLAN)

## Quick install

```bash
git clone https://github.com/billerl/sentinel-home.git
cd sentinel-home
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
pip install -r requirements-piper.txt   # optional, recommended
python main.py
```

Place Piper voice files under `models/` (`.onnx` + optional `.onnx.json`). Details: **`installation-guide.md`**.

## Run

```bash
python main.py
```

## Web viewer (optional)

1. Set credentials: `python -m sentinel.web.set_password` (from repo root, venv active).  
2. In `settings.json`, set `"web": { "enabled": true, ... }` (defaults are merged on startup; see `installation-guide.md`).  
3. Restart the app. Open `http://127.0.0.1:8088/` (or your `web.bind_host` / `web.port`), sign in, and view the live grid.  
4. For remote HTTPS access, use **Cloudflare Tunnel** (or Tailscale) as documented in **`installation-guide.md`** — cameras can stay on an isolated LAN; the app only listens on the address you configure, not on the camera subnet.

## First-time setup

1. **Search Cameras** — refresh the list.  
2. Select a source; use **Connect** / **Disconnect** on the toggle button.  
3. Optional: **Prefer sub-stream**, **Feed FPS**, Telegram, zones/lines.

## Detection and trigger behavior

Rules implemented in `sentinel/detection/motion_detector.py`:

| Configuration | Motion triggers when |
|----------------|----------------------|
| **No zones, no lines** | Any qualifying motion blob (after night-bug filter, min area, etc.) |
| **Zones only** | Centroid lies **inside** at least one active polygon |
| **Lines only** | A track **crosses** a configured entry segment (direction rules apply) |
| **Zones + lines** | **Inside a zone OR** valid **line crossing** |

Entry line mode requires the motion path segment to **intersect** the drawn line segment (not just proximity to the infinite line).

**Vertical view:** the detector uses the same **90° clockwise** frame as the UI so stored normalized zones/lines match what you draw.

## Embedded MultiView (grid)

- **Open MultiView** switches the main feed area to a **two-up grid** (when two or more cameras are connected).  
- **Single-click** a tile: that camera becomes **active** (control panel + pending zone draw apply to it); **all** cameras can show **zone/line overlays** in overlay mode.  
- **Double-click** a tile: **single-camera** view for that feed.  
- **Return to Grid** / **Single View** toggles layout.  
- Tiles use a **stacked layout** so the grid gets the full feed area; scaling uses **letterboxed** fit so aspect ratio stays correct (small black bars if cell aspect ≠ video).

Standalone **`MultiViewWindow`** exists in `multiview_window.py` for optional use; the primary workflow is the embedded grid in `main_window.py`.

## Recording

- Clips go under `recorded_videos/`.  
- **Enable Video Recording** is honored for motion and for Telegram clip requests.  
- Cooldown is configurable per camera profile.

## Telegram setup

See **`installation-guide.md`** for token, chat ID, and test message steps.

**Network:** Telegram uses **outbound HTTPS** from the PC running Sentinel to Telegram’s servers — not your camera-only subnet.

## Structured logging

When events occur (camera errors, reconnects, recording toggle, volume changes, etc.), the app may append JSON objects to **`sentinel_events.log`**. The file is ignored by git (`*.log`). Use it when diagnosing stream stability or control bugs.

**Privacy:** log lines may contain **RTSP credentials** as part of URL fields—treat the file like a secret.

## Troubleshooting (short)

| Symptom | Things to try |
|--------|----------------|
| RTSP decode / `h264` errors | Sub-stream, lower bitrate/FPS, wired path, disable SVC |
| Motion outside drawn zone | Check line-only vs zone-only rules; vertical vs horizontal view |
| Piper silent | `requirements-piper.txt`, `models/`, `which aplay` |
| High CPU | Lower **Feed FPS**, fewer streams, lower camera resolution |
| Multiple Python processes | `pgrep -af main.py` — keep a single intentional run |

More detail: **`installation-guide.md`**.

## Settings file

Runtime state is stored in **`settings.json`** (gitignored). Includes camera sources, profiles (zones, lines, sensitivity, recording), Telegram fields, UI expansion flags, etc.

## Project layout (abbreviated)

```
sentinel-home/
  main.py
  requirements.txt
  requirements-piper.txt
  readme.md
  installation-guide.md
  models/                 # Piper ONNX voices (local)
  sentinel/               # application package
  tests/                  # unit tests
```

Full tree: **`project-structure.md`**.

## License

MIT (see `LICENSE` if present in the repository).
