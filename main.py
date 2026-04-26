#!/usr/bin/env python3
"""
Sentinel Home - Motion Detection Application
Entry point script for launching the application.
"""

import sys
from PySide6.QtWidgets import QApplication
from sentinel.app import MotionDetectionApp


def main():
    """Application entry point."""
    app = QApplication(sys.argv)
    
    # Create and show the main application window
    motion_app = MotionDetectionApp()
    motion_app.show()
    
    # Start the application event loop
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
