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
        t = skyfield_time_from_utc(utc_dt)

        observer = eph["earth"] + Topos(latitude_degrees=data.lat, longitude_degrees=data.lon)

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
                "lat_deg": float(lat.degrees),
                "retrograde": retrograde_flag(observer, body, t),
            }

        jd_ut = swe_julday_ut(utc_dt)
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
