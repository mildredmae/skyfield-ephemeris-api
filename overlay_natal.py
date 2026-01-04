from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple, Any
from datetime import datetime, timedelta, timezone

import swisseph as swe


# ----------------------------
# Config (keep aligned with main.py)
# ----------------------------

# Major aspects + orbs (LOCKED to spec)
ASPECT_SPECS = [
    ("conjunction", 0.0, 8.0),
    ("sextile", 60.0, 4.0),
    ("square", 90.0, 6.0),
    ("trine", 120.0, 6.0),
    ("opposition", 180.0, 8.0),
]

# "exact" threshold
# IMPORTANT: If your existing engine uses a different exact threshold, change this to match.
EXACT_ORB_DEG = 0.10

# Planet list:
# IMPORTANT: This must match whatever your /transits_range already returns per timestamp.
PLANETS = [
    ("Sun", swe.SUN),
    ("Moon", swe.MOON),
    ("Mercury", swe.MERCURY),
    ("Venus", swe.VENUS),
    ("Mars", swe.MARS),
    ("Jupiter", swe.JUPITER),
    ("Saturn", swe.SATURN),
    ("Uranus", swe.URANUS),
    ("Neptune", swe.NEPTUNE),
    ("Pluto", swe.PLUTO),
]


# ----------------------------
# Utilities
# ----------------------------

def _wrap_360(x: float) -> float:
    x = x % 360.0
    if x < 0:
        x += 360.0
    return x


def _min_angle_sep(a: float, b: float) -> float:
    """Smallest angular separation between two longitudes (0..180)."""
    d = abs(_wrap_360(a) - _wrap_360(b))
    return min(d, 360.0 - d)


def _dms(lon: float) -> str:
    """Normalized DMS string (no sec=60 spillover)."""
    lon = _wrap_360(lon)
    deg = int(lon)
    minutes_full = (lon - deg) * 60.0
    minute = int(minutes_full)
    sec_full = (minutes_full - minute) * 60.0
    sec = int(round(sec_full))

    # normalize
    if sec == 60:
        sec = 0
        minute += 1
    if minute == 60:
        minute = 0
        deg += 1
    deg = deg % 360

    return f"{deg}°{minute:02d}'{sec:02d}\""


