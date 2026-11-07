"""
What Sentinel tells the family without being asked: package news, the morning recap, routine and weather messages.

Moved out of engine.py; the engine hands it what it needs.
"""

import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from sentinel.logging_utils import app_logger


class HouseNews:
    """Messages to every Sentinel chat that hasn't said Pause. packages (PackageWatch) is looked up each time:
    it starts later than this, and only when object detection is on."""

    def __init__(self, settings: Dict[str, Any], telegram, recording_store, weather,
                 packages: Callable[[], Any], weather_warnings: Callable[[], List[Dict[str, Any]]],
                 home_coords: Callable[[], Optional[Tuple[float, float]]],
                 seen: Callable[[str, str, Optional[bytes]], None]) -> None:
        self.settings = settings
        self.telegram = telegram
        self.recording_store = recording_store
        self.weather = weather
        self._packages = packages
        self._weather_warnings = weather_warnings
        self._home_coords = home_coords
        self._seen = seen  # (camera, "package", photo) -> routines

    def recap_extra(self) -> List[str]:
        """The recap's last lines: parcels still waiting, warnings in effect, today's weather."""
        lines = []
        packages = self._packages()
        if packages is not None:
            lines += [f"📦 {n} {'package' if n == 1 else 'packages'} still at the {cam}"
                      for cam, n in packages.packages().items()]
        lines += [f"⚠️ {a['event']}" + (f" until {a['until']}" if a["until"] else "") for a in self._weather_warnings()]
        if self.settings.get("weather_location"):
            w = self.weather.current()
            if w.get("temp") is not None:
                lines.append(f"{w.get('icon', '')} Now {w['temp']}{w.get('unit', '°')}, {w.get('words', '').lower()}; "
                             f"high {w.get('high')}°, {w.get('rain_today')}% chance of rain")
        return lines

    def _recap_chats(self, chat: Optional[str]) -> List[str]:
        return [chat] if chat else self.telegram.unpaused_chats()

    def recap_text(self, chat: Optional[str], text: str) -> None:
        for c in self._recap_chats(chat):
            self.telegram.reply_to_chat(c, text)

    def recap_video(self, chat: Optional[str], path: str, caption: str) -> None:
        for c in self._recap_chats(chat):
            self.telegram.send_video(c, path, caption)

    def tell_everyone(self, text: str) -> None:
        """A message to every Sentinel chat (weather warnings)."""
        for chat in list(self.telegram.chat_ids):
            self.telegram.reply_to_chat(chat, text)

    def on_package(self, camera_key: str, camera_name: str, text: str, jpeg: Optional[bytes]) -> None:
        """A parcel left at a door: into the history (questions, timeline, the morning recap) and to routines."""
        try:
            self.recording_store.add_event(time.time(), camera_key, camera_name, "package", text, label="package",
                                           snapshot_jpeg=jpeg)
        except Exception as e:
            app_logger.log("error", "event_store_failed", error_message=str(e), component="engine", action="package")
        self._seen(camera_key, "package", jpeg)

    def package_message(self, text: str, jpeg: Optional[bytes], ask_done: bool = False) -> None:
        """Package news to every unpaused chat; "left" and the sunset reminder get a "✅ Got it" button."""
        if not (ask_done and jpeg):
            self.routine_message(text, jpeg)
            return
        for chat_id in self.telegram.unpaused_chats():
            self.telegram.send_photo_with_buttons(chat_id, jpeg, text, [[("✅ Got it", "pkg:got")]])

    def packages_taken_in(self) -> str:
        packages = self._packages()
        n = packages.clear() if packages is not None else 0
        return ("✅ Okay, the package is marked as taken in: no more reminders." if n
                else "There wasn't a package waiting, as far as I could see.")

    def sunset_today(self, now: float) -> Optional[float]:
        from sentinel.engine.routines import sun_times

        spot = self._home_coords()
        times = sun_times(spot[0], spot[1], now) if spot else None
        return times[1] if times else None

    def routine_message(self, text: str, jpeg: Optional[bytes]) -> None:
        for chat_id in self.telegram.unpaused_chats():
            if jpeg:
                self.telegram.send_photo(chat_id, jpeg, text)
            else:
                self.telegram.reply_to_chat(chat_id, text)
