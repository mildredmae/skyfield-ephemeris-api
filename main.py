from fastapi import FastAPI
from pydantic import BaseModel
from skyfield.api import load, Topos
from datetime import datetime, timedelta
import math
import swisseph as swe

app = FastAPI(title="Skyfield Ephemeris API", version="1.1.0")

# ----------------------------
# Global loads (faster on Render)
# ----------------------------
ts = load.timescale()
eph = load("de440s.bsp")


# ----------------------------
# Models
# ----------------------------
class EphemerisRequest(BaseModel):
    date: str   # "YYYY-MM-DD" (LOCAL date)
    time: str   # "HH:MM" (LOCAL time)
    tz: float   # timezone offset from UTC (e.g., -5, -4, 0, 5.5)
    lat: float
    lon: float


class WesternChartRequest(BaseModel):
    date: str          # "YYYY-MM-DD" (LOCAL date)
    time: str          # "HH:MM" (LOCAL time)
    tz: float          # timezone offset from UTC (e.g., -5, -4, 0, 5.5)
    lat: float
    lon: float
    house_system: str  # "whole_sign" | "porphyry" | "placidus"


# ----------------------------
# Helpers
# ----------------------------
HOUSE_SYSTEM_MAP = {
    "whole_sign": "W",  # Whole Sign
    "porphyry": "O",    # Porphyry
    "placidus": "P",    # Placidus
}


def normalize_deg(deg: float) -> float:
    return deg % 360.0


def to_utc_datetime(date_str: str, time_str: str, tz_offset: float) -> datetime:
    """
    Convert local date+time + timezone offset into UTC datetime.

    tz_offset is "hours from UTC", e.g.:
      - New York standard: -5
      - New York DST: -4
      - London winter: 0
      - India: +5.5
    """
    local_dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
    utc_dt = local_dt - timedelta(hours=tz_offset)
    return utc_dt


def skyfield_time_from_utc(utc_dt: datetime):
    return ts.utc(utc_dt.year, utc_dt.month, utc_dt.day, utc_dt.hour, utc_dt.minute, utc_dt.second)


