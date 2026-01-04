from fastapi import FastAPI
from pydantic import BaseModel
from skyfield.api import load, Topos
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Any, Tuple
import os
import traceback
import swisseph as swe

app = FastAPI(title="Skyfield Ephemeris API", version="1.2.4")

# ----------------------------
# Global loads (faster on Render)
# ----------------------------
ts = load.timescale()
eph = load("de440s.bsp")

# Swiss ephemeris path (where optional data files can live)
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
HOUSE_SYSTEM_MAP: Dict[str, bytes] = {
    "whole_sign": b"W",  # Whole Sign
    "porphyry": b"O",    # Porphyry
    "placidus": b"P",    # Placidus
}


def to_py_float(x: Any) -> float:
    # Handles numpy.float64, Decimal-like, etc.
    return float(x)


def to_py_bool(x: Any) -> bool:
    # Handles numpy.bool_ etc.
    return bool(x)


def normalize_deg(deg: Any) -> float:
    return to_py_float(deg) % 360.0


def to_utc_datetime(date_str: str, time_str: str, tz_offset: float) -> datetime:
    local_dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
    utc_dt = local_dt - timedelta(hours=tz_offset)
    return utc_dt


def skyfield_time_from_utc(utc_dt: datetime):
    return ts.utc(utc_dt.year, utc_dt.month, utc_dt.day, utc_dt.hour, utc_dt.minute, utc_dt.second)


