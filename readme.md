# Sentinel Home - Motion Detection

Sentinel Home is a Python + PySide6 desktop app for local camera monitoring with motion detection, recording, and Telegram notifications.

## Docs Index

- `readme.md` - Feature overview, setup, usage, and operations
- `installation-guide.md` - Step-by-step installation and troubleshooting
- `project-structure.md` - Codebase layout and module responsibilities

## Key Features

- Real-time motion detection using OpenCV MOG2.
- Local USB and network RTSP camera support.
- Automatic network camera discovery with optional sub-stream preference.
- Active zones and entry lines:
  - Draw polygons for monitored areas.
  - Draw line(s) for entry crossing events.
  - Trigger logic supports zone OR line crossing behavior.
- Motion-triggered recording:
  - 10-second clips by default.
  - Recording rendered with motion boxes.
- Snapshot capture and image recall.
- Main feed full-screen mode (`F11`, `Esc`, or button).
- MultiView grid for discovered network cameras:
  - Click a stream to focus it.
  - Return to grid with `Return to MultiView`.
- Audio alerts (Piper TTS if available, `pyttsx3` fallback).
- Telegram notifications:
  - Multiple recipients (comma-separated chat IDs).
  - Cooldown throttling.
  - Optional clip-in-notification mode (single message with video caption).
  - Pause/Resume/Status controls by chat command.
  - On-demand `clip` command (record now and send 10-second clip).
- Night bug filter (scheduled by hour) to suppress insect-like false positives.
- Auto reconnect for dropped camera streams.
- Collapsible control sections with persisted open/closed state.

## Requirements

- Python 3.10+ recommended
- Linux, macOS, or Windows
- Camera source:
  - Local webcam (`/dev/video*` etc.) and/or
  - RTSP network camera

## Installation

1. Clone and enter the project:
   ```bash
   git clone <your-repo-url>
   cd sentinel
   ```

2. Create and activate a virtual environment:
   ```bash
   python3 -m venv venv
   source venv/bin/activate
   ```
   On Windows (PowerShell):
   ```powershell
   python -m venv venv
   .\venv\Scripts\Activate.ps1
   ```

3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

## Run

```bash
python main.py
```

## First-Time Setup

1. Open app and click `Search Cameras`.
2. Select your camera from dropdown.
3. Click `Camera Offline` (button toggles feed on/off).
4. Optional:
   - Set `Prefer sub-stream for network cameras`.
   - Tune `Detection Size` and `Sensitivity`.
   - Draw active zones and entry lines.

## Detection and Trigger Behavior

- **No zones, no lines**: qualifying motion can trigger.
- **Zones only**: motion in active zone triggers.
- **Lines only**: line crossing triggers.
- **Zones + lines**: zone OR line crossing can trigger.

Use this pattern for driveway use cases:
- Entry line at driveway boundary to catch incoming vehicles.
- Active zone over driveway to keep alerts for activity inside zone (pets/people).

## Recording

- Recording starts on valid trigger and writes a clip to `recorded_videos/`.
- Recording duration is frame-count based for stable output length.
- `Motion Cooldown` controls minimum gap between recording starts.

## Telegram Setup

### 1) Create bot

In Telegram, open `@BotFather`:
- `/newbot`
- Save bot token (full string, including prefix before `:`).

### 2) Get chat ID(s)

- Start chat with bot (or send a message in your bot-enabled group).
- Open:
  - `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates`
- Copy `message.chat.id`.

### 3) Configure in Sentinel

In `Telegram Alerts` section:
- Enable Telegram notifications.
- Paste bot token.
- Paste one or more chat IDs (comma separated).
- Set cooldown seconds.
- Optional: enable `Include recording video clip`.
- Click `Send Telegram Test`.

## Telegram Commands

Supported commands (case-insensitive):

- `pause <minutes>` - pause notifications for that chat.
- `resume` - resume notifications for that chat.
- `status` - show active/paused state for that chat.
- `clip` - record and send a 10-second clip on demand.
- `help` - show command list.

Unknown messages get a help response listing valid commands.

## Night Bug Filter

Use `Night bug filter` in Motion Sensitivity section:
- Enable/disable filter.
- Configure `Night hours` start/end (0-23).
- Designed to suppress tiny, fast, edge-near, short-lived night artifacts (insects near lens) while preserving larger real events.

## Full Screen and MultiView

- Main view full-screen:
  - Click `Enter Full Screen`
  - or press `F11`
  - `Esc` exits.
- MultiView:
  - `Open MultiView`
  - click any tile to focus
  - click `Return to MultiView` to go back to grid.

## Troubleshooting

- RTSP decode errors (`h264 ... no frame`, `cabac decode ...`):
  - Prefer sub-stream.
  - Lower resolution/bitrate/fps in camera.
  - Prefer wired network and stable RTSP transport.
  - Keep H.264+ / smart codec features off.
- No Telegram messages:
  - Verify token, chat ID(s), and bot chat started.
  - Re-test with `Send Telegram Test`.
- Too many alerts:
  - Increase Telegram cooldown.
  - Increase motion cooldown.
  - Tighten zones/line placement.
  - Enable night bug filter schedule.

## Settings File

Runtime settings are saved to:
- `settings.json`

Includes camera source, detection controls, zone/line data, Telegram settings, UI state, and reconnect/night filter options.

## Project Layout

```text
sentinel/
├── main.py
├── settings.json
├── readme.md
├── requirements.txt
├── sentinel/
│   ├── app.py
│   ├── constants.py
│   ├── detection/
│   ├── audio/
│   ├── notifications/
│   ├── settings/
│   └── ui/
├── captured_images/
└── recorded_videos/
```

## License

This project is licensed under the MIT License. See `LICENSE` if present in your repo.