#!/usr/bin/env python3
"""
Sentinel Home - Motion Detection Application
Entry point script for launching the application.
"""

import signal
import sys
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from sentinel.app import MotionDetectionApp


def main():
    """Application entry point."""
    app = QApplication(sys.argv)

    # Create and show the main application window
    motion_app = MotionDetectionApp()
    motion_app.show()

    # systemd stops the service with SIGTERM: close the window so closeEvent
    # finalizes in-progress recordings instead of the process dying mid-clip.
    def _shutdown(signum, _frame):
        print(f"Received signal {signum}, shutting down")
        motion_app.main_window.close()
        app.quit()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    # Qt's event loop blocks Python signal handlers; wake the interpreter regularly.
    signal_timer = QTimer()
    signal_timer.timeout.connect(lambda: None)
    signal_timer.start(500)

    # Start the application event loop
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
