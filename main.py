from fastapi import FastAPI
from pydantic import BaseModel
from skyfield.api import load, Topos
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Any, Tuple
import os
import traceback
import swisseph as swe
from fastapi.responses import JSONResponse

app = FastAPI(
    title="Skyfield Ephemeris API",
    version="1.2.8",
    default_response_class=JSONResponse,
)

ts = load.timescale()
eph = load("de440s.bsp")

SWEPH_PATH = os.getenv("SWEPH_PATH", "ephe")
swe.set_ephe_path(SWEPH_PATH)

class WesternChartRequest(BaseModel):
    date: str
    time: str
    tz: float
    lat: float
    lon: float
    house_system: str  # "whole_sign" | "porphyry" | "placidus"

HOUSE_SYSTEM_MAP: Dict[str, bytes] = {
    "whole_sign": b"W",
    "porphyry": b"O",
    "placidus": b"P",
}

SIGN_NAMES = [
    "Aries","Taurus","Gemini","Cancer","Leo","Virgo",
    "Libra","Scorpio","Sagittarius","Capricorn","Aquarius","Pisces"
]
# ----------------------------
# Aspect detection (v1)
# ----------------------------

ASPECT_ANGLES = {
    "conjunction": 0,
    "sextile": 60,
    "square": 90,
    "trine": 120,
    "opposition": 180,
}

ASPECT_ORBS = {
    "conjunction": 8.0,
    "opposition": 8.0,
    "square": 6.0,
    "trine": 6.0,
    "sextile": 4.0,
}

