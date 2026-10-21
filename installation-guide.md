# Installation Guide for Sentinel Home

This guide covers practical setup for:

- Local USB and RTSP network cameras  
- Motion detection, zones/lines, and recording  
- Optional Piper TTS (recommended on Linux) and pyttsx3 fallback  
- Optional Telegram alerts (requires **outbound internet** HTTPS to `api.telegram.org`)  
- Structured runtime logging (`sentinel_events.log`, gitignored)

For feature overview and behavior details, see `readme.md`.

## Prerequisites

- **Python 3.10+** recommended  
- **Git** (to clone the repository) — install with your OS package manager if missing  
- A **camera** (local V4L2 webcam and/or RTSP URL)  
- **Linux audio** (for Piper): ALSA `aplay` — package `alsa-utils` on Debian/Ubuntu  
- **Telegram** (optional): a bot token and chat ID(s); the **PC running Sentinel** must reach the public internet for `https://api.telegram.org` (independent of whether cameras are on an isolated LAN)

## Recommended install (virtual environment)

1. **Clone** (replace URL with your fork if needed):

   ```bash
   git clone https://github.com/billerl/sentinel-home.git
   cd sentinel-home
   ```

2. **Create and activate a venv**:

   ```bash
   python3 -m venv venv
   source venv/bin/activate
   ```

   Windows (PowerShell):

   ```powershell
   python -m venv venv
   .\venv\Scripts\Activate.ps1
   ```

3. **Install core dependencies**:

   ```bash
   pip install --upgrade pip
   pip install -r requirements.txt
   ```

4. **Optional — Piper TTS** (neural voices, used automatically when models are present):

   ```bash
   pip install -r requirements-piper.txt
   ```

5. **Run**:

   ```bash
   python main.py
   ```

## Piper TTS (optional, recommended on Linux)

Sentinel selects Piper when the `piper-tts` package imports cleanly **and** at least one `models/*.onnx` file exists (with optional matching `*.onnx.json`). Otherwise it uses **pyttsx3**.

### 1) Install Python packages

Inside the activated venv:

```bash
pip install -r requirements.txt
pip install -r requirements-piper.txt
```

### 2) Install `aplay` (Linux)

Piper output is played by piping WAV bytes to **`aplay`**:

```bash
sudo apt update
sudo apt install -y alsa-utils
which aplay   # should print a path
```

### 3) Add voice models

```bash
mkdir -p models
```

