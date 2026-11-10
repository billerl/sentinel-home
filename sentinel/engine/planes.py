"""
Planes near the house: "what plane is that?", "any planes around?", and a map of them on the TV.

Positions come from adsb.lol, a free community network of home ADS-B receivers (planes broadcast where they are
about once a second); a USB receiver of our own could feed the same answers later. Where a flight came from and
is going comes from adsbdb.com by its flight number. Airlines reuse a number for several legs, so the looked-up
route is often another leg (on 2026-11-10, "Southwest 3232: Los Angeles to San Francisco" for a 737 over Ohio):
a route is only told when the plane really is between those two airports and heading for the second. Private
planes file no public route; for them it's just what they are.
"""

from __future__ import annotations

import contextvars
import json
import math
import re
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional, Tuple

from sentinel.logging_utils import app_logger

POSITIONS_URL = "https://api.adsb.lol/v2/point/{lat:.4f}/{lon:.4f}/{radius}"
ROUTE_URL = "https://api.adsbdb.com/v0/callsign/{callsign}"
USER_AGENT = "SentinelHome/1.0 (self-hosted home security; https://github.com/billerl/sentinel-home)"
MAP_RADIUS_NM = 40  # what the TV map shows (and what's fetched)
ASK_RADIUS_NM = 25  # "any planes around?"
OVERHEAD_NM = 12  # "what plane is that?": closer than this counts as the one being asked about
POSITIONS_FRESH = 10.0  # seconds a fetch is reused
AWAY_NM = 5.0  # someone asking from farther than this from home hears about the planes over them instead
# Where the person asking is, while their question is answered (their phone's position: Telegram, the Talk button, the
# Planes page); None = the house. Set by whoever takes the question (Engine.answer_question, the Talk route).
ASKER: "contextvars.ContextVar[Optional[Tuple[float, float]]]" = contextvars.ContextVar("planes_asker", default=None)
ROUTE_KEEP = 6 * 3600.0
NM_TO_MILES = 1.15078
EARTH_NM = 3440.065
# ICAO type codes people would rather hear as names (the common ones over the US); others: the code's maker, if known.
TYPE_NAMES = {
    "A19N": "Airbus A319neo", "A20N": "Airbus A320neo", "A21N": "Airbus A321neo", "A318": "Airbus A318",
    "A319": "Airbus A319", "A320": "Airbus A320", "A321": "Airbus A321", "A332": "Airbus A330", "A333": "Airbus A330",
    "A339": "Airbus A330neo", "A343": "Airbus A340", "A346": "Airbus A340", "A359": "Airbus A350", "A35K": "Airbus A350",
    "A388": "Airbus A380", "BCS1": "Airbus A220", "BCS3": "Airbus A220",
    "B712": "Boeing 717", "B733": "Boeing 737", "B734": "Boeing 737", "B735": "Boeing 737", "B736": "Boeing 737",
    "B737": "Boeing 737", "B738": "Boeing 737", "B739": "Boeing 737", "B37M": "Boeing 737 MAX", "B38M": "Boeing 737 MAX",
    "B39M": "Boeing 737 MAX", "B3XM": "Boeing 737 MAX", "B744": "Boeing 747", "B748": "Boeing 747", "B752": "Boeing 757",
    "B753": "Boeing 757", "B762": "Boeing 767", "B763": "Boeing 767", "B764": "Boeing 767", "B772": "Boeing 777",
    "B773": "Boeing 777", "B77L": "Boeing 777", "B77W": "Boeing 777", "B788": "Boeing 787 Dreamliner",
    "B789": "Boeing 787 Dreamliner", "B78X": "Boeing 787 Dreamliner", "MD11": "MD-11", "DC10": "DC-10",
    "CRJ2": "CRJ-200", "CRJ7": "CRJ-700", "CRJ9": "CRJ-900", "CRJX": "CRJ-1000", "E135": "Embraer 135",
    "E145": "Embraer 145", "E170": "Embraer 170", "E75L": "Embraer 175", "E75S": "Embraer 175", "E190": "Embraer 190",
    "E195": "Embraer 195", "E290": "Embraer E190-E2", "DH8D": "Dash 8", "AT72": "ATR 72", "AT76": "ATR 72",
    "DHC7": "Dash 7", "C130": "C-130 Hercules", "C30J": "C-130 Hercules", "C17": "C-17", "KC135": "KC-135",
    "K35R": "KC-135 tanker", "C5M": "C-5 Galaxy",
    "C150": "Cessna 150", "C152": "Cessna 152", "C162": "Cessna Skycatcher", "C170": "Cessna 170", "C172": "Cessna 172",
    "C177": "Cessna Cardinal", "C182": "Cessna 182", "C206": "Cessna 206", "C208": "Cessna Caravan", "C210": "Cessna 210",
    "C310": "Cessna 310", "C340": "Cessna 340", "C414": "Cessna 414", "C421": "Cessna 421", "C510": "Cessna Citation Mustang",
    "C525": "Cessna Citation", "C25A": "Cessna Citation", "C25B": "Cessna Citation", "C25C": "Cessna Citation",
    "C560": "Cessna Citation", "C56X": "Cessna Citation Excel", "C680": "Cessna Citation Sovereign",
    "C68A": "Cessna Citation Latitude", "C700": "Cessna Citation Longitude", "C750": "Cessna Citation X",
    "P28A": "Piper Cherokee", "P28B": "Piper Cherokee", "P28R": "Piper Arrow", "PA32": "Piper Saratoga",
    "PA34": "Piper Seneca", "PA44": "Piper Seminole", "PA46": "Piper Malibu", "P46T": "Piper Meridian",
    "PA18": "Piper Super Cub", "J3": "Piper Cub", "SR20": "Cirrus SR20", "SR22": "Cirrus SR22", "S22T": "Cirrus SR22",
    "SF50": "Cirrus Vision Jet", "BE20": "Beechcraft King Air", "BE30": "Beechcraft King Air", "BE35": "Beechcraft Bonanza",
    "BE36": "Beechcraft Bonanza", "BE58": "Beechcraft Baron", "BE9L": "Beechcraft King Air", "B350": "Beechcraft King Air",
    "M20P": "Mooney", "M20T": "Mooney", "DA40": "Diamond DA40", "DA42": "Diamond DA42", "PC12": "Pilatus PC-12",
    "PC24": "Pilatus PC-24", "TBM7": "TBM turboprop", "TBM8": "TBM turboprop", "TBM9": "TBM turboprop",
    "AT8T": "Air Tractor crop duster", "AT5T": "Air Tractor crop duster", "AT6T": "Air Tractor crop duster",
    "CL30": "Challenger 300", "CL35": "Challenger 350", "CL60": "Challenger 600", "GLF4": "Gulfstream IV",
    "GLF5": "Gulfstream V", "GLF6": "Gulfstream G650", "GLEX": "Bombardier Global Express", "GL5T": "Bombardier Global",
    "GL7T": "Bombardier Global 7500", "LJ35": "Learjet 35", "LJ45": "Learjet 45", "LJ60": "Learjet 60", "LJ75": "Learjet 75",
    "E50P": "Embraer Phenom 100", "E55P": "Embraer Phenom 300", "E545": "Embraer Legacy", "E550": "Embraer Praetor",
    "HDJT": "HondaJet", "FA20": "Falcon 20", "FA50": "Falcon 50", "F900": "Falcon 900", "FA7X": "Falcon 7X",
    "F2TH": "Falcon 2000", "H25B": "Hawker 800", "PRM1": "Beechcraft Premier",
    "R22": "Robinson R22 helicopter", "R44": "Robinson R44 helicopter", "R66": "Robinson R66 helicopter",
    "EC35": "Airbus EC135 helicopter", "EC45": "Airbus EC145 helicopter", "EC30": "Airbus H130 helicopter",
    "AS50": "Airbus AStar helicopter", "B06": "Bell 206 helicopter", "B407": "Bell 407 helicopter",
    "B429": "Bell 429 helicopter", "A109": "AW109 helicopter", "A119": "AW119 helicopter", "S76": "Sikorsky S-76 helicopter",
    "H60": "Black Hawk helicopter", "UH60": "Black Hawk helicopter",
}
DIRECTIONS = ("north", "northeast", "east", "southeast", "south", "southwest", "west", "northwest")


