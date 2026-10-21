# Sentinel Home Project Structure

```
sentinel-home/                         # repository root
├── main.py                            # Desktop entry point (thin launcher)
├── setup.py                           # setuptools package metadata
├── requirements.txt                   # Core Python dependencies
├── requirements-piper.txt             # Optional Piper TTS (`pip install -r ...`)
├── readme.md                          # User documentation
├── installation-guide.md              # Installation + troubleshooting
├── project-structure.md               # This file
├── handoff_summary.md                 # Cross-session continuity for contributors
├── Sentinel.spec                      # PyInstaller spec (if used)
├── Sentinel.svg                       # App icon asset
├── models/                            # Piper `.onnx` + `.onnx.json` voice models (local, not all in git)
├── tests/                             # Unit tests (`python3 -m unittest discover -s tests`)
│   └── test_recording_and_audio.py
├── captured_images/                   # Saved snapshots (gitignored when populated)
├── recorded_videos/                   # Saved clips (gitignored when populated)
├── settings.json                      # Runtime settings (generated; gitignored)
├── sentinel_events.log                # Structured JSONL event log (gitignored via *.log)
└── sentinel/                          # Application package
    ├── __init__.py
    ├── app.py                         # Composes settings, detector, audio, UI
    ├── constants.py                   # Defaults (paths, FPS bounds, recording, UI)
    ├── logging_utils.py               # JSONL structured logger (`sentinel_events.log`)
    ├── web/                             # Optional HTTP live viewer (MJPEG)
    │   ├── __init__.py                  # Lazy exports for FrameBroker / WebServer
    │   ├── bind_util.py                 # Refuse binding on camera-only subnets
    │   ├── frame_broker.py              # Latest frame per camera (thread-safe)
    │   ├── auth.py                      # Bcrypt login, sessions, lockout / backoff
    │   ├── server.py                    # Flask routes (login, grid, MJPEG)
    │   ├── set_password.py              # CLI: `python -m sentinel.web.set_password`
    │   └── templates/                   # Jinja HTML (login, grid)
    ├── audio/
    │   ├── __init__.py
    │   └── alert_system.py            # Piper TTS + `aplay`, pyttsx3 fallback, volume scaling
    ├── detection/
    │   ├── __init__.py
    │   ├── motion_detector.py         # Camera IO, MOG2, zones/lines, recording, orientation
    │   └── network_discovery.py       # RTSP discovery / resolve / endpoints
    ├── notifications/
    │   ├── __init__.py
    │   └── telegram_notifier.py       # HTTPS Bot API, commands, clips
    ├── settings/
    │   ├── __init__.py
    │   └── manager.py                 # `settings.json` load/save + camera profile migration
    └── ui/
        ├── __init__.py
        ├── main_window.py             # Main UI, multi-session grid, overlays, controls
        ├── multiview_window.py        # Optional standalone multi-view dialog (legacy path)
        └── styles.py                  # Qt stylesheet fragment
```

## About This Structure

The project is organized by responsibility:

- **`sentinel/detection/`**: motion detection, tracking, zones/lines, recording, RTSP discovery
- **`sentinel/notifications/`**: Telegram (outbound HTTPS to `api.telegram.org`)
- **`sentinel/audio/`**: TTS alerts (Piper preferred, pyttsx3 fallback)
- **`sentinel/settings/`**: persistent settings (`settings.json`)
- **`sentinel/ui/`**: PySide6 main window and auxiliary views
- **`sentinel/logging_utils.py`**: append-only JSONL for camera/control events (local file)
- **`sentinel/web/`**: optional Flask MJPEG viewer; shares decoded frames via `FrameBroker` (no second RTSP decode)

`main.py` stays a thin launcher; `sentinel/app.py` wires components into `MainWindow`.

## Benefits of This Structure

- Each component can be tested or replaced independently
- Clear boundaries for cameras vs notifications vs speech
- Easier onboarding via this file + `readme.md` + `installation-guide.md`