Download a **matching pair** from the [Piper voices](https://github.com/rhasspy/piper/blob/master/VOICES.md) project or your voice provider, for example:

- `models/en_US-lessac-medium.onnx`  
- `models/en_US-lessac-medium.onnx.json`  

The basename (without `.onnx`) becomes the **voice id** in the UI.

### 4) Verify Piper at startup

Run the app and watch the console:

- Success: `Piper TTS initialised with voice: <voice_id>`  
- Fallback: `Piper TTS not installed...` or `Error loading Piper model...` then pyttsx3

### 5) Volume and speed sliders

- **Speech speed** is applied inside Piper via synthesis length scale.  
- **Volume** (0–10) is applied to Piper output by scaling WAV amplitude before `aplay` (see `sentinel/audio/alert_system.py`).  
- pyttsx3 path uses engine `volume` / `rate` properties when Piper is not active.

## Optional: unit tests

From repo root with venv activated:

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
```

Requires `numpy` (listed in `requirements.txt`).

## First run checklist

1. Click **Search Cameras**.  
2. Pick a source from the dropdown (or add RTSP host / resolve pending camera).  
3. Toggle the feed with **Camera Offline** / **Disconnect Selected**.  
4. For IP cameras, prefer **sub-stream** where possible (checkbox).  
5. Tune **Detection Size** / **Sensitivity** and optional **Feed FPS** (lower = less CPU).  
6. Draw **active zones** and/or **entry lines** as needed (see `readme.md` for gating rules).

## Web viewer (optional, live MJPEG grid)

The desktop app can expose a small **Flask** server with a login page and a multi-camera live grid. Frames are the same ones already decoded for the UI (no extra RTSP load). Defaults listen on **`127.0.0.1:8088`** only, which pairs well with **Cloudflare Tunnel** so you do not open inbound router ports.

### 1) Install dependencies

Core `requirements.txt` already includes **Flask** and **bcrypt**. After `pip install -r requirements.txt`, continue below.

### 2) Set username and password

From the repository root (venv activated):

```bash
python -m sentinel.web.set_password
```

This writes `web.auth.username` and `web.auth.password_hash` into **`settings.json`** (gitignored). Passwords are **bcrypt**-hashed; the plaintext is never stored.

### 3) Enable the web server

Edit **`settings.json`** (or rely on merged defaults after one app run) and set at least:

```json
"web": {
  "enabled": true,
  "bind_host": "127.0.0.1",
  "port": 8088,
  "camera_subnet_block": "192.168.0.0/24"
}
```

- **`bind_host`**: `127.0.0.1` is recommended when using **Cloudflare Tunnel** (`cloudflared` on the same PC talks to Flask over loopback). To allow phones on **home Wi‑Fi** without going through Cloudflare, set `bind_host` to this machine’s **LAN IP** (must **not** be an address inside `camera_subnet_block`, or startup will refuse — cameras stay isolated).
- **`camera_subnet_block`**: CIDR used only to **block accidental bind** on your camera-only NIC (adjust if your cameras use a different subnet).
- **`web.published_frame`**: `"overlay"` matches the on-screen overlays (zones/lines) when overlay mode is on; `"raw"` publishes the detector’s raw frame even if the UI shows overlays.

Restart **`python main.py`**. You should see `Web viewer: http://127.0.0.1:8088/` in the console when startup succeeds.

### 4) Auth and lockout

Login uses **session cookies** (not HTTP Basic). After **`web.auth.max_attempts`** failed logins from the same client IP within **`attempt_window_minutes`**, the client is locked out; lockout length grows with **`lockout_backoff_factor`** up to **`lockout_max_minutes`**. Successful login clears the lockout for that IP. Set **`web.auth.bind_session_to_ip`** to `false` only if a trusted reverse proxy changes client IPs between requests.

Events are logged to **`sentinel_events.log`** as `web_login_success`, `web_login_failed`, `web_login_locked`, `web_session_revoked`, `web_server_start`, etc.

### 5) Remote HTTPS with Cloudflare Tunnel (typical)

Same pattern as exposing Home Assistant at `ha.example.com`:

1. Install `cloudflared` on this PC and run `cloudflared tunnel login` (pick the Cloudflare zone that holds your domain).
2. Create a tunnel and a DNS record, e.g. `cams.example.com` → that tunnel.
3. In `~/.cloudflared/config.yml`, map the hostname to the local viewer, for example:

   ```yaml
   ingress:
     - hostname: cams.example.com
       service: http://127.0.0.1:8088
     - service: http_status:404
   ```

4. Run `cloudflared` as a service. Browse `https://cams.example.com` and sign in.

Optional: add **Cloudflare Access** (Zero Trust) in front of that hostname so only your accounts can reach the origin before Flask login.

**Note:** Cloudflare’s free terms restrict some “video” proxying; keep **`web.max_fps`** and **`web.jpeg_quality`** modest for personal use, or use **Tailscale** instead of a public hostname if you prefer.

### 6) Tailscale (alternative)

Install Tailscale on the PC and your phone, then browse `http://<tailscale-ip>:8088` (or set `web.bind_host` to the `tailscale0` address). No Cloudflare account required.

---

## Telegram setup (optional)

1. Create a bot with `@BotFather`, copy the token.  
2. Send any message to the bot (or use a group where the bot is allowed).  
3. Open `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` and read `message.chat.id`.  
4. In Sentinel: enable Telegram, paste token and chat ID(s) (comma-separated), set cooldown, **Send Telegram Test**.

### Telegram commands

- `pause <minutes>` — pause alerts to Telegram for this chat (does not stop recording).  
- `resume` — unpause.  
- `status` — pause state plus recording / Telegram / night-bug / reconnect summary.  
- `clip` — short clip per connected camera (uploaded even if “motion clip uploads” is off).  
- `help` — full command list and examples.  

**Enable / disable** (case-insensitive; combine with `and` or commas):  
`recording` (motion clips to disk), `messages` (Telegram motion alerts), `clips` (attach motion recordings to Telegram), `night bug` or `night bug filter`, `reconnect` or `auto reconnect`.

Examples: `Enable recording and messages`, `Disable clips`, `Enable night bug filter`.

Unknown input returns the same help text as `help`.

**Network note:** Telegram uses **HTTPS to Telegram’s servers**, not your camera subnet. The machine running Sentinel needs a normal **internet egress** route (firewall/NAT/proxy aware).

## Structured logging

On run, the app may append JSON lines to **`sentinel_events.log`** in the project directory (camera connect/disconnect, stream errors, reconnect attempts, recording toggle, volume changes, etc.).

- The file is covered by `.gitignore` (`*.log`) so it is not committed.  
- Useful when diagnosing RTSP drops or UI/control issues.  
- **Privacy:** some fields may echo the **full RTSP URL** (including embedded passwords). Restrict permissions on this file and do not paste raw lines into public chats.

## Common troubleshooting

### No camera / unstable RTSP

- Use **sub-stream**, lower bitrate/resolution/FPS on the camera.  
- Prefer **wired** Ethernet to the camera path.  
- Disable **SVC** / exotic smart codecs if decode errors appear in the console.  
- Increase **Retry (sec)** or enable **Auto reconnect**.

### Motion outside the drawn zone still alerts

- Confirm you are not using **entry lines** without zones (line-only mode triggers on crossing).  
- **Vertical** camera view: zones must be drawn in vertical view; detection uses the same orientation as the UI.

### Piper silent or not used

- `pip show piper-tts`  
- Models: `ls models/*.onnx` and matching `.json` where applicable  
- `which aplay`  
- Check console for Piper init errors.

### High CPU with two RTSP streams

- Lower **Feed FPS** in the UI.  
- Reduce camera resolution/bitrate.  
- Ensure only **one** `python main.py` is running (`pgrep -af main.py`).

### Many `python` rows in `htop`

- Usually multiple **separate** `main.py` launches or old processes; use `pgrep -af main.py` and stop extras.  
- `htop` display modes can occasionally confuse threads vs processes — verify with `ps`/`pgrep`.

## Settings file

`settings.json` is created at runtime and **must not be committed** (tokens, RTSP passwords). It is listed in `.gitignore`.
