"""
Wi-Fi motion sensors: small ESP32 boards (firmware/wifi_motion) that notice people moving by how
their bodies disturb the Wi-Fi signal between the board and the router.

Each board turns the signal's wobble into a "score" four times a second and serves the last ten
seconds of scores at GET /status. Sentinel finds boards by the UDP "hello" they broadcast every 30 s,
polls them once a second, and decides here what counts as motion, so the thresholds can be tuned on
real recordings without touching the boards.

Motion = the score clearly above the room's own quiet level. The quiet level is learned, not set,
and it can jump: a door opened, someone sitting down next to a board or a nudged antenna moves the
score to a new level and keeps it there, steady. Walking makes it swing. So whenever the last 30 s
were steady, their median becomes the quiet level at once; swinging stretches never teach it. Every
score is also written to a daily CSV (wifi_motion/YYYY-MM-DD.csv, kept 14 days) so thresholds can be
tuned from what really happened.
"""

import csv
import ipaddress
import json
import os
import re
import socket
import statistics
import threading
import time
import urllib.request
from collections import deque
from datetime import datetime
from typing import Any, Deque, Dict, List, Optional, Tuple

from sentinel.logging_utils import app_logger

KNOWN_FILE = "wifi_sensors.json"
LOG_DIR = "wifi_motion"
HELLO_PORT = 47301
POLL_SECONDS = 1.0
OFFLINE_AFTER = 15.0
KEEP_DAYS = 14
SERIES_SECONDS = 120  # what the web page's live chart shows
NAME_MAX = 30
NAME_OK = re.compile(r"^[\w '&().\-]{1,%d}$" % NAME_MAX)
ID_OK = re.compile(r"^[0-9a-f]{4,12}$")

# First tuning (one sensor + beacon across a living room, 2026-11): still room ~0.92-1.05x its quiet level
# (single spikes to 1.2x, which the 1 s median removes); someone pacing ~1.2-1.5x, peaks 2-2.5x. Replaying
# that recording, 1.4x caught 96% of the pacing within ~2 s and flagged none of the still minutes; 2.0x
# missed long stretches. Re-check on longer quiet recordings (the daily CSVs).
# Steadiness = p90/p10 of the last 30 s of scores: 1.08-1.13 when still (also across a 12-minute jump to
# a new level that the old 30-minute percentile read as non-stop motion), 1.49-1.68 while someone paced.
STEADY_SAMPLES = 30 * 4
STEADY_SPREAD = 1.25
BASELINE_PERCENTILE = 10     # fallback if the first two minutes are never steady
WARMUP_SAMPLES = 2 * 60 * 4
RATIO = 1.4                  # motion when the score is this many times the quiet level...
MIN_DELTA = 3.0              # ...and at least this much above it (tiny quiet levels make ratios jumpy)
TRIGGER_SAMPLES = 2          # consecutive smoothed scores over the line (0.5 s)
HOLD_SECONDS = 15.0          # motion "ends" after this long with nothing


