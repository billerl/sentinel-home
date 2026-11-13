# Installation Guide for Sentinel Home

The **[readme](readme.md#install-about-15-minutes)** has the short install: about 15 minutes from nothing to
cameras on the web page. This guide has the rest: running it all the time, using it away from home,
Telegram, audio and voice, the backup drive, and troubleshooting.

Sentinel runs on **Linux** (built and used on Ubuntu; other Debian-style systems should work the same).
It has no window of its own: you use it through its web page. Python 3.10 or newer.

> Using an AI assistant to help? Point it at [`AGENTS.md`](AGENTS.md) first.

## Install

Follow steps 1-6 of the [readme](readme.md#install-about-15-minutes). In short: system packages
(`git curl python3-venv alsa-utils xvfb`), clone, a virtual environment with `requirements.txt` and
`requirements-piper.txt`, the object detector model, `venv/bin/python -m sentinel.web.set_password`, then
`venv/bin/python main.py` and open `http://127.0.0.1:8088/`. What you see when it starts:

- `Web viewer: http://127.0.0.1:8088/`: the web page is up.
- `Web page: no login set yet`: stop Sentinel, run `set_password`, start it again.
- `No cameras yet`: add one on the web page (Settings → Cameras → Add a camera).

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

## Always-on setup (how Sentinel is normally run)

Sentinel is meant to run all the time on a small Linux PC, with everything set up from its web page.
Nothing in the code is tied to one household: your cameras, logins, keys, family and cars all live in
files that are created on first run and are git-ignored (`settings.json`, `camera_admin.json`,
`recorded_videos/*.db`, `.spotifyd/`, `.web_sessions.json`).

1. **Install** as above, plus the models from the readme's *Quick install* (at least the object detector).
2. **Web login:** `venv/bin/python -m sentinel.web.set_password` (asks for a username and password).
3. **Run as a service** so it starts at boot and restarts itself. Create `/etc/systemd/system/sentinel.service`
   (replace `youruser` and the path), then `sudo systemctl enable --now sentinel`:

   ```ini
   [Unit]
   Description=Sentinel Home
   After=network.target

   [Service]
   User=youruser
   WorkingDirectory=/home/youruser/sentinel-home
   ExecStart=/usr/bin/xvfb-run -a /home/youruser/sentinel-home/venv/bin/python main.py
   Restart=always
   RestartSec=10
   Environment="PYTHONUNBUFFERED=1"

   [Install]
   WantedBy=multi-user.target
   ```

   `xvfb-run` (package `xvfb`) gives the app a virtual screen, so no monitor or desktop session is needed.
   For the speaker and microphone, add the user to the `audio` group; for detection on an Intel GPU, to
   `render` (`sudo usermod -aG audio,render youruser`).
4. **Open the web page** (`http://127.0.0.1:8088` on the PC, or remotely, below) → **Settings**, and set up:
   - **Cameras** → *Add a camera*: brand, address, the camera's own username and password.
   - **Telegram** (optional): bot token and chat ID (see *Telegram setup* below).
   - **Ask Claude** (optional, under Telegram): questions Sentinel doesn't understand go to Claude. It uses
     the [Claude Code](https://claude.com/claude-code) command-line app signed in to *your* Claude account
     on this PC (install it, run `claude` once as the service user and sign in). No API key is stored in
     Sentinel; set a daily limit there. Path: `assistant_claude_path` if `claude` isn't on the PATH.
   - **Audio alerts / voice assistant** (optional): a speaker and a microphone on the PC. The wake word is
     "Hey Jarvis" (bundled with openWakeWord); a custom one is a trained openWakeWord model in
     `models/wakeword/<name>.onnx`, chosen with `voice_wake_model`. Needs the Whisper model from the readme.
   - **Spotify** (optional, Premium): create a free app at developer.spotify.com with the redirect URI
     `http://127.0.0.1:8888/callback`, paste its Client ID, press *Connect Spotify*.
   - **Weather** (optional): your town or ZIP code (free Open-Meteo, no key).
5. **Family and cars** (optional): on the **Family** page add people and send each a setup link for the
   free OwnTracks phone app (needs the remote address below); add places like Home. On the **Vehicles**
   page name your cars from the photos Sentinel collects. Whose car is whose, and nicknames ("Kate",
   "Mom"), are what the alerts, Telegram answers and voice assistant then use.
6. **Remote access:** a Cloudflare Tunnel to your own domain or Tailscale (sections below). Put the
   public address under **Settings → Remote access**, so setup links and QR codes point to it.

## Optional: unit tests

From repo root with venv activated:

```bash
PYTHONPATH=.:tests venv/bin/python -m unittest discover -s tests
```

They don't need cameras, models or a network, and don't touch your settings or recordings.

## The web page

It's on by default at `http://127.0.0.1:8088/`, reachable **only from this PC** until you choose otherwise
(below). First steps after signing in:

1. **Settings → Cameras → Add a camera:** brand, address, the camera's own login. **Test connection** shows
   a picture or says what's wrong. Prefer the camera's low-resolution "substream" for detection (the brand
   choice fills it in); the full-quality stream is recorded too.
2. **Zones:** draw where each camera should watch, or a line something has to cross.
3. **Settings → Detection / Recording:** sensitivity, what to alert on, how long to keep clips.
4. **Help** (in Settings) explains every feature in plain English.

Web settings live under `"web"` in `settings.json` (stop Sentinel before editing it by hand):

```json
"web": {
  "enabled": true,
  "bind_host": "127.0.0.1",
  "port": 8088,
  "camera_subnet_block": "192.168.0.0/24"
}
```

- **`bind_host`**: `127.0.0.1` (this PC only) is right when using a **Cloudflare Tunnel**, which runs on the same PC. To also allow phones and TVs on your **home Wi-Fi** directly, add this PC's home-network address after a comma, e.g. `"127.0.0.1,192.168.1.20"` (give the PC a fixed address in your router; none may be inside `camera_subnet_block`, or startup refuses, so cameras stay isolated). With a home address listed, a TV in the house (Fire TV kiosk, smart-TV browser) that opens the tunnel's public address is sent to the home address automatically, still signed in, so its video stays inside the house and keeps working when the internet is down. It's recognised by its IPv6 address being in this PC's own /64; phones are never moved (they leave the house).
- **`camera_subnet_block`**: the camera network (if your cameras are on their own network card), so the web page is never opened up on it by mistake.
- **`web.published_frame`**: `"overlay"` shows zones and lines on the live picture; `"raw"` doesn't.

### Login and lockout

Login uses **session cookies** (not HTTP Basic). After **`web.auth.max_attempts`** failed logins from the same client IP within **`attempt_window_minutes`**, the client is locked out; lockout length grows with **`lockout_backoff_factor`** up to **`lockout_max_minutes`**. Successful login clears the lockout for that IP. Set **`web.auth.bind_session_to_ip`** to `false` only if a trusted reverse proxy changes client IPs between requests.

Events are logged to **`sentinel_events.log`** as `web_login_success`, `web_login_failed`, `web_login_locked`, `web_session_revoked`, `web_server_start`, etc.

### Away from home: Cloudflare Tunnel (your own domain)

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

### Away from home: Tailscale (simpler, no domain needed)

Install Tailscale on the PC and your phone, then browse `http://<tailscale-ip>:8088` (or set `web.bind_host` to the `tailscale0` address). No Cloudflare account required.

---

## Backup drive (optional)

Sentinel can back itself up every night to a USB drive plugged into the PC (Settings → Backup). It only writes
inside a `Sentinel Backup` folder on the drive, so the drive can hold other files too. Because Sentinel runs as a
background service, Linux won't let it open USB drives until you allow it once:

```bash
echo 'polkit.addRule(function(a, s) { if (s.user == "'$USER'" && (a.id == "org.freedesktop.udisks2.filesystem-mount" || a.id == "org.freedesktop.udisks2.filesystem-mount-other-seat")) return polkit.Result.YES; });' | sudo tee /etc/polkit-1/rules.d/50-sentinel-usb.rules
```

Run it as the user Sentinel runs as. Then plug the drive in and tap **Use this drive for backups**.

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
- **Privacy:** passwords are removed before anything is written, but the log still holds camera addresses, names and times. Don't paste it into public chats.

## Common troubleshooting

### No camera / unstable RTSP

- **Settings → Cameras → Test connection** says what's wrong (wrong password, wrong stream path, no answer).
- Use the camera's **substream** for detection; lower its bitrate or frame rate in the camera's own settings.
- Prefer **wired** Ethernet for cameras. Turn off "smart codec"/SVC/H.265+ in the camera if the picture breaks up.
- Cameras accept only a few streams at once: close the camera's own app or other recorders while testing.
- Sentinel reconnects on its own; **Reconnect** under Settings → Cameras forces it.

### Motion outside the drawn zone still alerts

- With **lines only** (no zones), crossing a line anywhere along it counts.
- A zone counts the moving thing's **bottom edge** (its feet or tyres): draw zones on the ground, not around walls.

### Piper silent or not used

- `pip show piper-tts`  
- Models: `ls models/*.onnx` and matching `.json` where applicable  
- `which aplay`  
- Check console for Piper init errors.

### High CPU with two RTSP streams

- Use the cameras' substreams for detection, and lower their frame rate (10 fps is plenty).
- With an Intel graphics chip, detection runs there (Settings → Status shows the device; the user needs the `render` group).
- Make sure only **one** Sentinel is running (`pgrep -af main.py`).

### Many `python` rows in `htop`

- Usually multiple **separate** `main.py` launches or old processes; use `pgrep -af main.py` and stop extras.  
- `htop` display modes can occasionally confuse threads vs processes — verify with `ps`/`pgrep`.

## Settings file

`settings.json` is created at runtime and **must not be committed** (tokens, RTSP passwords). It is listed in `.gitignore`.
