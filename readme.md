# Sentinel Home - Motion Detection

A Python-based motion detection application using OpenCV and PySide6. This program captures video from a webcam, detects motion, and provides audio alerts using text-to-speech (TTS).

![Sentinel Home](https://img.shields.io/badge/Sentinel%20Home-Motion%20Detection-00FFFF)

## Features

- Real-time motion detection using OpenCV's Background Subtractor MOG2
- Audio alerts via text-to-speech (pyttsx3) when motion is detected
- GUI built with PySide6 for camera control and settings management
- Support for multiple cameras with a dropdown selection
- Customizable speech settings:
  - Adjustable speech speed and volume
  - Multiple voice options
- Camera feed orientation options (horizontal/vertical)
- Image capture functionality with timestamp-based naming
- Recall latest captured image
- Dark theme UI with cyan accents
- Motion-triggered video recording:
  - Automatically records 10-second video clips when motion is detected
  - Visual recording indicator with countdown timer
  - Browse and play recordings with system video player
  - Organized storage with timestamp-based filenames

## Requirements

- Python 3.6+
- A webcam or compatible camera device
- Windows, macOS, or Linux operating system

## Installation

1. **Clone the repository**:
   ```bash
   git clone https://github.com/yourusername/sentinel.git
   cd sentinel
   ```

2. **Create and activate a virtual environment**:
   ```bash
   python -m venv .venv
   
   # On Windows:
   .venv\Scripts\activate
   
   # On macOS/Linux:
   source .venv/bin/activate
   ```

3. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

## Usage

1. **Run the application**:
   ```bash
   python main.py
   ```

2. **Using the application**:
   - Click "Search Cameras" to detect available cameras
   - Select your camera from the dropdown
   - Click "Camera Offline" to toggle the feed on
   - Adjust speech settings as needed
   - Use "Capture Image" to save the current frame
   - Use "Recall Image" to view the most recently captured image

## Settings

The application automatically saves your preferences in a `settings.json` file, including:
- Camera selection
- Speech settings (speed, volume, voice)
- Camera orientation
- Window and feed size
- Feed status (online/offline)

## Directory Structure

```
sentinel/
├── main.py              # Main application code
├── requirements.txt     # Required Python packages
├── .gitignore           # Git ignore file
├── README.md            # This file
├── sentinel.ico         # Application icon (if present)
└── captured_images/     # Directory for storing captured images (created automatically)
```

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

## License

This project is licensed under the MIT License - see the LICENSE file for details.

### Third-Party Licenses

This project uses several third-party libraries, each with their own licenses:

- **PySide6**: Licensed under LGPL v3.0. PySide6 is the official Python module from the Qt for Python project, which provides access to the complete Qt 6.0+ framework. See [Qt licensing](https://www.qt.io/licensing/) for more details.

- **OpenCV**: Licensed under Apache 2.0 License. OpenCV is an open-source computer vision and machine learning software library. See [OpenCV license](https://opencv.org/license/) for more details.

- **NumPy**: Licensed under BSD License. See [NumPy license](https://numpy.org/doc/stable/license.html) for more details.

- **pyttsx3**: Licensed under GNU GPL v3.0. This is a text-to-speech conversion library in Python. See [pyttsx3 license](https://github.com/nateshmbhat/pyttsx3/blob/master/LICENSE) for more details.

When distributing this application, please be aware of the obligations under these licenses, particularly:
- The LGPL v3.0 license of PySide6 may require you to make available the source code of your application if you distribute binaries
- The GNU GPL v3.0 license of pyttsx3 requires that derivative works be licensed under the same terms