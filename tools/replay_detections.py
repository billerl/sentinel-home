"""
Replay recorded clips through the object detector + tracker and print the alerts
they would produce. Use it before changing any detection threshold.

    venv/bin/python tools/replay_detections.py 2026-10-20 [max_clips] [out_dir]

Samples up to ``max_clips`` (default 60) of that day's clips, runs detection at ~2 fps
like the live engine, and saves a boxed snapshot per alert in ``out_dir``
(default: a temp folder). Read-only with respect to recordings.
"""

import os
import sqlite3
import sys
import tempfile
import time

import cv2

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
from sentinel.detection.objects import ObjectDetector  # noqa: E402
from sentinel.engine.objects import CameraObjectTracker, describe  # noqa: E402


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    day = sys.argv[1]
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else 60
    out = sys.argv[3] if len(sys.argv) > 3 else tempfile.mkdtemp(prefix="sentinel-replay-")
    os.makedirs(out, exist_ok=True)
    db = sqlite3.connect(f"file:{os.path.join(REPO, 'recorded_videos', 'recordings.db')}?mode=ro", uri=True)
    day0 = time.mktime(time.strptime(day, "%Y-%m-%d"))
    rows = db.execute("SELECT id, path, camera_key, camera_name, start_ts FROM recordings "
                      "WHERE start_ts BETWEEN ? AND ? ORDER BY start_ts", (day0, day0 + 86400)).fetchall()
    rows = rows[::max(1, len(rows) // limit)][:limit]
    det = ObjectDetector(device="auto")
    print(f"{len(rows)} clips, detector on {det.device}", flush=True)
    for rid, path, cam, name, ts in rows:
        cap = cv2.VideoCapture(os.path.join(REPO, "recorded_videos", path))
        fps = cap.get(cv2.CAP_PROP_FPS) or 12
        every = max(1, int(round(fps / 2)))
        tracker = CameraObjectTracker(cam)
        i = 0
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            if i % every == 0:
                for track in tracker.update(det.detect(frame, min_score=0.3), frame, ts + i / fps, lambda b: True):
                    source = track.best_frame if track.best_frame is not None else frame
                    text = describe(track, name, None, source, frame.shape[1])
                    snap = source.copy()
                    x, y, w, h = track.best_box or track.box
                    cv2.rectangle(snap, (x, y), (x + w, y + h), (0, 200, 255), 2)
                    file = os.path.join(out, f"{rid}_{track.track_id}_{track.label}.jpg")
                    cv2.imwrite(file, snap)
                    print(time.strftime("%H:%M:%S", time.localtime(ts)), f"{track.best_score:.2f}", text,
                          os.path.basename(file))
            i += 1
    print("snapshots in", out)


if __name__ == "__main__":
    main()