def zodiac_sign_index(lon_deg: float) -> int:
    return int(lon_deg // 30)  # Aries=0 ... Pisces=11


def retrograde_flag(observer, body, t) -> bool:
    dt1 = t.utc_datetime()
    dt2 = dt1 + timedelta(hours=1)
    t2 = ts.utc(dt2.year, dt2.month, dt2.day, dt2.hour, dt2.minute, dt2.second)

    p1 = to_py_float(observer.at(t).observe(body).apparent().ecliptic_latlon()[0].degrees)
    p2 = to_py_float(observer.at(t2).observe(body).apparent().ecliptic_latlon()[0].degrees)

    delta = ((p2 - p1 + 540.0) % 360.0) - 180.0
    return to_py_bool(delta < 0.0)


def assign_house_quadrant(lonp: float, cusps_deg: List[float]) -> int:
    for i in range(12):
        start = cusps_deg[i]
        end = cusps_deg[(i + 1) % 12]
        if start <= end:
            if start <= lonp < end:
                return i + 1
        else:
            if lonp >= start or lonp < end:
                return i + 1
    return 12


def swe_julday_ut(utc_dt: datetime) -> float:
    return to_py_float(
        swe.julday(
            utc_dt.year,
            utc_dt.month,
            utc_dt.day,
            utc_dt.hour + utc_dt.minute / 60.0 + utc_dt.second / 3600.0
        )
    )


def swe_calc_lon(jd_ut: float, body_id: int) -> Tuple[Optional[float], Optional[str]]:
    try:
        res, _flag = swe.calc_ut(jd_ut, body_id)
        lon = normalize_deg(res[0])
        return lon, None
    except Exception as e:
        return None, str(e)


def swe_fixstar_lon(jd_ut: float, star_name: str) -> Tuple[Optional[float], Optional[str]]:
    try:
        starpos, _retflag = swe.fixstar2_ut(star_name, jd_ut)
        lon = normalize_deg(starpos[0])
        return lon, None
    except Exception as e:
        return None, str(e)


def extract_house_cusps(cusps: Any) -> Tuple[Optional[List[float]], Optional[str]]:
    try:
        n = len(cusps)
    except Exception:
        return None, f"cusps has no length (type={type(cusps)})"

    if n == 13:
        return [normalize_deg(cusps[i]) for i in range(1, 13)], None
    if n == 12:
        return [normalize_deg(cusps[i]) for i in range(0, 12)], None

    return None, f"Unexpected cusps length: {n}. Raw cusps={cusps}"


# ----------------------------
# Health check
# ----------------------------
@app.get("/health")
def health():
    return {
        "status": "ok",
        "version": app.version,
        "swiss_ephe_path": SWEPH_PATH,
    }


# ----------------------------
# Existing endpoints (kept)
# ----------------------------
@app.post("/ephemeris")
def ephemeris_endpoint(data: EphemerisRequest):
    try:
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

        results: Dict[str, Any] = {}
        for name, body in bodies.items():
            ast_pos = observer.at(t).observe(body).apparent()
            lon, lat, _dist = ast_pos.ecliptic_latlon()
            results[name] = {
                "lon_deg": normalize_deg(lon.degrees),
                "lat_deg": to_py_float(lat.degrees),
            }

        return {
            "meta": {
                "input_local": {"date": data.date, "time": data.time, "tz": to_py_float(data.tz)},
                "utc_datetime": utc_dt.isoformat(),
                "lat": to_py_float(data.lat),
                "lon": to_py_float(data.lon),
            },
            "bodies": results,
        }
    except Exception:
        return {"error": "Internal error in /ephemeris", "trace": traceback.format_exc()}


@app.post("/extended_ephemeris")
def extended_ephemeris_endpoint(data: EphemerisRequest):
    try:
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

        results: Dict[str, Any] = {}
        for name, body in bodies.items():
            ast_pos = observer.at(t).observe(body).apparent()
            lon, lat, _dist = ast_pos.ecliptic_latlon()
            results[name] = {
                "lon_deg": normalize_deg(lon.degrees),
                "lat_deg": to_py_float(lat.degrees),
                "retrograde": retrograde_flag(observer, body, t),
            }

        return {
            "meta": {
                "input_local": {"date": data.date, "time": data.time, "tz": to_py_float(data.tz)},
                "utc_datetime": utc_dt.isoformat(),
                "lat": to_py_float(data.lat),
                "lon": to_py_float(data.lon),
            },
            "bodies": results,
        }
    except Exception:
        return {"error": "Internal error in /extended_ephemeris", "trace": traceback.format_exc()}


# ----------------------------
# Canonical endpoint for Astrology Bob (Phase 2)
# ----------------------------
@app.post("/western_chart")
def western_chart(data: WesternChartRequest):
    try:
        if data.house_system not in HOUSE_SYSTEM_MAP:
            return {"error": f"Unsupported house_system '{data.house_system}'. Use one of {list(HOUSE_SYSTEM_MAP.keys())}."}

        hsys = HOUSE_SYSTEM_MAP[data.house_system]

        utc_dt = to_utc_datetime(data.date, data.time, data.tz)
        t = skyfield_time_from_utc(utc_dt)

        observer = eph["earth"] + Topos(latitude_degrees=data.lat, longitude_degrees=data.lon)

        # Planet positions (Skyfield)
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

        planets: Dict[str, Any] = {}
        for name, body in skyfield_bodies.items():
            ast_pos = observer.at(t).observe(body).apparent()
            lon, lat, _dist = ast_pos.ecliptic_latlon()
            planets[name] = {
                "lon_deg": normalize_deg(lon.degrees),
                "lat_deg": to_py_float(lat.degrees),
                "retrograde": retrograde_flag(observer, body, t),
            }

        # Houses + angles (Swiss)
        jd_ut = swe_julday_ut(utc_dt)
        cusps, ascmc = swe.houses(jd_ut, to_py_float(data.lat), to_py_float(data.lon), hsys)

        house_cusps, cusp_err = extract_house_cusps(cusps)
        if cusp_err or house_cusps is None:
            return {
                "error": "Swiss Ephemeris returned unexpected cusp structure.",
                "detail": cusp_err,
                "raw_cusps_type": str(type(cusps)),
            }

        asc = normalize_deg(ascmc[0])
        mc = normalize_deg(ascmc[1])

        try:
            vertex = normalize_deg(ascmc[3])
        except Exception:
            vertex = None

        # Planet -> house assignment
        planet_houses: Dict[str, int] = {}
        if data.house_system == "whole_sign":
            asc_sign = zodiac_sign_index(asc)
            for p, vals in planets.items():
                p_sign = zodiac_sign_index(vals["lon_deg"])
                planet_houses[p] = int(((p_sign - asc_sign) % 12) + 1)
        else:
            for p, vals in planets.items():
                planet_houses[p] = int(assign_house_quadrant(vals["lon_deg"], house_cusps))

        # Points
        points: Dict[str, Any] = {}
        unavailable: Dict[str, str] = {}

        mean_node, err = swe_calc_lon(jd_ut, swe.MEAN_NODE)
        if err:
            unavailable["Mean Node"] = err
        else:
            points["Mean Node"] = mean_node
            points["North Node"] = mean_node
            points["South Node"] = normalize_deg(mean_node + 180.0)

        true_node, err = swe_calc_lon(jd_ut, swe.TRUE_NODE)
        if err:
            unavailable["True Node"] = err
        else:
            points["True Node"] = true_node

        lilith_mean, err = swe_calc_lon(jd_ut, swe.MEAN_APOG)
        if err:
            unavailable["Lilith"] = err
        else:
            points["Lilith"] = lilith_mean

        lilith_i, err = swe_calc_lon(jd_ut, swe.INTP_APOG)
        if err:
            unavailable["Lilith (i)"] = err
            unavailable["Priapus (i)"] = err
        else:
            points["Lilith (i)"] = lilith_i
            points["Priapus (i)"] = normalize_deg(lilith_i + 180.0)

        # Part of Fortune
        sun_house = planet_houses.get("Sun")
        is_day_chart = True
        if sun_house is not None:
            is_day_chart = sun_house >= 7

        sun_lon = planets["Sun"]["lon_deg"]
        moon_lon = planets["Moon"]["lon_deg"]

        if is_day_chart:
            fortune = normalize_deg(asc + moon_lon - sun_lon)
        else:
            fortune = normalize_deg(asc + sun_lon - moon_lon)

        points["Fortune"] = fortune

        # Optional asteroids/TNOs
        asteroid_map = {
            "Ceres": 1,
            "Pallas": 2,
            "Juno": 3,
            "Vesta": 4,
            "Chiron": 2060,
            "Pholus": 5145,
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
                unavailable[name] = err
            else:
                points[name] = lon

        # Fixed stars
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
                unavailable[label] = err
            else:
                points[label] = lon

        points["Galactic Center"] = {
            "mode": "conventional_reference",
            "lon_deg": 266.4,
            "note": "Conventional reference value (approx). Not computed from RA/Dec in this phase."
        }

        return {
            "meta": {
                "input_local": {"date": data.date, "time": data.time, "tz": to_py_float(data.tz)},
                "utc_datetime": utc_dt.isoformat(),
                "lat": to_py_float(data.lat),
                "lon": to_py_float(data.lon),
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
            "unavailable": unavailable,
        }

    except Exception:
        return {
            "error": "Internal error in /western_chart",
            "trace": traceback.format_exc(),
        }
