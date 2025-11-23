# main.py — Complete working version for Render (Skyfield Ephemeris API)
# Includes both /ephemeris and /extended_ephemeris endpoints
# Fixed for de440s.bsp kernel compatibility and stable deployment.

from fastapi import FastAPI
from pydantic import BaseModel
from skyfield.api import load, Topos
from skyfield.data import mpc
import io
import requests
import math

app = FastAPI()

# Define request model
class EphemerisRequest(BaseModel):
    date: str   # YYYY-MM-DD
    time: str   # HH:MM
    tz: int
    lat: float
    lon: float


@app.post("/ephemeris")
def get_ephemeris(data: EphemerisRequest):
    """Return RA/Dec for major planets using the standard Skyfield kernel."""
    ts = load.timescale()
    eph = load('de440s.bsp')

    t = ts.utc(
        int(data.date[:4]),
        int(data.date[5:7]),
        int(data.date[8:]),
        int(data.time[:2]),
        int(data.time[3:])
    )

    observer = eph['earth'] + Topos(latitude_degrees=data.lat, longitude_degrees=data.lon)

    # Correct planet mapping for de440s.bsp kernel
    bodies = {
        "Sun": "10",
        "Moon": "301",
        "Mercury": "1 MERCURY BARYCENTER",
        "Venus": "2 VENUS BARYCENTER",
        "Mars": "4 MARS BARYCENTER",
        "Jupiter": "5 JUPITER BARYCENTER",
        "Saturn": "6 SATURN BARYCENTER",
        "Uranus": "7 URANUS BARYCENTER",
        "Neptune": "8 NEPTUNE BARYCENTER",
        "Pluto": "9 PLUTO BARYCENTER"
    }

    result = {}

    for name, target in bodies.items():
        ast = eph[target]
        ast_pos = observer.at(t).observe(ast).apparent()
        ra, dec, _ = ast_pos.radec()
        result[name] = {"RA_hours": ra.hours, "Dec_degrees": dec.degrees}

    return result


@app.post("/extended_ephemeris")
def get_extended_ephemeris(data: EphemerisRequest):
    """Return RA/Dec for major planets plus Lilith, Chiron, and the four main asteroids."""
    ts = load.timescale()
    eph = load('de440s.bsp')

    t = ts.utc(
        int(data.date[:4]),
        int(data.date[5:7]),
        int(data.date[8:]),
        int(data.time[:2]),
        int(data.time[3:])
    )

    observer = eph['earth'] + Topos(latitude_degrees=data.lat, longitude_degrees=data.lon)

    result = {}

    # Correct planet mapping for de440s.bsp kernel
    bodies = {
        "Sun": "10",
        "Moon": "301",
        "Mercury": "1 MERCURY BARYCENTER",
        "Venus": "2 VENUS BARYCENTER",
        "Mars": "4 MARS BARYCENTER",
        "Jupiter": "5 JUPITER BARYCENTER",
        "Saturn": "6 SATURN BARYCENTER",
        "Uranus": "7 URANUS BARYCENTER",
        "Neptune": "8 NEPTUNE BARYCENTER",
        "Pluto": "9 PLUTO BARYCENTER"
    }

    # --- Major planets ---
    for name, target in bodies.items():
        ast = eph[target]
        ast_pos = observer.at(t).observe(ast).apparent()
        ra, dec, _ = ast_pos.radec()
        result[name] = {"RA_hours": ra.hours, "Dec_degrees": dec.degrees}

    # --- Mean Lilith (approx lunar apogee) ---
    moon = eph["moon"]
    earth = eph["earth"]
    moon_geo = earth.at(t).observe(moon).position.km
    lilith_vector = [-x for x in moon_geo]  # Opposite of perigee ≈ apogee
    ra_lil = math.degrees(math.atan2(lilith_vector[1], lilith_vector[0])) / 15
    dec_lil = math.degrees(
        math.asin(lilith_vector[2] / (sum(x ** 2 for x in lilith_vector) ** 0.5))
    )
    result["Lilith"] = {"RA_hours": ra_lil % 24, "Dec_degrees": dec_lil}

    # --- Chiron + Main Asteroids ---
    try:
        url = "https://minorplanetcenter.net/Extended_Files/MPCORB.DAT"
        f = io.BytesIO(requests.get(url).content)
        asteroids = mpc.load_mpcorb_dataframe(f)
        for name in ["2060 Chiron", "1 Ceres", "2 Pallas", "3 Juno", "4 Vesta"]:
            row = asteroids[asteroids["designation"] == name].iloc[0]
            asteroid = mpc.mpcorb_orbit(row, ts, center="500@0")
            ast_pos = observer.at(t).observe(asteroid).apparent()
            ra, dec, _ = ast_pos.radec()
            result[name] = {"RA_hours": ra.hours, "Dec_degrees": dec.degrees}
    except Exception as e:
        result["asteroid_error"] = f"Could not compute asteroids: {str(e)}"

    return result


# Run the app if executed locally
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=10000)