def distance_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2, dl = math.radians(lat1), math.radians(lat2), math.radians(lon2 - lon1)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_NM * math.asin(min(1.0, math.sqrt(a)))


def bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2, dl = math.radians(lat1), math.radians(lat2), math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def compass(degrees: float) -> str:
    return DIRECTIONS[int((degrees % 360 + 22.5) // 45) % 8]


def route_fits(plane: Dict[str, Any], route: Dict[str, Any]) -> bool:
    """Is the plane on its way from the route's first airport to its second? Off the line by more than a little,
    or flying away from the destination, means the flight number was looked up for another leg."""
    lat, lon, o, d = plane["lat"], plane["lon"], route["from"], route["to"]
    if None in (o.get("lat"), o.get("lon"), d.get("lat"), d.get("lon")):
        return False
    whole = distance_nm(o["lat"], o["lon"], d["lat"], d["lon"])
    via = distance_nm(o["lat"], o["lon"], lat, lon) + distance_nm(lat, lon, d["lat"], d["lon"])
    if via - whole > max(60.0, 0.15 * whole):
        return False
    to_go = distance_nm(lat, lon, d["lat"], d["lon"])
    track = plane.get("track")
    if track is None or to_go < 30:  # (circling to land: any heading)
        return True
    off = abs((track - bearing(lat, lon, d["lat"], d["lon"]) + 180) % 360 - 180)
    return off < 60


def _feet(alt: Any) -> str:
    if alt == "ground":
        return "on the ground"
    try:
        ft = float(alt)
    except (TypeError, ValueError):
        return ""
    ft = round(ft, -2) if ft < 10000 else round(ft, -3)
    return f"{int(ft):,} feet up"


class Planes:
    def __init__(self, home: Callable[[], Optional[Tuple[float, float]]],
                 get_json: Optional[Callable[[str], Any]] = None, clock: Callable[[], float] = time.time) -> None:
        self.home = home
        self._get = get_json or self._http_json
        self._clock = clock
        self._lock = threading.Lock()
        self._positions: Dict[Tuple[float, float], Tuple[float, List[Dict[str, Any]]]] = {}  # spot -> (when, planes)
        self._routes: Dict[str, Tuple[float, Optional[Dict[str, Any]]]] = {}  # callsign -> (when, route or None)
        self._pool = ThreadPoolExecutor(4, thread_name_prefix="planes")

    @staticmethod
    def _http_json(url: str) -> Any:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.loads(r.read(4 << 20))

    # ------------------------------------------------------------ data
    def center(self, at: Optional[Tuple[float, float]] = None) -> Tuple[Tuple[float, float], bool]:
        """Where to look: (the spot, whether it's the asker away from home). The asker's position (``at``, else
        ASKER) when they're more than a few miles from home; else the house. ValueError when neither is known."""
        home, at = self.home(), at or ASKER.get()
        if at and (not home or distance_nm(home[0], home[1], at[0], at[1]) > AWAY_NM):
            return (float(at[0]), float(at[1])), True
        if home:
            return (float(home[0]), float(home[1])), False
        raise ValueError("no home")

    def nearby(self, spot: Optional[Tuple[float, float]] = None) -> List[Dict[str, Any]]:
        """Planes within MAP_RADIUS_NM of the spot (default: center()), nearest overhead first (ground distance and
        height together). Raises OSError when the flight tracker can't be reached, ValueError when the house's
        place isn't known."""
        home = spot or self.center()[0]
        key = (round(home[0], 2), round(home[1], 2))  # (~1 km: the TV's map and a phone at home share a fetch)
        now = self._clock()
        with self._lock:
            got = self._positions.get(key)
            if got and now - got[0] < POSITIONS_FRESH:
                return got[1]
        try:
            data = self._get(POSITIONS_URL.format(lat=home[0], lon=home[1], radius=MAP_RADIUS_NM))
        except (OSError, ValueError) as e:
            app_logger.log("info", "planes_unreachable", error_message=str(e)[:200], component="planes", action="fetch")
            raise OSError("the flight tracker didn't answer") from e
        planes = [p for p in (self._plane(a, home) for a in (data or {}).get("ac") or []) if p]
        planes.sort(key=lambda p: p["slant_nm"])
        self._find_routes(planes)
        with self._lock:
            self._positions = {k: v for k, v in self._positions.items() if now - v[0] < 60}  # (phones on the move)
            self._positions[key] = (self._clock(), planes)
        return planes

    @staticmethod
    def _plane(a: Dict[str, Any], home: Tuple[float, float]) -> Optional[Dict[str, Any]]:
        lat, lon = a.get("lat"), a.get("lon")
        if lat is None or lon is None:
            return None
        dist = distance_nm(home[0], home[1], lat, lon)
        alt = a.get("alt_baro", a.get("alt_geom"))
        try:
            alt_nm = 0.0 if alt == "ground" else float(alt) / 6076.0
        except (TypeError, ValueError):
            alt_nm = 0.0
        callsign = re.sub(r"[^A-Z0-9]", "", (a.get("flight") or "").upper())
        code = (a.get("t") or "").upper()
        kind = TYPE_NAMES.get(code) or ("helicopter" if a.get("category") == "A7" else code)
        return {
            "id": a.get("hex") or callsign, "callsign": callsign, "reg": (a.get("r") or "").strip(),
            "kind": kind, "military": bool(int(a.get("dbFlags") or 0) & 1),
            "lat": lat, "lon": lon, "alt": alt, "track": a.get("track"), "speed_kt": a.get("gs"),
            "dist_nm": dist, "dir": compass(bearing(home[0], home[1], lat, lon)),
            "slant_nm": math.hypot(dist, alt_nm), "route": None, "airline": None,
        }

    def _find_routes(self, planes: List[Dict[str, Any]]) -> None:
        """Each airline-style flight number's route (looked up once per few hours), kept only where it fits."""
        wanted = {p["callsign"] for p in planes if re.fullmatch(r"[A-Z]{3}\d{1,4}[A-Z]{0,2}", p["callsign"])}
        now = self._clock()
        with self._lock:
            todo = [c for c in wanted if c not in self._routes or now - self._routes[c][0] > ROUTE_KEEP]
        for callsign, route in zip(todo, self._pool.map(self._lookup_route, todo)):
            with self._lock:
                self._routes[callsign] = (now, route)
        with self._lock:
            routes = {c: self._routes[c][1] for c in wanted if c in self._routes}
        for p in planes:
            route = routes.get(p["callsign"])
            if route:
                p["airline"] = route.get("airline")
                if route_fits(p, route):
                    p["route"] = route

    def _lookup_route(self, callsign: str) -> Optional[Dict[str, Any]]:
        try:
            data = self._get(ROUTE_URL.format(callsign=urllib.parse.quote(callsign)))
        except (OSError, ValueError):
            return None
        fr = (data or {}).get("response")
        fr = fr.get("flightroute") if isinstance(fr, dict) else None
        if not isinstance(fr, dict):
            return None

        def airport(a: Any) -> Dict[str, Any]:
            a = a if isinstance(a, dict) else {}
            return {"city": a.get("municipality") or a.get("name") or "", "code": a.get("iata_code") or a.get("icao_code") or "",
                    "lat": a.get("latitude"), "lon": a.get("longitude")}

        airline = fr.get("airline") if isinstance(fr.get("airline"), dict) else {}
        return {"from": airport(fr.get("origin")), "to": airport(fr.get("destination")), "airline": airline.get("name")}

    # ------------------------------------------------------------ words
    @staticmethod
    def describe(p: Dict[str, Any], lead: bool = True) -> str:
        """'Delta flight 1776, an Airbus A321 from Tampa to Detroit, 11,000 feet up, 6 miles northwest'."""
        kind = p["kind"] or "plane"
        article = "an" if kind[:1].upper() in "AEIOU" else "a"
        if p["military"]:
            who = f"a military {kind}" if p["kind"] else "a military plane"
        elif p["airline"] and p["callsign"]:
            number = re.sub(r"^[A-Z]{3}", "", p["callsign"])
            who = f"{p['airline']} flight {number}" + (f", {article} {kind}" if p["kind"] else "")
        elif p["reg"] or p["callsign"]:  # (a private plane's call sign is its registration, N622DJ)
            who = f"{article} {kind} ({p['reg'] or p['callsign']})"
        else:
            who = f"{article} {kind}"
        parts = [who]
        if p["route"]:
            parts[0] += f" from {p['route']['from']['city']} to {p['route']['to']['city']}"
        feet = _feet(p["alt"])
        if feet:
            parts.append(feet)
        miles = p["dist_nm"] * NM_TO_MILES
        parts.append(("close by" if p["alt"] == "ground" else "right overhead") if miles < 1 else f"{miles:.0f} miles {p['dir']}" if miles >= 2
                     else f"about a mile {p['dir']}")
        text = ", ".join(parts)
        return text[0].upper() + text[1:] if lead else text

    def answer(self, kind: str) -> str:
        """kind "that" (the plane overhead) or "list" (what's around)."""
        try:
            spot, away = self.center()
            planes = self.nearby(spot)
        except ValueError:
            return "I don't know where the house is yet: save Home on the Family page first."
        except OSError:
            return "I couldn't reach the flight tracker just now. Try again in a minute."
        # (planes parked or taxiing at an airport aren't what anyone looks up at: only when nothing is flying)
        near = [p for p in planes if p["dist_nm"] <= ASK_RADIUS_NM]
        near = [p for p in near if p["alt"] != "ground"] or near
        of = " of you" if away else ""
        if not near:
            return f"I don't see any planes within {ASK_RADIUS_NM * NM_TO_MILES:.0f} miles{of} right now."
        if kind == "that":
            first = near[0]
            said = ("That's probably " if first["dist_nm"] <= OVERHEAD_NM else
                    f"Nothing's right over {'you' if away else 'the house'}. The closest is ") \
                + self.describe(first, lead=False) + "."
            if first["airline"] and not first["route"] and not first["military"]:
                said += " I couldn't confirm where it's headed."
            return said
        count = len(near)
        lines = [f"{count} plane{'s' if count != 1 else ''} within {ASK_RADIUS_NM * NM_TO_MILES:.0f} miles{of}. "
                 + ("The closest:" if count > 1 else "")]
        lines += [f"• {self.describe(p)}" for p in near[:3]]
        return "\n".join(lines)

    def map_view(self, at: Optional[Tuple[float, float]] = None) -> Dict[str, Any]:
        """The plane map: every plane with what it is, around the house (a TV) or around ``at`` (a phone's own
        position, when it's away from home)."""
        home = self.home()
        spot, away = None, False
        try:
            spot, away = self.center(at)
            planes = self.nearby(spot)
            error = None
        except ValueError:
            planes, error = [], "Save Home on the Family page first."
        except OSError:
            planes, error = [], "The flight tracker isn't answering."
        out = []
        for p in planes:
            route = p["route"]
            out.append({"id": p["id"], "lat": p["lat"], "lon": p["lon"], "track": p["track"],
                        "tag": p["callsign"] or p["reg"] or p["kind"] or "?", "feet": _feet(p["alt"]),
                        "low": isinstance(p["alt"], (int, float)) and p["alt"] < 5000 or p["alt"] == "ground",
                        "text": self.describe(p),
                        "route": f"{route['from']['code'] or route['from']['city']} → {route['to']['code'] or route['to']['city']}"
                        if route else ""})
        return {"home": list(home) if home else None, "center": list(spot) if spot else None, "away": away,
                "planes": out, "radius_nm": MAP_RADIUS_NM, "error": error, "now": self._clock()}


# ------------------------------------------------------------ recognising the question
_PLANE = r"(?:air ?planes?|planes?|aircraft|jets?|helicopters?|choppers?)"
_SHOW = re.compile(r"^(?:please )?(?:can you |could you )?(?:show|put|bring|pull|open|display)\b", re.IGNORECASE)
_THAT = re.compile(r"^(?:what|which|whose|who)(?:'s| is| was|'re| are)? (?:that|this|the) " + _PLANE
                   + r"|^(?:what|which|whose)(?: kind of| type of)? " + _PLANE + r" (?:is|was) (?:that|this|it|overhead|flying|going)"
                   + r"|^(?:what|which) flight is (?:that|this|overhead)"
                   + r"|^(?:what|who)(?:'s| is) (?:flying |that )?(?:over(?:head)?|above)(?: (?:us|me|the house))?$"
                   + r"|^(?:what|who)(?:'s| is) (?:that )?flying (?:over|overhead|above|by|low)", re.IGNORECASE)
_LIST = re.compile(r"^(?:are there |is there |any |how many |what |which |list |tell me (?:about )?(?:the )?)"
                   r"(?:any |the )?" + _PLANE + r"(?: are)? (?:around|nearby|near (?:us|here|the house)|overhead|"
                   r"in the (?:area|sky)|flying (?:around|over|nearby)|above (?:us|the house)|close by|out there|up there)"
                   r"|^(?:what|any)(?: other)? " + _PLANE + r"$|^(?:what|which) planes are (?:there|up|flying)",
                   re.IGNORECASE)


def plane_question(text: str) -> Optional[str]:
    """"what plane is that?" -> "that"; "any planes around?" -> "list"; None for anything else ("show the planes
    on the TV" is the TV's)."""
    t = re.sub(r"\s+", " ", re.sub(r"[?!.,]+", " ", text or "")).strip()
    t = re.sub(r"^(?:hey |ok |okay )?(?:jarvis|sentinel)\s*", "", t, flags=re.IGNORECASE)
    if not t or _SHOW.match(t):
        return None
    if _THAT.match(t):
        return "that"
    if _LIST.match(t):
        return "list"
    return None


def plane_map_words(words: str) -> bool:
    """"the planes", "airplanes overhead", "the flight map" (after "show me", for the TV)."""
    w = re.sub(r"\s+", " ", (words or "").lower()).strip()
    return bool(re.fullmatch(r"(?:the |all the |any )?(?:" + _PLANE + r"|flights?|air traffic|flight map|plane map|"
                             r"flight tracker|flightradar)(?: (?:around|nearby|overhead|in the area|above us|map))?", w))
