from fastapi import FastAPI
from pydantic import BaseModel
from skyfield.api import load, Topos
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Optional, Any, Tuple
import os
import traceback
import swisseph as swe
from fastapi.responses import JSONResponse
from overlay_natal import NatalInput, build_natal_chart, augment_transits_with_natal_overlay

app = FastAPI(
    title="Skyfield Ephemeris API",
    version="1.2.8",
    default_response_class=JSONResponse,
)

ts = load.timescale()
eph = load("de440s.bsp")

SWEPH_PATH = os.getenv("SWEPH_PATH", "ephe")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SWEPH_PATH_ABS = os.path.join(BASE_DIR, SWEPH_PATH)
swe.set_ephe_path(SWEPH_PATH_ABS)
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

def normalize_dms(deg: int, minute: int, second: int):
    if second >= 60:
        second = 0
        minute += 1
    if minute >= 60:
        minute = 0
        deg += 1
    return deg, minute, second
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
    return respond({"status": "ok", "version": app.version, "swiss_ephe_path": SWEPH_PATH, "swiss_ephe_path_abs": SWEPH_PATH_ABS, "swiss_ephe_dir_exists": bool(os.path.isdir(SWEPH_PATH_ABS)), "seas_18_present": bool(os.path.exists(os.path.join(SWEPH_PATH_ABS, "seas_18.se1"))), "cwd": os.getcwd()})