class MotionJudge:
    """Scores in, motion on/off out. Pure (no clock or network) so it can be replayed on recorded CSVs."""

    def __init__(self, ratio: float = RATIO, min_delta: float = MIN_DELTA) -> None:
        self.ratio, self.min_delta = ratio, min_delta
        self._recent: Deque[float] = deque(maxlen=4)  # the last second, for a median against single spikes
        self._history: Deque[float] = deque(maxlen=WARMUP_SAMPLES)
        self._baseline: Optional[float] = None
        self._since_baseline = 0
        self._over = 0
        self.motion = False
        self.last_motion = 0.0
        self.started = 0.0
        self.peak = 0.0
        self.smoothed: Optional[float] = None

    @property
    def baseline(self) -> Optional[float]:
        return self._baseline

    def threshold(self) -> Optional[float]:
        if self._baseline is None:
            return None
        return max(self._baseline * self.ratio, self._baseline + self.min_delta)

    def _update_baseline(self) -> None:
        """Every 10 s: a steady last 30 s becomes the quiet level; the first time, settle for a low percentile."""
        window = sorted(list(self._history)[-STEADY_SAMPLES:])
        if len(window) == STEADY_SAMPLES:
            low, high = window[len(window) // 10], window[len(window) * 9 // 10]
            if low > 0 and high / low <= STEADY_SPREAD:
                self._baseline = statistics.median(window)
                return
        if self._baseline is None and len(self._history) >= WARMUP_SAMPLES:
            values = sorted(self._history)
            self._baseline = values[len(values) * BASELINE_PERCENTILE // 100]

    def feed(self, ts: float, score: float) -> Optional[str]:
        """One score; returns "started" or "ended" when motion changes, else None. score < 0 = no reading."""
        if score < 0:
            return None
        self._recent.append(score)
        self.smoothed = statistics.median(self._recent)
        self._history.append(score)
        self._since_baseline += 1
        if self._since_baseline >= 40:  # every 10 s
            self._update_baseline()
            self._since_baseline = 0
        if self._baseline is None:
            return None
        if self.smoothed > self.threshold():
            self._over += 1
        else:
            self._over = 0
        if self._over >= TRIGGER_SAMPLES:
            self.last_motion = ts
            self.peak = max(self.peak, self.smoothed)
            if not self.motion:
                self.motion, self.started, self.peak = True, ts, self.smoothed
                return "started"
        elif self.motion and ts - self.last_motion > HOLD_SECONDS:
            self.motion = False
            return "ended"
        return None


class Sensor:
    def __init__(self, sensor_id: str, ip: str = "", name: str = "", host: str = "", fw: str = "") -> None:
        self.id, self.ip, self.host, self.fw = sensor_id, ip, host, fw
        self.name = name or f"Wi-Fi sensor {sensor_id}"
        self.judge = MotionJudge()
        self.last_seq: Optional[int] = None
        self.last_seen = 0.0
        self.rssi: Optional[int] = None
        self.rate: Optional[float] = None
        self.battery: Optional[Dict[str, float]] = None
        self.source = ""  # what the board measures: "beacon" (a second board) or "router" (ping replies)
        self.series: Deque[Tuple[float, float]] = deque(maxlen=SERIES_SECONDS * 4)


def _private_ipv4(text: str) -> bool:
    try:
        ip = ipaddress.ip_address(text)
    except ValueError:
        return False
    return ip.version == 4 and ip.is_private and not ip.is_loopback


def _clean_name(name: Any) -> str:
    return re.sub(r"\s+", " ", name).strip() if isinstance(name, str) else ""


class WifiMotion:
    """Finds the boards, polls them, judges motion, logs it, and serves the Sensors page."""

    def __init__(self, store_dir: str, fetch=None) -> None:
        self.store_dir = store_dir
        self.path = os.path.join(store_dir, KNOWN_FILE)
        self.log_dir = os.path.join(store_dir, LOG_DIR)
        self._fetch = fetch or self._http_status
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._threads: List[threading.Thread] = []
        self._sensors: Dict[str, Sensor] = {}
        self._csv_day = ""
        self._csv_file = None
        self._csv = None
        self._load()

    # ---- known boards (id -> last address and name) ----
    def _load(self) -> None:
        try:
            with open(self.path) as f:
                data = json.load(f)
        except (OSError, ValueError):
            return
        for sid, info in (data if isinstance(data, dict) else {}).items():
            if isinstance(sid, str) and ID_OK.match(sid) and isinstance(info, dict):
                ip = info.get("ip") if _private_ipv4(str(info.get("ip"))) else ""
                self._sensors[sid] = Sensor(sid, ip, _clean_name(info.get("name")), _clean_name(info.get("host")),
                                            _clean_name(info.get("fw")))

    def _save(self) -> None:
        data = {s.id: {"ip": s.ip, "name": s.name, "host": s.host, "fw": s.fw} for s in self._sensors.values()}
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=1)
        os.replace(tmp, self.path)

    def hello(self, payload: bytes, source_ip: str) -> bool:
        """A board's broadcast. The address used is the one the packet came from, not what it claims."""
        try:
            msg = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return False
        if not isinstance(msg, dict) or msg.get("sentinel_sensor") != "wifi_motion":
            return False
        sid = str(msg.get("id") or "").lower()
        if not ID_OK.match(sid) or not _private_ipv4(source_ip):
            return False
        with self._lock:
            sensor = self._sensors.get(sid)
            new = sensor is None
            if new:
                sensor = self._sensors[sid] = Sensor(sid)
            changed = new or sensor.ip != source_ip or sensor.fw != _clean_name(msg.get("fw"))
            sensor.ip, sensor.host, sensor.fw = source_ip, _clean_name(msg.get("host"))[:40], _clean_name(msg.get("fw"))[:16]
            if changed:
                self._save()
        if new:
            app_logger.log("info", "wifi_sensor_found", sensor=sid, ip=source_ip, fw=sensor.fw,
                           component="wifi_motion", action="hello")
        return True

    # ---- polling ----
    @staticmethod
    def _http_status(ip: str) -> Dict[str, Any]:
        with urllib.request.urlopen(f"http://{ip}/status", timeout=2.0) as r:
            return json.loads(r.read(64 * 1024).decode("utf-8"))

    def poll(self, sensor: Sensor, now: Optional[float] = None) -> None:
        if not sensor.ip:
            return
        try:
            status = self._fetch(sensor.ip)
        except (OSError, ValueError):
            return
        now = time.time() if now is None else now
        samples = status.get("samples") if isinstance(status, dict) else None
        if not isinstance(samples, list):
            return
        with self._lock:
            was_online = now - sensor.last_seen < OFFLINE_AFTER
            sensor.last_seen = now
            if not was_online:
                app_logger.log("info", "wifi_sensor_online", sensor=sensor.id, ip=sensor.ip,
                               component="wifi_motion", action="poll")
            rssi = status.get("wifi_rssi")
            sensor.rssi = int(rssi) if isinstance(rssi, (int, float)) else None
            batt = status.get("battery")
            sensor.battery = ({"v": float(batt["v"]), "pct": float(batt["pct"])}
                              if isinstance(batt, dict) and isinstance(batt.get("v"), (int, float))
                              and isinstance(batt.get("pct"), (int, float)) else None)
            source = status.get("source") if isinstance(status.get("source"), str) else ""
            if source != sensor.source:
                if sensor.source:  # a different signal path has a different quiet level: learn it afresh
                    sensor.judge = MotionJudge()
                    sensor.series.clear()
                sensor.source = source[:10]
            rows = [s for s in samples if isinstance(s, list) and len(s) >= 3
                    and all(isinstance(v, (int, float)) for v in s[:3])]
            if rows and sensor.last_seq is not None and rows[-1][0] < sensor.last_seq:
                # The board restarted: its counter began again. Restarts mostly mean it was unplugged and moved
                # (or updated), so learn the room afresh rather than judging a new spot by the old one's quiet level.
                sensor.last_seq = None
                sensor.judge = MotionJudge()
                sensor.series.clear()
            fresh = [r for r in rows if sensor.last_seq is None or r[0] > sensor.last_seq]
            if not fresh:
                return
            sensor.last_seq = int(fresh[-1][0])
            # The board reports 4 per second, newest last; spread them back over the time they cover.
            for i, (seq, score, rate) in enumerate(fresh):
                ts = now - (len(fresh) - 1 - i) * 0.25
                sensor.rate = float(rate)
                self._record(ts, sensor, float(score), float(rate))
                if score >= 0:
                    sensor.series.append((ts, float(score)))
                change = sensor.judge.feed(ts, float(score))
                if change == "started":
                    app_logger.log("info", "wifi_motion_started", sensor=sensor.id, name=sensor.name,
                                   score=round(sensor.judge.smoothed, 2),
                                   baseline=round(sensor.judge.baseline or 0, 2),
                                   component="wifi_motion", action="judge")
                elif change == "ended":
                    app_logger.log("info", "wifi_motion_ended", sensor=sensor.id, name=sensor.name,
                                   seconds=round(sensor.judge.last_motion - sensor.judge.started, 1),
                                   peak=round(sensor.judge.peak, 2), component="wifi_motion", action="judge")
            if self._csv_file:
                try:
                    self._csv_file.flush()
                except OSError:
                    pass

    # ---- daily CSV of every score (for tuning) ----
    def _record(self, ts: float, sensor: Sensor, score: float, rate: float) -> None:
        day = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
        try:
            if day != self._csv_day:
                if self._csv_file:
                    self._csv_file.close()
                os.makedirs(self.log_dir, exist_ok=True)
                path = os.path.join(self.log_dir, f"{day}.csv")
                fresh = not os.path.exists(path)
                self._csv_file = open(path, "a", newline="")
                self._csv = csv.writer(self._csv_file)
                if fresh:
                    self._csv.writerow(["time", "sensor", "score", "rate", "rssi"])
                self._csv_day = day
                self._prune_logs()
            self._csv.writerow([f"{ts:.2f}", sensor.id, f"{score:.2f}", f"{rate:.1f}",
                                sensor.rssi if sensor.rssi is not None else ""])
        except OSError:
            pass

    def _prune_logs(self) -> None:
        cutoff = time.time() - KEEP_DAYS * 86400
        for name in os.listdir(self.log_dir):
            full = os.path.join(self.log_dir, name)
            if name.endswith(".csv") and os.path.getmtime(full) < cutoff:
                os.remove(full)

    # ---- threads ----
    def start(self) -> None:
        for target, name in ((self._listen, "wifi-motion-hello"), (self._poll_loop, "wifi-motion-poll")):
            t = threading.Thread(target=target, name=name, daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=3)
        if self._csv_file:
            self._csv_file.close()

    def _listen(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("0.0.0.0", HELLO_PORT))
            sock.settimeout(1.0)
        except OSError as e:
            app_logger.log("error", "wifi_motion_listen_failed", error_message=str(e), component="wifi_motion",
                           action="listen")
            sock.close()
            return
        with sock:
            while not self._stop.is_set():
                try:
                    data, (addr, _port) = sock.recvfrom(2048)
                except socket.timeout:
                    continue
                except OSError:
                    time.sleep(1)
                    continue
                self.hello(data, addr)

    def _poll_loop(self) -> None:
        while not self._stop.wait(POLL_SECONDS):
            with self._lock:
                sensors = list(self._sensors.values())
            for sensor in sensors:
                self.poll(sensor)

    # ---- the web page and questions ----
    def rename(self, sensor_id: str, name: str) -> Tuple[bool, str]:
        name = _clean_name(name)
        if not NAME_OK.match(name):
            return False, "Use up to 30 letters, numbers and spaces."
        with self._lock:
            sensor = self._sensors.get(sensor_id)
            if sensor is None:
                return False, "That sensor isn't known."
            sensor.name = name
            self._save()
        return True, "renamed"

    def view(self, now: Optional[float] = None) -> Dict[str, Any]:
        now = time.time() if now is None else now
        out = []
        with self._lock:
            for s in self._sensors.values():
                j = s.judge
                out.append({
                    "id": s.id, "name": s.name, "ip": s.ip, "fw": s.fw,
                    "online": now - s.last_seen < OFFLINE_AFTER,
                    "motion": j.motion, "last_motion": j.last_motion or None,
                    "score": None if j.smoothed is None else round(j.smoothed, 2),
                    "baseline": None if j.baseline is None else round(j.baseline, 2),
                    "threshold": None if j.threshold() is None else round(j.threshold(), 2),
                    "learning": j.baseline is None,
                    "rssi": s.rssi, "rate": s.rate, "battery": s.battery, "source": s.source,
                    "series": [[round(t - now, 2), v] for t, v in s.series],
                })
        return {"sensors": out}
