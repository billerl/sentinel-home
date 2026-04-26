# Sentinel Home Project Structure

```
sentinel/
├── main.py                              # Desktop entry point
├── readme.md                            # User documentation
├── installation-guide.md                # Installation-focused guide
├── project-structure.md                 # This file
├── requirements.txt                     # Python dependencies
├── settings.json                        # Runtime settings (generated/updated)
├── captured_images/                     # Saved snapshots
├── recorded_videos/                     # Saved recordings
├── sentinel/
│   ├── __init__.py
│   ├── app.py                           # Wiring of app components
│   ├── constants.py                     # App constants/defaults
│   ├── audio/
│   │   ├── __init__.py
│   │   └── alert_system.py              # Piper/pyttsx3 alert engine
│   ├── detection/
│   │   ├── __init__.py
│   │   ├── motion_detector.py           # Detection, zones/lines, recording logic
│   │   └── network_discovery.py         # RTSP camera discovery
│   ├── notifications/
│   │   ├── __init__.py
│   │   └── telegram_notifier.py         # Telegram send/commands/pause/clip
│   ├── settings/
│   │   ├── __init__.py
│   │   └── manager.py                   # Persistent settings layer
│   └── ui/
│       ├── __init__.py
│       ├── main_window.py               # Main control + feed window
│       ├── multiview_window.py          # Multi-camera grid/focus view
│       └── styles.py
```

## About This Structure

The project is organized by responsibility:

- **`sentinel/detection/`**: motion detection, tracking, zones/lines, recording, network camera discovery
- **`sentinel/notifications/`**: Telegram notifications + inbound command handling
- **`sentinel/audio/`**: speech alert subsystem
- **`sentinel/settings/`**: load/save user settings (`settings.json`)
- **`sentinel/ui/`**: main window and multiview interfaces

`main.py` remains a thin launcher, while `sentinel/app.py` composes the modules into the running desktop application.

## Benefits of This Structure

- Each component can be tested individually
- Easier to understand and maintain
- Flexible for feature growth (new notifiers/camera backends/UI pages)
- Better separation of concerns
- Makes collaboration easier