@app.post("/western_chart")
def western_chart(data: WesternChartRequest):
    try:
        swe.set_ephe_path(SWEPH_PATH_ABS)
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
            "Chiron": swe.CHIRON,
            "Pholus": swe.PHOLUS,
            "Ceres": swe.CERES,
            "Pallas": swe.PALLAS,
            "Juno": swe.JUNO,
            "Vesta": swe.VESTA,
            "Varuna": swe.VARUNA,
            "Mean Node": swe.MEAN_NODE,
            "True Node": swe.TRUE_NODE,
            "Mean Apogee": swe.MEAN_APOG,
            "Osculating Apogee": swe.OSCU_APOG,
            "Earth": swe.EARTH,
            "Interpolated Apogee": swe.INTP_APOG,
            "Interpolated Perigee": swe.INTP_PERG,
        }

        planets: Dict[str, Any] = {}
        for name, swe_id in swe_body_map.items():
            xx, _ = swe.calc_ut(jd_ut, swe_id, flags)
            lon = normalize_deg(xx[0])
            lat = float(xx[1])
            speed_lon = float(xx[3])
            deg_i = int(lon % 30)
            min_i = int(((lon % 30) - deg_i) * 60)
            sec_i = int(round(((((lon % 30) - deg_i) * 60) - min_i) * 60))
            deg_i, min_i, sec_i = normalize_dms(deg_i, min_i, sec_i)

            planets[name] = {
                "lon_deg": lon,
                "lat_deg": lat,
                "speed_lon_deg_per_day": speed_lon,
                "retrograde": bool(speed_lon < 0.0),
                "sign_index": int(lon // 30),
                "deg_in_sign": float(lon % 30),
                "sign": SIGN_NAMES[int(lon // 30)],
                "deg": deg_i,
                "min": min_i,
                "sec": sec_i,
            }

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

class NatalPayload(BaseModel):
    date: str   # YYYY-MM-DD (local)
    time: str   # HH:MM (local)
    tz: float   # local offset hours (e.g., -5, 0)
    lat: float
    lon: float

class TransitsRangeRequest(BaseModel):
    start_date: str         # YYYY-MM-DD (local)
    start_time: str         # HH:MM (local)
    end_date: str           # YYYY-MM-DD (local)
    end_time: str           # HH:MM (local)
    tz: float               # local offset hours (e.g., -5, 0)
    lat: float
    lon: float
    house_system: str = "porphyry"  # keep for later (house activations)
    bodies: str = "major"          # "major" | "expanded"
    aspects_bodies: str = "major"  # "major" | "selected"
    natal_overlay: bool = False
    natal: "NatalPayload | None" = None

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
        swe_body_map_major = {
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

        swe_body_map_expanded = {
            **swe_body_map_major,
            # Phase 1A (Asteroids / Minor Bodies)
            "Chiron": swe.CHIRON,
            "Pholus": swe.PHOLUS,
            "Ceres": swe.CERES,
            "Pallas": swe.PALLAS,
            "Juno": swe.JUNO,
            "Vesta": swe.VESTA,
            "Varuna": swe.VARUNA,
            # Phase 1B (Nodes & Points)
            "Mean Node": swe.MEAN_NODE,
            "True Node": swe.TRUE_NODE,
            "Mean Apogee": swe.MEAN_APOG,
            "Osculating Apogee": swe.OSCU_APOG,
            "Earth": swe.EARTH,
            "Interpolated Apogee": swe.INTP_APOG,
            "Interpolated Perigee": swe.INTP_PERG,
        }

        if data.bodies not in ("major", "expanded"):
            return respond({"error": "Unsupported bodies value", "detail": "Use one of: major, expanded"}, status_code=400)
        if data.aspects_bodies not in ("major", "selected"):
            return respond({"error": "Unsupported aspects_bodies value", "detail": "Use one of: major, selected"}, status_code=400)

        swe_body_map = swe_body_map_major if data.bodies == "major" else swe_body_map_expanded

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

                deg_i = int(lon % 30)
                min_i = int(((lon % 30) - deg_i) * 60)
                sec_i = int(round(((((lon % 30) - deg_i) * 60) - min_i) * 60))
                deg_i, min_i, sec_i = normalize_dms(deg_i, min_i, sec_i)

                planets[name] = {
                    "lon_deg": lon,
                    "lat_deg": lat,
                    "speed_lon_deg_per_day": speed_lon,
                    "retrograde": bool(speed_lon < 0.0),
                    "sign_index": int(lon // 30),
                    "deg_in_sign": float(lon % 30),
                    "sign": SIGN_NAMES[int(lon // 30)],
                    "deg": deg_i,
                    "min": min_i,
                    "sec": sec_i,
                }
            aspects_input = planets if data.aspects_bodies == "selected" else {k: planets[k] for k in swe_body_map_major.keys()}
            aspects = detect_aspects(aspects_input)

            positions.append({
                "utc_datetime": cur.isoformat(),
                "planets": planets,
                "aspects": aspects
            })


            cur = cur + timedelta(hours=1)

        # Natal overlay (v1): natal_aspects only
        if data.natal_overlay:
            if data.natal is None:
                return respond({"error": "natal required when natal_overlay=true"}, status_code=400)

            natal_input = NatalInput(
                date=data.natal.date,
                time=data.natal.time,
                tz=float(data.natal.tz),
                lat=float(data.natal.lat),
                lon=float(data.natal.lon),
            )
            natal_chart = build_natal_chart(natal_input, house_system=data.house_system)
            positions = augment_transits_with_natal_overlay(
                transits=positions,
                natal_chart=natal_chart,
                include_natal_aspects=True,
                include_house_activations=True,
            )

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
# ============================================================
# Moon Next Sign (minute-accurate) — backend-only capability
# ============================================================

class MoonNextSignRequest(BaseModel):
    start_date: str  # YYYY-MM-DD
    start_time: str  # HH:MM (24h)
    tz: float        # hours offset from UTC, e.g. -5, 0, 1
    target_sign: str # e.g., "Leo"
    max_days: int = 30
    coarse_step_minutes: int = 30


def _to_utc_datetime_from_local(date_str: str, time_str: str, tz_hours: float) -> datetime:
    y, m, d = [int(x) for x in date_str.split("-")]
    hh, mm = [int(x) for x in time_str.split(":")]
    offset = timedelta(hours=float(tz_hours))
    tzinfo = timezone(offset)
    dt_local = datetime(y, m, d, hh, mm, 0, tzinfo=tzinfo)
    return dt_local.astimezone(timezone.utc).replace(tzinfo=None)  # naive UTC


def _moon_lon_and_sign(dt_utc: datetime) -> Tuple[float, int, str]:
    # Swiss Ephemeris expects UT; build fractional hour
    jd = swe.julday(
        dt_utc.year, dt_utc.month, dt_utc.day,
        dt_utc.hour + dt_utc.minute / 60.0 + dt_utc.second / 3600.0
    )
    flags = swe.FLG_SWIEPH
    xx, _ = swe.calc_ut(jd, swe.MOON, flags)
    lon = float(xx[0]) % 360.0
    sign_index = int(lon // 30)
    return lon, sign_index, SIGN_NAMES[sign_index]


def _parse_sign_index(name: str) -> int:
    n = str(name).strip().lower()
    for i, sname in enumerate(SIGN_NAMES):
        if sname.lower() == n:
            return i
    raise ValueError(f"Unknown sign '{name}'. Must be one of: {', '.join(SIGN_NAMES)}")


def _find_next_moon_sign_ingress_minute(
    start_utc: datetime,
    target_sign_index: int,
    max_days: int,
    coarse_step_minutes: int,
) -> Dict[str, Any]:
    if max_days <= 0:
        raise ValueError("max_days must be > 0")
    if coarse_step_minutes <= 0:
        raise ValueError("coarse_step_minutes must be > 0")

    # Determine starting sign
    _, start_sign_index, start_sign_name = _moon_lon_and_sign(start_utc)
    already_in_target = (start_sign_index == target_sign_index)

    # "next move into X sign" means an ingress event (prev != target, now == target)
    # If already in target at start, we wait until it leaves, then look for the next ingress back into target.
    waiting_for_exit = already_in_target

    prev_dt = start_utc
    _, prev_sign_index, prev_sign_name = _moon_lon_and_sign(prev_dt)

    # Coarse scan forward to bracket ingress
    end_utc = start_utc + timedelta(days=int(max_days))
    step = timedelta(minutes=int(coarse_step_minutes))

    cur_dt = prev_dt + step
    while cur_dt <= end_utc:
        _, cur_sign_index, cur_sign_name = _moon_lon_and_sign(cur_dt)

        if waiting_for_exit:
            if cur_sign_index != target_sign_index:
                waiting_for_exit = False
            prev_dt, prev_sign_index, prev_sign_name = cur_dt, cur_sign_index, cur_sign_name
            cur_dt = cur_dt + step
            continue

        # We have exited target (or never were in it). Look for first time we are in target.
        if cur_sign_index == target_sign_index and prev_sign_index != target_sign_index:
            # Bracket is (prev_dt, cur_dt]
            low = prev_dt
            high = cur_dt

            # Binary search to the minute: find earliest time in [low, high] where sign == target
            while (high - low).total_seconds() > 60:
                mid = low + timedelta(seconds=int((high - low).total_seconds() // 2))
                _, mid_sign_index, _ = _moon_lon_and_sign(mid)
                if mid_sign_index == target_sign_index:
                    high = mid
                else:
                    low = mid

            # Normalize to minute boundary (best effort minute accuracy)
            high_min = high.replace(second=0, microsecond=0)

            _, from_idx, from_name = _moon_lon_and_sign((high_min - timedelta(minutes=1)))
            _, to_idx, to_name = _moon_lon_and_sign(high_min)

            return {
                "found": True,
                "utc_datetime": high_min.isoformat(),
                "from_sign": from_name,
                "to_sign": to_name,
                "start_sign": start_sign_name,
                "already_in_target_at_start": bool(already_in_target),
            }

        prev_dt, prev_sign_index, prev_sign_name = cur_dt, cur_sign_index, cur_sign_name
        cur_dt = cur_dt + step

    return {
        "found": False,
        "utc_datetime": None,
        "from_sign": None,
        "to_sign": None,
        "start_sign": start_sign_name,
        "already_in_target_at_start": bool(already_in_target),
    }


@app.post("/moon_next_sign")
def moon_next_sign(data: MoonNextSignRequest):
    """
    Minute-accurate (best effort) next ingress of the Moon into a target tropical sign,
    computed from a user-provided local start datetime + tz.
    """
    try:
        start_utc = _to_utc_datetime_from_local(data.start_date, data.start_time, float(data.tz))
        target_idx = _parse_sign_index(data.target_sign)

        res = _find_next_moon_sign_ingress_minute(
            start_utc=start_utc,
            target_sign_index=target_idx,
            max_days=int(data.max_days),
            coarse_step_minutes=int(data.coarse_step_minutes),
        )

        if res["found"]:
            # Compute local datetime using the input tz (fixed offset)
            offset = timedelta(hours=float(data.tz))
            dt_local = (datetime.fromisoformat(res["utc_datetime"]) + offset).replace(second=0, microsecond=0)
            local_iso = dt_local.isoformat()
        else:
            local_iso = None

        return respond({
            "meta": {
                "input_local": {
                    "start_date": data.start_date,
                    "start_time": data.start_time,
                    "tz": float(data.tz),
                    "target_sign": data.target_sign,
                    "max_days": int(data.max_days),
                    "coarse_step_minutes": int(data.coarse_step_minutes),
                },
                "utc_start": start_utc.isoformat(),
                "swiss_ephe_path": SWEPH_PATH,
            },
            "result": {
                "found": res["found"],
                "utc_datetime": res["utc_datetime"],
                "local_datetime": local_iso,
                "from_sign": res["from_sign"],
                "to_sign": res["to_sign"],
                "start_sign": res.get("start_sign"),
                "already_in_target_at_start": res.get("already_in_target_at_start"),
            }
        })

    except Exception:
        return respond({"error": "Internal error in /moon_next_sign", "trace": traceback.format_exc()}, status_code=500)