def _sign(lon: float) -> str:
    signs = ["Aries","Taurus","Gemini","Cancer","Leo","Virgo","Libra","Scorpio","Sagittarius","Capricorn","Aquarius","Pisces"]
    idx = int(_wrap_360(lon) // 30)
    return signs[idx]


def _to_jd_ut(dt_utc: datetime) -> float:
    """Convert UTC datetime -> Julian Day (UT)."""
    if dt_utc.tzinfo is None:
        raise ValueError("dt_utc must be timezone-aware UTC datetime")
    dt_utc = dt_utc.astimezone(timezone.utc)
    return swe.julday(
        dt_utc.year,
        dt_utc.month,
        dt_utc.day,
        dt_utc.hour + dt_utc.minute/60.0 + dt_utc.second/3600.0
    )


def _calc_planets(dt_utc: datetime) -> Dict[str, Dict[str, Any]]:
    jd = _to_jd_ut(dt_utc)
    out: Dict[str, Dict[str, Any]] = {}
    for name, pid in PLANETS:
        lon, lat, dist, speed_lon = swe.calc_ut(jd, pid)[0]
        lon = float(lon)
        lat = float(lat)
        speed_lon = float(speed_lon)
        out[name] = {
            "lon": _wrap_360(lon),
            "lat": lat,
            "speed": speed_lon,
            "retrograde": speed_lon < 0,
            "sign": _sign(lon),
            "dms": _dms(lon),
        }
    return out


def _calc_houses(dt_utc: datetime, lat: float, lon: float, house_system: str) -> Dict[str, Any]:
    """
    Swiss Ephemeris houses.
    Support: whole_sign, porphyry, placidus.
    """
    jd = _to_jd_ut(dt_utc)

    hs_map = {
        "placidus": b'P',
        "porphyry": b'O',
        "whole_sign": b'W',
    }
    if house_system not in hs_map:
        raise ValueError(f"Unsupported house_system: {house_system}")

    cusps, ascmc = swe.houses_ex(jd, lat, lon, hs_map[house_system])

    house_cusps = [float(cusps[i]) for i in range(1, 13)]
    angles = {
        "asc": float(ascmc[0]),
        "mc": float(ascmc[1]),
        "armc": float(ascmc[2]),
        "vertex": float(ascmc[3]),
    }
    return {"house_cusps": house_cusps, "angles": angles}


def _house_of_lon(lon: float, cusps: List[float]) -> int:
    """
    Determine which house a longitude falls into given 12 cusp longitudes.
    Houses are [cusp_i, cusp_{i+1}) moving forward; wraps at 360 handled.
    """
    lon = _wrap_360(lon)
    c = [_wrap_360(x) for x in cusps]
    for i in range(12):
        start = c[i]
        end = c[(i + 1) % 12]
        if start <= end:
            if start <= lon < end:
                return i + 1
        else:
            if lon >= start or lon < end:
                return i + 1
    return 12


def compute_transit_natal_aspects(
    transit_lons: Dict[str, float],
    natal_lons: Dict[str, float],
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for tname, tlon in transit_lons.items():
        for nname, nlon in natal_lons.items():
            sep = _min_angle_sep(tlon, nlon)
            for asp_name, angle, orb_allow in ASPECT_SPECS:
                orb = abs(sep - angle)
                if orb <= orb_allow + 1e-9:
                    out.append({
                        "transit_planet": tname,
                        "natal_planet": nname,
                        "aspect": asp_name,
                        "angle": angle,
                        "orb": round(orb, 2),
                        "exact": orb <= EXACT_ORB_DEG,
                        "applying": None,  # filled later via lookahead
                    })
                    break
    return out


def _orb_for_pair_aspect(t_lon: float, n_lon: float, angle: float) -> float:
    sep = _min_angle_sep(t_lon, n_lon)
    return abs(sep - angle)


@dataclass(frozen=True)
class NatalInput:
    date: str  # YYYY-MM-DD
    time: str  # HH:MM (24h)
    tz: float  # hours offset from UTC, e.g. -5, 0, 1
    lat: float
    lon: float


def _parse_local_datetime(natal: NatalInput) -> datetime:
    y, m, d = [int(x) for x in natal.date.split("-")]
    hh, mm = [int(x) for x in natal.time.split(":")]
    offset = timedelta(hours=float(natal.tz))
    tzinfo = timezone(offset)
    return datetime(y, m, d, hh, mm, 0, tzinfo=tzinfo)


def build_natal_chart(
    natal: NatalInput,
    house_system: str,
) -> Dict[str, Any]:
    dt_local = _parse_local_datetime(natal)
    dt_utc = dt_local.astimezone(timezone.utc)

    planets = _calc_planets(dt_utc)
    natal_lons = {k: float(v["lon"]) for k, v in planets.items()}

    houses = _calc_houses(dt_utc, natal.lat, natal.lon, house_system)
    planet_houses = {p: _house_of_lon(lon, houses["house_cusps"]) for p, lon in natal_lons.items()}

    return {
        "meta": {
            "input_local": {
                "date": natal.date,
                "time": natal.time,
                "tz": natal.tz,
                "lat": natal.lat,
                "lon": natal.lon,
            },
            "computed_utc": dt_utc.isoformat().replace("+00:00", "Z"),
        },
        "house_system": house_system,
        "angles": houses["angles"],
        "house_cusps": houses["house_cusps"],
        "planets": planets,
        "planet_houses": planet_houses,
        "natal_longitudes": natal_lons,  # internal use
    }


def augment_transits_with_natal_overlay(
    transits: List[Dict[str, Any]],
    natal_chart: Dict[str, Any],
    include_natal_aspects: bool,
    include_house_activations: bool,
) -> List[Dict[str, Any]]:
    natal_lons: Dict[str, float] = natal_chart["natal_longitudes"]
    cusps: List[float] = natal_chart["house_cusps"]

    # Transit longitudes series
    transit_lons_series: List[Dict[str, float]] = []
    for row in transits:
        planets = row.get("planets", {})
        transit_lons_series.append({
            p: float(planets[p].get("lon", planets[p].get("lon_deg")))
            for p in planets.keys()
            if (("lon" in planets[p]) or ("lon_deg" in planets[p]))
        })

    natal_aspects_series: List[List[Dict[str, Any]]] = []
    house_act_series: List[List[Dict[str, Any]]] = []

    for i, row in enumerate(transits):
        t_lons = transit_lons_series[i]

        natal_aspects = compute_transit_natal_aspects(t_lons, natal_lons) if include_natal_aspects else []
        natal_aspects_series.append(natal_aspects)

        acts = [{"transit_planet": p, "natal_house": _house_of_lon(lon, cusps)} for p, lon in t_lons.items()] if include_house_activations else []
        house_act_series.append(acts)

    # Applying flag via lookahead
    if include_natal_aspects:
        for i in range(len(transits) - 1):
            t_lons_now = transit_lons_series[i]
            t_lons_next = transit_lons_series[i + 1]

            for rec in natal_aspects_series[i]:
                angle = float(rec["angle"])
                orb_now = _orb_for_pair_aspect(t_lons_now[rec["transit_planet"]], natal_lons[rec["natal_planet"]], angle)
                orb_next = _orb_for_pair_aspect(t_lons_next[rec["transit_planet"]], natal_lons[rec["natal_planet"]], angle)
                rec["applying"] = bool(orb_next < orb_now)

        # last timestamp: applying stays None (null in JSON)

    # Attach to rows
    for i, row in enumerate(transits):
        if include_natal_aspects:
            row["natal_aspects"] = natal_aspects_series[i]
        if include_house_activations:
            row["house_activations"] = house_act_series[i]

    return transits
