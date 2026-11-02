#!/usr/bin/env python3
"""
Sentinel Home - headless home security engine.

Runs the cameras, recordings, Telegram bot and web viewer. The web viewer
(see settings.json "web") is the user interface.
"""

import os
import signal
import sys

from sentinel.engine import Engine


def main() -> int:
    engine = Engine()

    # systemd stops the service with SIGTERM; finish clips and save settings first.
    def _shutdown(signum, _frame):
        print(f"Received signal {signum}, shutting down")
        engine.stop()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    engine.start()
    engine.wait()
    # Clips are finished and settings saved. Skip Python's final cleanup: the speech libraries
    # (CTranslate2 / onnxruntime threads) can crash there, and the crash dump held up the restart.
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)


if __name__ == "__main__":
    sys.exit(main())
