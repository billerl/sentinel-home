"""
Wi-Fi motion sensors: small ESP32 boards (firmware/wifi_motion) that notice people moving by how
their bodies disturb the Wi-Fi signal between two boards (or, for a lone board, the router).

Every board sends 50 frames a second and measures the ones it hears from the others, so each pair of
boards is one "path" through the house, measured from both ends. Each end ("line") keeps its own quiet
level and motion; the page shows one card per path, moving when either end sees it.

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
LINK_OK = re.compile(r"^[0-9a-z]{2,6}$")
LINE_ID_OK = re.compile(r"^[0-9a-f]{4,12}-[0-9a-z]{2,6}$")
SHOW_AWAY_SECONDS = 10 * 60  # a line not heard from for this long leaves the page (its name is kept)

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
RATIO_MIN, RATIO_MAX = 1.1, 3.0  # the movement line the user may pick per path on the Sensors page


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


class Board:
    """One sensor board on the network."""

    def __init__(self, board_id: str, ip: str = "", host: str = "", fw: str = "", name: str = "") -> None:
        self.id, self.ip, self.host, self.fw = board_id, ip, host, fw
        self.name = name  # what the user calls the board ("Feather", "Kitchen board"); else its id
        self.last_seen = 0.0
        self.rssi: Optional[int] = None
        self.battery: Optional[Dict[str, float]] = None


class Line:
    """One radio path a board measures (to a beacon board, or to the router): what the page calls a sensor.

    Each line watches the space along its own straight path, so each has its own quiet level and motion."""

    def __init__(self, board: Board, link: str, source: str, name: str = "") -> None:
        self.board, self.link, self.source = board, link, source
        self.id = f"{board.id}-{link}"
        if not name:
            name = (f"Wi-Fi sensor {board.id} (router)" if source == "router"
                    else f"Wi-Fi sensor {board.id} to {link}")
        self.name = name
        self.judge = MotionJudge()
        self.last_seq: Optional[int] = None
        self.last_data = 0.0
        self.rssi: Optional[int] = None
        self.rate: Optional[float] = None
        self.series: Deque[Tuple[float, float]] = deque(maxlen=SERIES_SECONDS * 4)


def _private_ipv4(text: str) -> bool:
    try:
        ip = ipaddress.ip_address(text)
    except ValueError:
        return False
    return ip.version == 4 and ip.is_private and not ip.is_loopback


def _clean_name(name: Any) -> str:
    return re.sub(r"\s+", " ", name).strip() if isinstance(name, str) else ""


def path_id(line: "Line") -> str:
    """The path a line measures: board-to-board lines from both ends share one id, a router line is its own."""
    if line.source == "beacon" and ID_OK.match(line.link):
        return "-".join(sorted((line.board.id, line.link)))
    return line.id


def _status_links(status: Dict[str, Any]) -> List[Tuple[str, str, Any, List[list]]]:
    """[(link id, source, rssi, sample rows)] from a board's /status; older firmware had one top-level list."""
    raw = status.get("links")
    if not isinstance(raw, list):
        raw = [{"id": "rtr", "source": status.get("source") or "router", "rssi": status.get("rssi"),
                "samples": status.get("samples")}]
    out = []
    for link in raw:
        if not isinstance(link, dict) or not isinstance(link.get("samples"), list):
            continue
        lid = str(link.get("id") or "").lower()
        source = link.get("source") if link.get("source") in ("router", "beacon") else ""
        if not LINK_OK.match(lid) or not source:
            continue
        rows = [r for r in link["samples"] if isinstance(r, list) and len(r) >= 3
                and all(isinstance(v, (int, float)) for v in r[:3])]
        out.append((lid, source, link.get("rssi"), rows))
    return out


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
        self._boards: Dict[str, Board] = {}
        self._lines: Dict[str, Line] = {}
        self._names: Dict[str, str] = {}  # line id -> name the user gave it (kept while a line is away)
        self._ratios: Dict[str, float] = {}  # path id -> movement line the user picked (else RATIO)
        self._csv_day = ""
        self._csv_file = None
        self._csv = None
        self._load()

    # ---- known boards (id -> last address) and line names ----
    def _load(self) -> None:
        try:
            with open(self.path) as f:
                data = json.load(f)
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        boards = data.get("boards") if isinstance(data.get("boards"), dict) else data  # first format: boards only
        for bid, info in boards.items():
            if isinstance(bid, str) and ID_OK.match(bid) and isinstance(info, dict):
                ip = info.get("ip") if _private_ipv4(str(info.get("ip"))) else ""
                name = _clean_name(info.get("name"))
                self._boards[bid] = Board(bid, ip, _clean_name(info.get("host")), _clean_name(info.get("fw")),
                                          name if NAME_OK.match(name) and bid != name else "")
        names = data.get("names") if isinstance(data.get("names"), dict) else {}
        for lid, name in names.items():
            if isinstance(lid, str) and LINE_ID_OK.match(lid) and NAME_OK.match(_clean_name(name)):
                self._names[lid] = _clean_name(name)
        ratios = data.get("ratios") if isinstance(data.get("ratios"), dict) else {}
        for pid, ratio in ratios.items():
            if (isinstance(pid, str) and LINE_ID_OK.match(pid) and isinstance(ratio, (int, float))
                    and RATIO_MIN <= ratio <= RATIO_MAX):
                self._ratios[pid] = float(ratio)

    def _save(self) -> None:
        data = {"boards": {b.id: {"ip": b.ip, "host": b.host, "fw": b.fw, **({"name": b.name} if b.name else {})}
                           for b in self._boards.values()},
                "names": self._names, "ratios": self._ratios}
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
        bid = str(msg.get("id") or "").lower()
        if not ID_OK.match(bid) or not _private_ipv4(source_ip):
            return False
        with self._lock:
            board = self._boards.get(bid)
            new = board is None
            if new:
                board = self._boards[bid] = Board(bid)
            fw = _clean_name(msg.get("fw"))[:16]
            changed = new or board.ip != source_ip or board.fw != fw
            board.ip, board.host, board.fw = source_ip, _clean_name(msg.get("host"))[:40], fw
            if changed:
                self._save()
        if new:
            app_logger.log("info", "wifi_sensor_found", sensor=bid, ip=source_ip, fw=fw,
                           component="wifi_motion", action="hello")
        return True

    # ---- polling ----
    @staticmethod
    def _http_status(ip: str) -> Dict[str, Any]:
        with urllib.request.urlopen(f"http://{ip}/status", timeout=2.0) as r:
            return json.loads(r.read(64 * 1024).decode("utf-8"))

    def poll(self, board: Board, now: Optional[float] = None) -> None:
        if not board.ip:
            return
        try:
            status = self._fetch(board.ip)
        except (OSError, ValueError):
            return
        if not isinstance(status, dict):
            return
        now = time.time() if now is None else now
        with self._lock:
            if now - board.last_seen >= OFFLINE_AFTER:
                app_logger.log("info", "wifi_sensor_online", sensor=board.id, ip=board.ip,
                               component="wifi_motion", action="poll")
            board.last_seen = now
            rssi = status.get("wifi_rssi")
            board.rssi = int(rssi) if isinstance(rssi, (int, float)) else None
            batt = status.get("battery")
            board.battery = ({"v": float(batt["v"]), "pct": float(batt["pct"])}
                             if isinstance(batt, dict) and isinstance(batt.get("v"), (int, float))
                             and isinstance(batt.get("pct"), (int, float)) else None)
            for lid, source, link_rssi, rows in _status_links(status):
                line_id = f"{board.id}-{lid}"
                line = self._lines.get(line_id)
                if line is None:
                    line = self._lines[line_id] = Line(board, lid, source, self._names.get(line_id, ""))
                    line.judge = self._judge_for(line)
                    app_logger.log("info", "wifi_line_found", sensor=line_id, source=source,
                                   component="wifi_motion", action="poll")
                line.rssi = int(link_rssi) if isinstance(link_rssi, (int, float)) else None
                self._take(line, rows, now)
            if self._csv_file:
                try:
                    self._csv_file.flush()
                except OSError:
                    pass

    def _take(self, line: Line, rows: List[list], now: float) -> None:
        if rows and line.last_seq is not None and rows[-1][0] < line.last_seq:
            # The board restarted: its counter began again. Restarts mostly mean it was unplugged and moved
            # (or updated), so learn the room afresh rather than judging a new spot by the old one's quiet level.
            line.last_seq = None
            line.judge = self._judge_for(line)
            line.series.clear()
        fresh = [r for r in rows if line.last_seq is None or r[0] > line.last_seq]
        if not fresh:
            return
        line.last_seq = int(fresh[-1][0])
        line.last_data = now
        # The board reports 4 per second, newest last; spread them back over the time they cover.
        for i, (seq, score, rate) in enumerate(r[:3] for r in fresh):
            ts = now - (len(fresh) - 1 - i) * 0.25
            line.rate = float(rate)
            self._record(ts, line, float(score), float(rate))
            if score >= 0:
                line.series.append((ts, float(score)))
            change = line.judge.feed(ts, float(score))
            if change == "started":
                app_logger.log("info", "wifi_motion_started", sensor=line.id, path=path_id(line),
                               name=self._path_name(path_id(line), [line]),
                               score=round(line.judge.smoothed, 2), baseline=round(line.judge.baseline or 0, 2),
                               component="wifi_motion", action="judge")
            elif change == "ended":
                app_logger.log("info", "wifi_motion_ended", sensor=line.id, path=path_id(line),
                               name=self._path_name(path_id(line), [line]),
                               seconds=round(line.judge.last_motion - line.judge.started, 1),
                               peak=round(line.judge.peak, 2), component="wifi_motion", action="judge")

    # ---- daily CSV of every score (for tuning) ----
    def _record(self, ts: float, line: Line, score: float, rate: float) -> None:
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
            self._csv.writerow([f"{ts:.2f}", line.id, f"{score:.2f}", f"{rate:.1f}",
                                line.rssi if line.rssi is not None else ""])
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
                boards = list(self._boards.values())
            for board in boards:
                self.poll(board)

    # ---- the web page and questions ----
    def _path_name(self, pid: str, lines: List[Line]) -> str:
        if pid in self._names:
            return self._names[pid]
        for line in lines:  # named before paths were joined up
            if line.id in self._names:
                return self._names[line.id]
        if len(lines) == 1 and lines[0].source == "router":
            return f"{self._board_name(lines[0].board.id)} to the router"
        a, b = pid.split("-", 1)
        return f"{self._board_name(a)} to {self._board_name(b)}"

    def _board_name(self, bid: str) -> str:
        board = self._boards.get(bid)
        return board.name if board and board.name else bid

    def rename_board(self, bid: str, name: str) -> Tuple[bool, str]:
        name = _clean_name(name)
        if not NAME_OK.match(name):
            return False, "Use up to 30 letters, numbers and spaces."
        with self._lock:
            board = self._boards.get(bid)
            if board is None:
                return False, "That board isn't known."
            board.name = name
            self._save()
        return True, "renamed"

    def identify(self, bid: str) -> Tuple[bool, str]:
        """Ask a board to flash its light for 20 s (which one is which?)."""
        with self._lock:
            board = self._boards.get(bid)
            ip = board.ip if board else ""
        if not ip:
            return False, "That board isn't known."
        try:
            with urllib.request.urlopen(f"http://{ip}/identify", timeout=3.0) as r:
                r.read(256)
        except OSError:
            return False, "That board didn't answer. Is it plugged in?"
        return True, "flashing"

    def _judge_for(self, line: Line) -> MotionJudge:
        return MotionJudge(ratio=self._ratios.get(path_id(line), RATIO))

    def _path_lines(self, pid: str) -> List[Line]:
        return [line for line in self._lines.values() if path_id(line) == pid]

    def set_ratio(self, pid: str, ratio: Any) -> Tuple[bool, str]:
        """The user moved a path's movement line (how many times its quiet level counts as movement)."""
        if not isinstance(ratio, (int, float)) or isinstance(ratio, bool) or not RATIO_MIN <= ratio <= RATIO_MAX:
            return False, f"Pick a movement line between {RATIO_MIN} and {RATIO_MAX} times the quiet level."
        ratio = round(float(ratio), 2)
        with self._lock:
            lines = self._path_lines(pid)
            if not lines:
                return False, "That sensor isn't known."
            if ratio == RATIO:
                self._ratios.pop(pid, None)
            else:
                self._ratios[pid] = ratio
            for line in lines:
                line.judge.ratio = ratio
            self._save()
        app_logger.log("info", "wifi_motion_ratio", path=pid, ratio=ratio, component="wifi_motion", action="adjust")
        return True, "saved"

    def relearn(self, pid: str) -> Tuple[bool, str]:
        """Forget a path's quiet level and learn it again from the next steady half minute."""
        with self._lock:
            lines = self._path_lines(pid)
            if not lines:
                return False, "That sensor isn't known."
            for line in lines:
                line.judge = self._judge_for(line)
        app_logger.log("info", "wifi_motion_relearn", path=pid, component="wifi_motion", action="adjust")
        return True, "relearning"

    def rename(self, pid: str, name: str) -> Tuple[bool, str]:
        name = _clean_name(name)
        if not NAME_OK.match(name):
            return False, "Use up to 30 letters, numbers and spaces."
        with self._lock:
            if not self._path_lines(pid):
                return False, "That sensor isn't known."
            self._names[pid] = name
            self._save()
        return True, "renamed"

    def view(self, now: Optional[float] = None) -> Dict[str, Any]:
        """Paths heard from in the last SHOW_AWAY_SECONDS (a router line goes quiet while other boards are heard),
        each with its ends ("directions") as measured by the board at that end."""
        now = time.time() if now is None else now
        paths: Dict[str, List[Line]] = {}
        out = []
        with self._lock:
            for line in sorted(self._lines.values(), key=lambda line: line.id):
                if now - line.last_data <= SHOW_AWAY_SECONDS:
                    paths.setdefault(path_id(line), []).append(line)
            for pid, lines in sorted(paths.items()):
                ends = []
                for s in lines:
                    j = s.judge
                    ends.append({
                        "id": s.id, "board": self._board_name(s.board.id),
                        "hears": "the router" if s.source == "router" else self._board_name(s.link),
                        "online": now - s.last_data < OFFLINE_AFTER, "motion": j.motion,
                        "score": None if j.smoothed is None else round(j.smoothed, 2),
                        "baseline": None if j.baseline is None else round(j.baseline, 2),
                        "threshold": None if j.threshold() is None else round(j.threshold(), 2),
                        "rssi": s.rssi, "rate": s.rate,
                        "series": [[round(t - now, 2), v] for t, v in s.series],
                    })
                live = [e for e in ends if e["online"]]
                last = [s.judge.last_motion for s in lines if s.judge.last_motion]
                batteries = [s.board.battery for s in lines if s.board.battery]
                out.append({
                    "id": pid, "name": self._path_name(pid, lines), "source": lines[0].source,
                    "online": bool(live), "motion": any(e["motion"] for e in live),
                    "last_motion": max(last) if last else None,
                    "learning": all(s.judge.baseline is None for s in lines),
                    "ratio": self._ratios.get(pid, RATIO), "default_ratio": RATIO,
                    "rssi": min((e["rssi"] for e in live if e["rssi"] is not None), default=None),
                    "rate": min((e["rate"] for e in live if e["rate"] is not None), default=None),
                    "battery": batteries[0] if batteries else None,
                    "ips": sorted({s.board.ip for s in lines if s.board.ip}),
                    "directions": ends,
                })
            boards = [{"id": b.id, "name": self._board_name(b.id), "named": bool(b.name), "ip": b.ip, "fw": b.fw,
                       "online": now - b.last_seen < OFFLINE_AFTER, "battery": b.battery}
                      for b in sorted(self._boards.values(), key=lambda b: (b.name or b.id).lower())]
        return {"sensors": out, "boards": boards}
