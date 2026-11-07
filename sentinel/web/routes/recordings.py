"""
Recordings: the timeline's day of clips and browser-playable video of one clip.
"""

from __future__ import annotations

import re
import time
from typing import Dict, List

from flask import Flask, jsonify, request, send_file

from sentinel.web.context import WebContext


def register(app: Flask, ctx: WebContext) -> None:
    broker, recordings, _require_session = ctx.broker, ctx.recordings, ctx.require_session

    @app.route("/api/recordings")
    def api_recordings():
        """Timeline data for one local day: camera rows + clip offsets in seconds."""
        redir = _require_session()
        if redir:
            return jsonify({"error": "unauthorized"}), 401
        if recordings is None:
            return jsonify({"error": "recordings unavailable"}), 503
        from sentinel.recordings.store import local_day_bounds

        today = time.strftime("%Y-%m-%d")
        date_str = request.args.get("date") or today
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_str):
            return jsonify({"error": "bad date"}), 400
        try:
            day_start, day_end = local_day_bounds(date_str)
        except ValueError:
            return jsonify({"error": "bad date"}), 400

        clips = recordings.query(day_start, day_end)

        # Rows: live cameras first (keyed by host, as the detector names clips),
        # then any other cameras seen that day, with "unknown" last.
        rows: List[Dict[str, str]] = []
        seen = set()
        for key, display in broker.list_camera_hosts():
            if key not in seen:
                seen.add(key)
                rows.append({"key": key, "name": display})
        for clip in sorted(clips, key=lambda c: c["camera_key"] == "unknown"):
            if clip["camera_key"] not in seen:
                seen.add(clip["camera_key"])
                rows.append({"key": clip["camera_key"], "name": clip["camera_name"]})

        def _day(offset_days: int) -> str:
            return time.strftime(
                "%Y-%m-%d", time.localtime(day_start + 43200 + offset_days * 86400)
            )

        now = time.time()
        return jsonify(
            {
                "date": date_str,
                "prev_date": _day(-1),
                "next_date": _day(1) if date_str < today else None,
                "day_seconds": day_end - day_start,
                "now_offset": now - day_start if day_start <= now < day_end else None,
                "cameras": rows,
                "clips": [
                    {
                        "id": c["id"],
                        "cam": c["camera_key"],
                        "s": round(c["start_ts"] - day_start, 1),
                        "e": round(c["end_ts"] - day_start, 1),
                        "reason": c["reason"],
                        "hd": bool(c.get("hd_path")),
                        "labels": (c.get("labels") or "").split(",") if c.get("labels") else [],
                    }
                    for c in clips
                ],
            }
        )

    @app.route("/recordings/<int:recording_id>/video")
    def recording_video(recording_id: int):
        """Browser-playable MP4 of one clip (converted on first request, then cached)."""
        redir = _require_session()
        if redir:
            return redir
        if recordings is None:
            return "Recordings unavailable", 503
        path = recordings.web_playable_path(recording_id)
        if not path:
            return "Clip not found or could not be converted", 404
        return send_file(path, mimetype="video/mp4", conditional=True, max_age=3600)
