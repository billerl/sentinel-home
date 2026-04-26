# Sentinel Home Project Structure

```
sentinel/
├── main.py                     # Entry point (loads from modules)
├── sentinel/                   # Main package
│   ├── __init__.py             # Package initialization
│   ├── app.py                  # Main application class
│   ├── constants.py            # Configuration constants
│   ├── detection/              # Motion detection module
│   │   ├── __init__.py
│   │   └── motion_detector.py  # Motion detection class
│   ├── audio/                  # Audio module
│   │   ├── __init__.py
│   │   └── alert_system.py     # Text-to-speech alert system
│   ├── settings/               # Settings module
│   │   ├── __init__.py
│   │   └── manager.py          # Settings management class
│   └── ui/                     # User interface module
│       ├── __init__.py
│       ├── main_window.py      # Main window class
│       └── styles.py           # UI stylesheets
├── requirements.txt            # Dependencies
├── LICENSE                     # License file
├── .gitignore                  # Git ignore file
└── README.md                   # Project documentation
```

## About This Structure

This modular structure separates different components of the application:

1. **Constants**: Configuration values are stored in one place for easy adjustments
2. **Detection**: Motion detection logic is isolated
3. **Audio**: Text-to-speech functionality is separate
4. **Settings**: Settings management has its own module
5. **UI**: User interface code is organized separately

The main application class in `app.py` integrates these components. The entry point `main.py` remains simple.

## Benefits of This Structure

- Each component can be tested individually
- Easier to understand and maintain
- More flexible for future enhancements
- Better separation of concerns
- Makes collaboration easier
