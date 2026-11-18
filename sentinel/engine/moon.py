"""
The moon for the weather tile's night sky: how much of it is lit, which side, and whether it's above the horizon.

Worked out here from the date and the weather's own rough coordinates (nothing is fetched): Paul Schlyter's
"How to compute planetary positions" with the moon's main perturbations, good to a few arc-minutes, far closer
than a picture on a tile needs. (2026-11-18: the tile drew the same thin crescent every clear night, even with
the moon past half and after it had set.)
"""

from __future__ import annotations

import math
import time
from typing import Dict, Optional, Tuple

RISE_ALTITUDE = -0.833  # degrees: the moon's centre here is "risen" (refraction plus its own half-width)


def _sin(deg: float) -> float:
    return math.sin(math.radians(deg))


def _cos(deg: float) -> float:
    return math.cos(math.radians(deg))


def _days(when: float) -> float:
    """Days since 2000 Jan 0.0 UT (Schlyter's day number)."""
    return when / 86400.0 + 2440587.5 - 2451543.5


def _sun(d: float) -> Tuple[float, float]:
    """(the sun's ecliptic longitude, its mean anomaly), degrees."""
    w, e, m = 282.9404 + 4.70935e-5 * d, 0.016709 - 1.151e-9 * d, (356.0470 + 0.9856002585 * d) % 360
    ecc = m + math.degrees(e * _sin(m) * (1 + e * _cos(m)))
    xv, yv = _cos(ecc) - e, math.sqrt(1 - e * e) * _sin(ecc)
    return (math.degrees(math.atan2(yv, xv)) + w) % 360, m


def _moon(d: float) -> Tuple[float, float, float]:
    """(ecliptic longitude, latitude in degrees, distance in Earth radii) of the moon, seen from Earth's centre."""
    n, inc, w = 125.1228 - 0.0529538083 * d, 5.1454, 318.0634 + 0.1643573223 * d
    a, e, m = 60.2666, 0.054900, (115.3654 + 13.0649929509 * d) % 360
    ecc = m + math.degrees(e * _sin(m) * (1 + e * _cos(m)))
    for _ in range(5):
        ecc -= (ecc - math.degrees(e * _sin(ecc)) - m) / (1 - e * _cos(ecc))
    xv, yv = a * (_cos(ecc) - e), a * math.sqrt(1 - e * e) * _sin(ecc)
    v, r = math.degrees(math.atan2(yv, xv)), math.hypot(xv, yv)
    xh = r * (_cos(n) * _cos(v + w) - _sin(n) * _sin(v + w) * _cos(inc))
    yh = r * (_sin(n) * _cos(v + w) + _cos(n) * _sin(v + w) * _cos(inc))
    zh = r * _sin(v + w) * _sin(inc)
    lon, lat = math.degrees(math.atan2(yh, xh)), math.degrees(math.atan2(zh, math.hypot(xh, yh)))
    _, ms = _sun(d)
    lm = n + w + m  # the moon's mean longitude
    dd, f = lm - (ms + 282.9404 + 4.70935e-5 * d), lm - n  # its elongation and argument of latitude
    lon += (-1.274 * _sin(m - 2 * dd) + 0.658 * _sin(2 * dd) - 0.186 * _sin(ms) - 0.059 * _sin(2 * m - 2 * dd)
            - 0.057 * _sin(m - 2 * dd + ms) + 0.053 * _sin(m + 2 * dd) + 0.046 * _sin(2 * dd - ms)
            + 0.041 * _sin(m - ms) - 0.035 * _sin(dd) - 0.031 * _sin(m + ms) - 0.015 * _sin(2 * f - 2 * dd)
            + 0.011 * _sin(m - 4 * dd))
    lat += (-0.173 * _sin(f - 2 * dd) - 0.055 * _sin(m - f - 2 * dd) - 0.046 * _sin(m + f - 2 * dd)
            + 0.033 * _sin(f + 2 * dd) + 0.017 * _sin(2 * m + f))
    r += -0.58 * _cos(m - 2 * dd) - 0.46 * _cos(2 * dd)
    return lon % 360, lat, r


def altitude(lat: float, lon: float, when: float) -> float:
    """The moon's height above the horizon (degrees, as seen from the ground) at lat/lon (east +)."""
    d = _days(when)
    mlon, mlat, r = _moon(d)
    ecl = 23.4393 - 3.563e-7 * d
    x, y, z = _cos(mlon) * _cos(mlat), _sin(mlon) * _cos(mlat), _sin(mlat)
    ye, ze = y * _cos(ecl) - z * _sin(ecl), y * _sin(ecl) + z * _cos(ecl)
    ra, dec = math.degrees(math.atan2(ye, x)), math.degrees(math.atan2(ze, math.hypot(x, ye)))
    _, ms = _sun(d)
    ut_hours = (when % 86400) / 3600.0
    hour_angle = (ms + 282.9404 + 4.70935e-5 * d + 180 + ut_hours * 15 + lon) - ra
    alt = math.degrees(math.asin(_sin(lat) * _sin(dec) + _cos(lat) * _cos(dec) * _cos(hour_angle)))
    return alt - math.degrees(math.asin(1 / r)) * _cos(alt)  # (it's close: seen from the surface it sits lower)


def phase(when: float) -> Tuple[float, bool]:
    """(the share of the moon's face that's lit, 0-1; True while it's growing toward full)."""
    d = _days(when)
    slon, _ = _sun(d)
    mlon, mlat, _ = _moon(d)
    lit = (1 - _cos(mlon - slon) * _cos(mlat)) / 2
    return lit, (mlon - slon) % 360 < 180


def moon(lat: Optional[float], lon: Optional[float], when: Optional[float] = None) -> Dict[str, object]:
    """{"lit": 0-1, "waxing": bool, "up": bool (None without a place)} for the weather tile."""
    when = time.time() if when is None else when
    lit, waxing = phase(when)
    up = None if lat is None or lon is None else altitude(float(lat), float(lon), when) > RISE_ALTITUDE
    return {"lit": round(lit, 3), "waxing": waxing, "up": up}