def zodiac_sign_index(lon_deg: float) -> int:
    # Aries=0 ... Pisces=11
    return int(lon_deg // 30)


def retrograde_flag(observer, body, t):
    """
    Approximate retrograde: compare longitude now vs 1 hour later.
    Good enough for API layer; AB can interpret nuances.
    """
    t2 = ts.utc(t.utc_datetime().year, t.utc_datetime().month, t.utc_datetime().day,
                t.utc_datetime().hour, t.utc_datetime().minute + 60)
    p1 = observer.at(t).observe(body).apparent().ecliptic_latlon()[0].degrees
    p2 = observer.at(t2).observe(body).apparent().ecliptic_latlon()[0].degrees
    # normalize delta to [-180, +180]
    delta = ((p2 - p1 + 540) % 360) - 180
    return delta < 0


def assign_house_quadrant(lonp: float, cusps_deg: list[float]) -> int:
    """
    Assign house by cusp intervals (quadrant systems).
    cusps_deg is length 12, cusp1..cusp12 in degrees.
    """
    for i in range(12):
        start = cusps_deg[i]
        end = cusps_deg[(i + 1) % 12]
        if start <= end:
            if start <= lonp < end:
                return i + 1
        else:
            # wrap-around (e.g. 350 -> 20)
            if lonp >= start or lonp < end:
                return i + 1
    return 12


# ----------------------------
# Existing endpoints (kept, but FIXED tz handling)
# ----------------------------
@app.post("/ephemeris")
def ephemeris(data: EphemerisRequest):
    """
    NOTE: Kept for backward compatibility.
    This endpoint returns basic ecliptic longitudes (Skyfield) and now properly respects tz.
    """
    utc_dt = to_utc_datetime(data.date, data.time, data.tz)
    t = skyfield_time_from_utc(utc_dt)

    observer = eph["earth"] + Topos(latitude_degrees=data.lat, longitude_degrees=data.lon)

    bodies = {
        "Sun": eph["sun"],
        "Moon": eph["moon"],
        "Mercury": eph["mercury barycenter"],
        "Venus": eph["venus barycenter"],
        "Mars": eph["mars barycenter"],
        "Jupiter": eph["jupiter barycenter"],
        "Saturn": eph["saturn barycenter"],
        "Uranus": eph["uranus barycenter"],
        "Neptune": eph["neptune barycenter"],
        "Pluto": eph["pluto barycenter"],
    }

    results = {}
    for name, body in bodies.items():
        ast_pos = observer.at(t).observe(body).apparent()
        lon, lat, _dist = ast_pos.ecliptic_latlon()
        results[name] = {
            "lon_deg": normalize_deg(lon.degrees),
            "lat_deg": lat.degrees,
        }

    return {
        "meta": {
            "input_local": {"date": data.date, "time": data.time, "tz": data.tz},
            "utc_datetime": utc_dt.isoformat(),
            "lat": data.lat,
            "lon": data.lon,
        },
        "bodies": results,
    }


@app.post("/extended_ephemeris")
def extended_ephemeris(data: EphemerisRequest):
    """
    NOTE: Kept for backward compatibility.
    This endpoint returns ecliptic longitude/latitude and an approximate retrograde flag.
    """
    utc_dt = to_utc_datetime(data.date, data.time, data.tz)
    t = skyfield_time_from_utc(utc_dt)

    observer = eph["earth"] + Topos(latitude_degrees=data.lat, longitude_degrees=data.lon)

    bodies = {
        "Sun": eph["sun"],
        "Moon": eph["moon"],
        "Mercury": eph["mercury barycenter"],
        "Venus": eph["venus barycenter"],
        "Mars": eph["mars barycenter"],
        "Jupiter": eph["jupiter barycenter"],
        "Saturn": eph["saturn barycenter"],
        "Uranus": eph["uranus barycenter"],
        "Neptune": eph["neptune barycenter"],
        "Pluto": eph["pluto barycenter"],
    }

    results = {}
    for name, body in bodies.items():
        ast_pos = observer.at(t).observe(body).apparent()
        lon, lat, _dist = ast_pos.ecliptic_latlon()
        results[name] = {
            "lon_deg": normalize_deg(lon.degrees),
            "lat_deg": lat.degrees,
            "retrograde": retrograde_flag(observer, body, t),
        }

    return {
        "meta": {
            "input_local": {"date": data.date, "time": data.time, "tz": data.tz},
            "utc_datetime": utc_dt.isoformat(),
            "lat": data.lat,
            "lon": data.lon,
        },
        "bodies": results,
    }


# ----------------------------
# New canonical endpoint for Astrology Bob
# ----------------------------
@app.post("/western_chart")
def western_chart(data: WesternChartRequest):
    """
    Canonical Western chart packet for Astrology Bob.

    Returns:
      - Planet ecliptic positions (Skyfield)
      - Retrograde flags (approx)
      - House cusps + ASC/MC (Swiss Ephemeris houses)
      - Planet -> house mapping for:
          * Whole Sign
          * Porphyry
          * Placidus
    """

    if data.house_system not in HOUSE_SYSTEM_MAP:
        return {
            "error": f"Unsupported house_system '{data.house_system}'. Use one of {list(HOUSE_SYSTEM_MAP.keys())}."
        }

    hsys = HOUSE_SYSTEM_MAP[data.house_system]

    # 1) Convert local -> UTC
    utc_dt = to_utc_datetime(data.date, data.time, data.tz)
    t = skyfield_time_from_utc(utc_dt)

    # 2) Observer
    observer = eph["earth"] + Topos(latitude_degrees=data.lat, longitude_degrees=data.lon)

    # 3) Planet positions (Skyfield)
    bodies = {
        "Sun": eph["sun"],
        "Moon": eph["moon"],
        "Mercury": eph["mercury barycenter"],
        "Venus": eph["venus barycenter"],
        "Mars": eph["mars barycenter"],
        "Jupiter": eph["jupiter barycenter"],
        "Saturn": eph["saturn barycenter"],
        "Uranus": eph["uranus barycenter"],
        "Neptune": eph["neptune barycenter"],
        "Pluto": eph["pluto barycenter"],
    }

    planets = {}
    for name, body in bodies.items():
        ast_pos = observer.at(t).observe(body).apparent()
        lon, lat, _dist = ast_pos.ecliptic_latlon()
        planets[name] = {
            "lon_deg": normalize_deg(lon.degrees),
            "lat_deg": lat.degrees,
            "retrograde": retrograde_flag(observer, body, t),
        }

    # 4) Houses (Swiss Ephemeris)
    # Swiss needs Julian Day (UT)
    jd_ut = swe.julday(
        utc_dt.year,
        utc_dt.month,
        utc_dt.day,
        utc_dt.hour + utc_dt.minute / 60.0 + utc_dt.second / 3600.0
    )

    try:
        cusps, ascmc = swe.houses(jd_ut, data.lat, data.lon, hsys)
    except Exception as e:
        return {
            "error": "Swiss Ephemeris house calculation failed.",
            "detail": str(e),
            "note": "This service uses Swiss Ephemeris only for house cusps/angles. Planet positions are computed with Skyfield.",
        }

    # cusps is 1..12 (1-based)
    house_cusps = [normalize_deg(cusps[i]) for i in range(1, 13)]

    # ascmc: [ASC, MC, ...] (standard in pyswisseph)
    asc = normalize_deg(ascmc[0])
    mc = normalize_deg(ascmc[1])

    # 5) Planet -> house assignment
    planet_houses = {}

    if data.house_system == "whole_sign":
        asc_sign = zodiac_sign_index(asc)
        for p, vals in planets.items():
            p_sign = zodiac_sign_index(vals["lon_deg"])
            planet_houses[p] = ((p_sign - asc_sign) % 12) + 1
    else:
        for p, vals in planets.items():
            planet_houses[p] = assign_house_quadrant(vals["lon_deg"], house_cusps)

    return {
        "meta": {
            "input_local": {"date": data.date, "time": data.time, "tz": data.tz},
            "utc_datetime": utc_dt.isoformat(),
            "lat": data.lat,
            "lon": data.lon,
            "house_system": data.house_system,
        },
        "angles": {
            "asc_deg": asc,
            "mc_deg": mc,
        },
        "houses": {
            "cusps_deg": house_cusps,
        },
        "planets": planets,
        "planet_houses": planet_houses,
    }
