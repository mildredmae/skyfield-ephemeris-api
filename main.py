from fastapi import FastAPI
from pydantic import BaseModel
from skyfield.api import load, Topos
from datetime import datetime, timedelta
import os
import swisseph as swe

app = FastAPI(title="Skyfield Ephemeris API", version="1.2.0")

# ----------------------------
# Global loads (faster on Render)
# ----------------------------
ts = load.timescale()
eph = load("de440s.bsp")

# Swiss ephemeris path (where optional data files can live)
# If you don't set SWEPH_PATH, we default to current directory.
SWEPH_PATH = os.getenv("SWEPH_PATH", ".")
swe.set_ephe_path(SWEPH_PATH)

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
    """
    dt1 = t.utc_datetime()
    dt2 = dt1 + timedelta(hours=1)
    t2 = ts.utc(dt2.year, dt2.month, dt2.day, dt2.hour, dt2.minute, dt2.second)

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


def swe_julday_ut(utc_dt: datetime) -> float:
    return swe.julday(
        utc_dt.year,
        utc_dt.month,
        utc_dt.day,
        utc_dt.hour + utc_dt.minute / 60.0 + utc_dt.second / 3600.0
    )


def swe_calc_lon(jd_ut: float, body_id: int):
    """
    Swiss Ephemeris longitude helper with safe error handling.
    Returns (lon_deg, err=None) or (None, err=str)
    """
    try:
        res, _flag = swe.calc_ut(jd_ut, body_id)
        lon = normalize_deg(res[0])
        return lon, None
    except Exception as e:
        return None, str(e)


def swe_fixstar_lon(jd_ut: float, star_name: str):
    """
    Fixed-star longitude helper.
    Returns (lon_deg, err=None) or (None, err=str)
    """
    try:
        # fixstar2_ut returns (starpos, retflag)
        # starpos[0] is ecliptic longitude in degrees
        starpos, _retflag = swe.fixstar2_ut(star_name, jd_ut)
        lon = normalize_deg(starpos[0])
        return lon, None
    except Exception as e:
        return None, str(e)


# ----------------------------
# Existing endpoints (kept, timezone fixed)
# ----------------------------
@app.post("/ephemeris")
def ephemeris_endpoint(data: EphemerisRequest):
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
def extended_ephemeris_endpoint(data: EphemerisRequest):
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
# Canonical endpoint for Astrology Bob (Phase 2)
# ----------------------------
@app.post("/western_chart")
def western_chart(data: WesternChartRequest):
    """
    Canonical Western chart packet for Astrology Bob.

    Phase 2 adds:
      - Nodes (mean/true + derived north/south)
      - Lilith (mean apogee), Lilith (i) (interpolated apogee), Priapus (i)
      - Vertex (from ascmc)
      - Part of Fortune (day/night formula)
      - Optional asteroids/TNOs/fixed stars with graceful fallback
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
    skyfield_bodies = {
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
    for name, body in skyfield_bodies.items():
        ast_pos = observer.at(t).observe(body).apparent()
        lon, lat, _dist = ast_pos.ecliptic_latlon()
        planets[name] = {
            "lon_deg": normalize_deg(lon.degrees),
            "lat_deg": lat.degrees,
            "retrograde": retrograde_flag(observer, body, t),
        }

    # 4) Houses + angles (Swiss Ephemeris)
    jd_ut = swe_julday_ut(utc_dt)

    try:
        cusps, ascmc = swe.houses(jd_ut, data.lat, data.lon, hsys)
    except Exception as e:
        return {
            "error": "Swiss Ephemeris house calculation failed.",
            "detail": str(e),
            "note": "Planet positions are computed with Skyfield. Houses/angles require Swiss Ephemeris.",
        }

    house_cusps = [normalize_deg(cusps[i]) for i in range(1, 13)]

    asc = normalize_deg(ascmc[0])
    mc = normalize_deg(ascmc[1])

    # Vertex is typically ascmc[3] in pyswisseph
    vertex = None
    try:
        vertex = normalize_deg(ascmc[3])
    except Exception:
        vertex = None

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

    # 6) Swiss-calculated points (Nodes, Lilith variants)
    points = {}
    points_unavailable = {}

    # Mean Node / True Node
    mean_node, err = swe_calc_lon(jd_ut, swe.MEAN_NODE)
    if err:
        points_unavailable["Mean Node"] = err
    else:
        points["Mean Node"] = mean_node
        points["North Node"] = mean_node
        points["South Node"] = normalize_deg(mean_node + 180.0)

    true_node, err = swe_calc_lon(jd_ut, swe.TRUE_NODE)
    if err:
        points_unavailable["True Node"] = err
    else:
        points["True Node"] = true_node

    # Lilith = Mean Apogee
    lilith_mean, err = swe_calc_lon(jd_ut, swe.MEAN_APOG)
    if err:
        points_unavailable["Lilith"] = err
    else:
        points["Lilith"] = lilith_mean

    # Lilith (i) = Interpolated Apogee
    lilith_i, err = swe_calc_lon(jd_ut, swe.INTP_APOG)
    if err:
        points_unavailable["Lilith (i)"] = err
        points_unavailable["Priapus (i)"] = err
    else:
        points["Lilith (i)"] = lilith_i
        points["Priapus (i)"] = normalize_deg(lilith_i + 180.0)

    # 7) Part of Fortune (using selected house system)
    # Day chart if Sun is above horizon; we approximate using house placement in chosen system.
    sun_house = planet_houses.get("Sun")
    is_day_chart = True
    if sun_house is not None:
        # Houses 7-12 = above horizon (day)
        is_day_chart = sun_house >= 7

    sun_lon = planets["Sun"]["lon_deg"]
    moon_lon = planets["Moon"]["lon_deg"]

    if is_day_chart:
        fortune = normalize_deg(asc + moon_lon - sun_lon)
    else:
        fortune = normalize_deg(asc + sun_lon - moon_lon)

    points["Fortune"] = fortune

    # 8) Optional bodies that require Swiss data files (asteroids/TNOs/fixed stars)
    # If the required Swiss files aren't present, we return them as unavailable with error details.
    # Asteroid IDs use swe.AST_OFFSET + object_number
    asteroid_map = {
        # Asteroids / centaurs
        "Ceres": 1,
        "Pallas": 2,
        "Juno": 3,
        "Vesta": 4,
        "Chiron": 2060,
        "Pholus": 5145,

        # TNOs / dwarf planets / KBOs
        "Eris": 136199,
        "Haumea": 136108,
        "Ixion": 28978,
        "Makemake": 136472,
        "Orcus": 90482,
        "Quaoar": 50000,
        "Sedna": 90377,
        "Varuna": 20000,
    }

    for name, num in asteroid_map.items():
        lon, err = swe_calc_lon(jd_ut, swe.AST_OFFSET + num)
        if err:
            points_unavailable[name] = err
        else:
            points[name] = lon

    # Fixed stars (Swiss star catalog required)
    # NOTE: Star naming can be picky; if these don't resolve, the error will tell you.
    star_map = {
        "Aldebaran": "Aldebaran",
        "Antares": "Antares",
        "Regulus": "Regulus",
        "Sirius": "Sirius",
        "Spica": "Spica",
        "Rigel": "Rigel",
    }

    for label, swe_name in star_map.items():
        lon, err = swe_fixstar_lon(jd_ut, swe_name)
        if err:
            points_unavailable[label] = err
        else:
            points[label] = lon

    # Galactic Center:
    # There isn't a universally standardized single-value implementation in Swiss by default.
    # For now, we provide a conventional reference longitude (J2000-ish) and label it as such.
    # If you want it computed from RA/Dec precisely, we can add that in a later phase.
    points["Galactic Center"] = {
        "mode": "conventional_reference",
        "lon_deg": 266.4,
        "note": "Conventional reference value (approx). Not computed from RA/Dec in this phase."
    }

    return {
        "meta": {
            "input_local": {"date": data.date, "time": data.time, "tz": data.tz},
            "utc_datetime": utc_dt.isoformat(),
            "lat": data.lat,
            "lon": data.lon,
            "house_system": data.house_system,
            "swiss_ephe_path": SWEPH_PATH,
        },
        "angles": {
            "asc_deg": asc,
            "mc_deg": mc,
            "vertex_deg": vertex,
        },
        "houses": {
            "cusps_deg": house_cusps,
        },
        "planets": planets,
        "planet_houses": planet_houses,
        "points": points,
        "unavailable": points_unavailable,
    }