def detect_aspects(planets: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Detect major aspects between planets.
    Input: planets dict with absolute longitudes (0–360).
    Output: list of aspect objects.
    """
    aspects: List[Dict[str, Any]] = []

    names = sorted(planets.keys())

    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            a = names[i]
            b = names[j]

            lon1 = float(planets[a]["lon_deg"])
            lon2 = float(planets[b]["lon_deg"])

            delta = abs(lon1 - lon2)
            if delta > 180.0:
                delta = 360.0 - delta

            for aspect_name, aspect_angle in ASPECT_ANGLES.items():
                orb_limit = ASPECT_ORBS[aspect_name]
                orb = abs(delta - aspect_angle)

                if orb <= orb_limit:
                    aspects.append({
                        "planet_a": a,
                        "planet_b": b,
                        "aspect": aspect_name,
                        "angle": aspect_angle,
                        "orb": round(orb, 4),
                        "exact": orb <= 0.1,
                    })

    return aspects
def sanitize(obj: Any) -> Any:
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj

    if isinstance(obj, (bytes, bytearray)):
        try:
            return obj.decode("utf-8")
        except Exception:
            return str(obj)

    if isinstance(obj, dict):
        return {str(k): sanitize(v) for k, v in obj.items()}

    if isinstance(obj, list):
        return [sanitize(x) for x in obj]

    if isinstance(obj, (tuple, set)):
        return [sanitize(x) for x in obj]

    # numpy scalar
    if hasattr(obj, "item") and callable(getattr(obj, "item")):
        try:
            return sanitize(obj.item())
        except Exception:
            pass

    # numpy array
    if hasattr(obj, "tolist") and callable(getattr(obj, "tolist")):
        try:
            return sanitize(obj.tolist())
        except Exception:
            pass

    if hasattr(obj, "__dict__"):
        try:
            return sanitize(vars(obj))
        except Exception:
            pass

    return str(obj)

def respond(payload: Any, status_code: int = 200) -> JSONResponse:
    return JSONResponse(content=sanitize(payload), status_code=status_code)

def normalize_deg(deg: Any) -> float:
    return float(deg) % 360.0

def to_utc_datetime(date_str: str, time_str: str, tz_offset: float) -> datetime:
    local_dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
    return local_dt - timedelta(hours=tz_offset)

def skyfield_time_from_utc(utc_dt: datetime):
    return ts.utc(utc_dt.year, utc_dt.month, utc_dt.day, utc_dt.hour, utc_dt.minute, utc_dt.second)

def zodiac_sign_index(lon_deg: float) -> int:
    return int(lon_deg // 30)

def retrograde_flag(observer, body, t) -> bool:
    dt1 = t.utc_datetime()
    dt2 = dt1 + timedelta(hours=1)
    t2 = ts.utc(dt2.year, dt2.month, dt2.day, dt2.hour, dt2.minute, dt2.second)

    p1 = float(observer.at(t).observe(body).apparent().ecliptic_latlon()[0].degrees)
    p2 = float(observer.at(t2).observe(body).apparent().ecliptic_latlon()[0].degrees)

    delta = ((p2 - p1 + 540.0) % 360.0) - 180.0
    return bool(delta < 0.0)

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
    return float(
        swe.julday(
            utc_dt.year,
            utc_dt.month,
            utc_dt.day,
            utc_dt.hour + utc_dt.minute / 60.0 + utc_dt.second / 3600.0
        )
    )

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

# IMPORTANT: allow GET + HEAD so Render pings don’t get 405
@app.api_route("/", methods=["GET", "HEAD"])
def root():
    return respond({"status": "ok", "service": "skyfield-ephemeris-api", "version": app.version})

@app.api_route("/health", methods=["GET", "HEAD"])
def health():
    return respond({"status": "ok", "version": app.version, "swiss_ephe_path": SWEPH_PATH})

@app.post("/western_chart")
def western_chart(data: WesternChartRequest):
    try:
        if data.house_system not in HOUSE_SYSTEM_MAP:
            return respond(
                {"error": f"Unsupported house_system '{data.house_system}'. Use one of {list(HOUSE_SYSTEM_MAP.keys())}."},
                status_code=400,
            )

        hsys = HOUSE_SYSTEM_MAP[data.house_system]
        utc_dt = to_utc_datetime(data.date, data.time, data.tz)
        jd_ut = swe_julday_ut(utc_dt)

        # Planet positions via Swiss Ephemeris (astrology-accurate longitudes + speeds)
        flags = swe.FLG_SWIEPH | swe.FLG_SPEED
        swe_body_map = {
            "Sun": swe.SUN,
            "Moon": swe.MOON,
            "Mercury": swe.MERCURY,
            "Venus": swe.VENUS,
            "Mars": swe.MARS,
            "Jupiter": swe.JUPITER,
            "Saturn": swe.SATURN,
            "Uranus": swe.URANUS,
            "Neptune": swe.NEPTUNE,
            "Pluto": swe.PLUTO,
        }

        planets: Dict[str, Any] = {}
        for name, swe_id in swe_body_map.items():
            xx, _ = swe.calc_ut(jd_ut, swe_id, flags)
            lon = normalize_deg(xx[0])
            lat = float(xx[1])
            speed_lon = float(xx[3])
            planets[name] = {
                "lon_deg": lon,
                "lat_deg": lat,
                "speed_lon_deg_per_day": speed_lon,
                "retrograde": bool(speed_lon < 0.0),
                "sign_index": int(lon // 30),
                "deg_in_sign": float(lon % 30),
                "sign": SIGN_NAMES[int(lon // 30)],
                "deg": int(lon % 30),
                "min": int(((lon % 30) - int(lon % 30)) * 60),
                "sec": int(round(((((lon % 30) - int(lon % 30)) * 60) - int(((lon % 30) - int(lon % 30)) * 60)) * 60)),
            }

            aspects = detect_aspects(planets)

            positions.append({
                "utc_datetime": cur.isoformat(),
                "planets": planets,
                "aspects": aspects
            })
        cusps, ascmc = swe.houses(jd_ut, float(data.lat), float(data.lon), hsys)

        house_cusps, cusp_err = extract_house_cusps(cusps)
        if cusp_err or house_cusps is None:
            return respond(
                {"error": "Swiss Ephemeris returned unexpected cusp structure.", "detail": cusp_err, "raw_type": str(type(cusps))},
                status_code=500,
            )

        asc = normalize_deg(ascmc[0])
        mc = normalize_deg(ascmc[1])
        try:
            vertex = normalize_deg(ascmc[3])
        except Exception:
            vertex = None

        planet_houses: Dict[str, int] = {}
        if data.house_system == "whole_sign":
            asc_sign = zodiac_sign_index(asc)
            for p, vals in planets.items():
                p_sign = zodiac_sign_index(vals["lon_deg"])
                planet_houses[p] = int(((p_sign - asc_sign) % 12) + 1)
        else:
            for p, vals in planets.items():
                planet_houses[p] = int(assign_house_quadrant(vals["lon_deg"], house_cusps))

        return respond({
            "meta": {
                "input_local": {"date": data.date, "time": data.time, "tz": float(data.tz)},
                "utc_datetime": utc_dt.isoformat(),
                "lat": float(data.lat),
                "lon": float(data.lon),
                "house_system": data.house_system,
                "swiss_ephe_path": SWEPH_PATH,
            },
            "angles": {"asc_deg": asc, "mc_deg": mc, "vertex_deg": vertex},
            "houses": {"cusps_deg": house_cusps},
            "planets": planets,
            "planet_houses": planet_houses,
        })

    except Exception:
        return respond({"error": "Internal error in /western_chart", "trace": traceback.format_exc()}, status_code=500)

class TransitsRangeRequest(BaseModel):
    start_date: str         # YYYY-MM-DD (local)
    start_time: str         # HH:MM (local)
    end_date: str           # YYYY-MM-DD (local)
    end_time: str           # HH:MM (local)
    tz: float               # local offset hours (e.g., -5, 0)
    lat: float
    lon: float
    house_system: str = "porphyry"  # keep for later (house activations)

@app.post("/transits_range")
def transits_range(data: TransitsRangeRequest):
    try:
        # Parse local datetimes, convert to UTC using your existing helper
        start_utc = to_utc_datetime(data.start_date, data.start_time, data.tz)
        end_utc = to_utc_datetime(data.end_date, data.end_time, data.tz)

        if end_utc <= start_utc:
            return respond({"error": "end must be after start"}, status_code=400)

        # Hourly sampling (inclusive of start, exclusive of end)
        flags = swe.FLG_SWIEPH | swe.FLG_SPEED
        swe_body_map = {
            "Sun": swe.SUN,
            "Moon": swe.MOON,
            "Mercury": swe.MERCURY,
            "Venus": swe.VENUS,
            "Mars": swe.MARS,
            "Jupiter": swe.JUPITER,
            "Saturn": swe.SATURN,
            "Uranus": swe.URANUS,
            "Neptune": swe.NEPTUNE,
            "Pluto": swe.PLUTO,
        }

        positions: List[Dict[str, Any]] = []

        cur = start_utc
        while cur < end_utc:
            jd_ut = swe_julday_ut(cur)

            planets: Dict[str, Any] = {}
            for name, swe_id in swe_body_map.items():
                xx, _ = swe.calc_ut(jd_ut, swe_id, flags)
                lon = normalize_deg(xx[0])
                lat = float(xx[1])
                speed_lon = float(xx[3])

                planets[name] = {
                    "lon_deg": lon,
                    "lat_deg": lat,
                    "speed_lon_deg_per_day": speed_lon,
                    "retrograde": bool(speed_lon < 0.0),
                    "sign_index": int(lon // 30),
                    "deg_in_sign": float(lon % 30),
                    "sign": SIGN_NAMES[int(lon // 30)],
                    "deg": int(lon % 30),
                    "min": int(((lon % 30) - int(lon % 30)) * 60),
                    "sec": int(round(((((lon % 30) - int(lon % 30)) * 60) - int(((lon % 30) - int(lon % 30)) * 60)) * 60)),
                }

            aspects = detect_aspects(planets)

            positions.append({
                "utc_datetime": cur.isoformat(),
                "planets": planets,
                "aspects": aspects
            })


            cur = cur + timedelta(hours=1)

        return respond({
            "meta": {
                "input_local": {
                    "start_date": data.start_date,
                    "start_time": data.start_time,
                    "end_date": data.end_date,
                    "end_time": data.end_time,
                    "tz": float(data.tz),
                },
                "utc_start": start_utc.isoformat(),
                "utc_end": end_utc.isoformat(),
                "step": "hourly",
                "lat": float(data.lat),
                "lon": float(data.lon),
                "swiss_ephe_path": SWEPH_PATH,
            },
            "positions": positions
        })

    except Exception:
        return respond({"error": "Internal error in /transits_range", "trace": traceback.format_exc()}, status_code=500)

