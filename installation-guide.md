# Installation Guide for Sentinel Home

This guide provides detailed instructions for installing and running the Sentinel Home motion detection application.

## Prerequisites

- Python 3.6 or higher
- A webcam or compatible camera device
- Git (for cloning the repository)

## Installation Methods

### Method 1: Using Git and Virtual Environment (Recommended)

1. **Clone the repository**:
   ```bash
   git clone https://github.com/yourusername/sentinel.git
   cd sentinel
   ```

2. **Create and activate a virtual environment**:
   ```bash
   # Create a virtual environment
   python -m venv .venv

   # On Windows
   .venv\Scripts\activate

   # On macOS/Linux
   source .venv/bin/activate
   ```

3. **Install the package in development mode**:
   ```bash
   pip install -e .
   ```

4. **Run the application**:
   ```bash
   python main.py
   ```

### Method 2: Direct Installation

1. **Clone the repository**:
   ```bash
   git clone https://github.com/yourusername/sentinel.git
   cd sentinel
   ```

2. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

3. **Run the application**:
   ```bash
   python main.py
   ```

### Method 3: Installation via pip (Future)

Once the package is published to PyPI, you can install it directly:

```bash
pip install sentinel-motion-detection

# Run the application
sentinel
```

## Troubleshooting

### Camera Issues

- If no cameras are detected, ensure your webcam is properly connected
- Some webcams may require specific drivers to be installed
- Try disconnecting and reconnecting your webcam
- If using a laptop with built-in webcam, check if it's enabled in BIOS/system settings

### Text-to-Speech Issues

- On Windows, ensure the Microsoft Speech API is installed
- On macOS, no additional setup should be needed
- On Linux, you may need to install additional packages:
  ```bash
  # Ubuntu/Debian
  sudo apt-get install espeak
  
  # Fedora
  sudo dnf install espeak
  ```


### Video Recording Feature

When motion is detected, the application automatically:
- Records a 10-second video clip
- Displays a red recording indicator with countdown
- Saves the video with a timestamp in the "recorded_videos" directory

You can access these recordings through the "Browse Recordings" button, which will open your system's default video player.

Note: The video recordings use the XVID codec, which is widely supported but may require additional codecs on some systems:
- On Windows, you might need to install K-Lite Codec Pack
- On macOS, VLC player is recommended
- On Linux, ensure you have the necessary gstreamer plugins installed


### UI Issues

- If you encounter UI rendering problems, ensure your system meets the minimum requirements for PySide6
- Try updating your graphics drivers if you experience visual glitches

## Development Setup

If you want to contribute to the development:

1. Follow the installation steps using the virtual environment
2. Install development dependencies:
   ```bash
   pip install -r requirements-dev.txt  # If provided
   ```

3. Run tests:
   ```bash
   pytest  # If tests are available
   ```

## Further Information

For more detailed information on using the application, refer to the README.md file.
