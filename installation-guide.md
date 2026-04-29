# Installation Guide for Sentinel Home

This guide focuses on practical setup for current Sentinel Home features:
- local + RTSP cameras
- motion detection/recording
- Telegram alerts and chat commands

For full feature documentation, see `readme.md`.

## Prerequisites

- Python 3.10+ recommended
- Git
- A camera source:
  - local webcam, and/or
  - RTSP network camera
- Telegram account (only if using Telegram notifications)

## Recommended Install (Virtual Environment)

1. Clone:
   ```bash
   git clone <your-repo-url>
   cd sentinel
   ```

2. Create and activate venv:
   ```bash
   python3 -m venv venv
   source venv/bin/activate
   ```
   Windows (PowerShell):
   ```powershell
   python -m venv venv
   .\venv\Scripts\Activate.ps1
   ```

3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

4. Run:
   ```bash
   python main.py
   ```

## First Run Checklist

1. Click `Search Cameras`.
2. Select camera source.
3. Toggle feed on with `Camera Offline` button.
4. Optionally enable `Prefer sub-stream for network cameras`.
5. Tune `Detection Size` / `Sensitivity`.
6. Draw zones and entry line(s) as needed.

## Telegram Setup (Optional)

1. Create bot with `@BotFather` and copy token.
2. Send a message to bot (or bot-enabled group).
3. Open:
   - `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates`
4. Copy `message.chat.id`.
5. In Sentinel Telegram panel:
   - enable Telegram
   - set token
   - set one or more chat IDs (comma-separated)
   - set cooldown
   - send test message

## Telegram Commands (Current)

- `pause <minutes>`
- `resume`
- `status`
- `clip`
- `help`

Unknown commands return a help message with valid commands.

## Piper TTS (Optional but Recommended)

Sentinel already includes Piper integration in code (`sentinel/audio/alert_system.py`).
No code changes are required to "turn on" Piper; it activates when:

1. the Python package is installed, and
2. at least one model exists in `models/*.onnx` (with matching `.onnx.json`).

### 1) Install Piper package

Inside the project venv:

```bash
pip install -r requirements-piper.txt
```

### 2) Ensure local audio playback tool exists (Linux)

Sentinel plays Piper output through `aplay`, so install ALSA utils if needed:

```bash
sudo apt update
sudo apt install -y alsa-utils
```

### 3) Download a Piper voice model

Create model directory and add a voice pair from the official Piper voices repo:

```bash
mkdir -p models
```

Download both files with the same basename, for example:

- `models/en_US-lessac-medium.onnx`
- `models/en_US-lessac-medium.onnx.json`

### 4) Run Sentinel

```bash
python main.py
```

If Piper loads successfully, startup logs include:
- `Piper TTS initialised with voice: <voice_id>`

If not installed or no model is present, Sentinel falls back to `pyttsx3`.

## Common Troubleshooting

### No camera frames / unstable RTSP

- Prefer sub-stream.
- Lower camera bitrate/resolution/fps.
- Keep H.264+ / smart codec / SVC disabled.
- Use wired network where possible.

### Too many false alerts at night

- Enable `Night bug filter`.
- Set schedule hours (example: 20 -> 6).
- Refine active zones and entry lines.

### Too many Telegram messages

- Increase Telegram cooldown.
- Increase motion cooldown if recordings are too frequent.

### TTS issues

Sentinel prefers Piper if available; otherwise uses `pyttsx3`.
On Linux, audio output tools and voice packages may be required depending on your distro.
For Piper specifically, verify:
- `pip show piper-tts`
- model files exist in `models/` as `.onnx` + `.onnx.json` pairs
- `aplay` exists (`which aplay`)
