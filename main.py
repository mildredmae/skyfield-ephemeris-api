from fastapi import FastAPI
from pydantic import BaseModel
from ephemeris import get_planet_positions

app = FastAPI()

class EphemerisRequest(BaseModel):
    date: str
    time: str
    tz: int
    lat: float
    lon: float

@app.post("/ephemeris")
def get_positions(req: EphemerisRequest):
    return get_planet_positions(req.date, req.time, req.tz, req.lat, req.lon)

# --- NEW: Extended Ephemeris endpoint (Lilith, Chiron, Asteroids) ---
from skyfield.api import load, Topos
from skyfield.data import mpc
import io, requests, math

@app.post("/extended_ephemeris")
def get_extended_ephemeris(data: EphemerisRequest):
    """Return RA/Dec for major planets plus Lilith, Chiron, and the four main asteroids."""
    ts = load.timescale()
    eph = load('de440s.bsp')
    t = ts.utc(int(data.date[:4]), int(data.date[5:7]), int(data.date[8:]), int(data.time[:2]), int(data.time[3:]))
    observer = eph['earth'] + Topos(latitude_degrees=data.lat, longitude_degrees=data.lon)

    result = {}

    # Major planets
    bodies = ['sun','moon','mercury','venus','mars','jupiter barycenter',
              'saturn barycenter','uranus barycenter','neptune barycenter','pluto barycenter']
    for b in bodies:
        ast = eph[b]
        ast_pos = observer.at(t).observe(ast).apparent()
        ra, dec, _ = ast_pos.radec()
        result[b.title()] = {"RA_hours": ra.hours, "Dec_degrees": dec.degrees}

    # Lilith (approx lunar apogee)
    moon = eph['moon']
    earth = eph['earth']
    moon_geo = earth.at(t).observe(moon).position.km
    lilith_vector = [-x for x in moon_geo]  # opposite perigee ≈ apogee
    ra_lil = math.degrees(math.atan2(lilith_vector[1], lilith_vector[0])) / 15
    dec_lil = math.degrees(math.asin(lilith_vector[2] / (sum(x**2 for x in lilith_vector)**0.5)))
    result["Lilith"] = {"RA_hours": ra_lil % 24, "Dec_degrees": dec_lil}

    # Chiron + main asteroids
    url = 'https://minorplanetcenter.net/Extended_Files/MPCORB.DAT'
    f = io.BytesIO(requests.get(url).content)
    asteroids = mpc.load_mpcorb_dataframe(f)
    for name in ['2060 Chiron','1 Ceres','2 Pallas','3 Juno','4 Vesta']:
        row = asteroids[asteroids['designation']==name].iloc[0]
        asteroid = mpc.mpcorb_orbit(row, ts, center='500@0')
        ast_pos = observer.at(t).observe(asteroid).apparent()
        ra, dec, _ = ast_pos.radec()
        result[name] = {"RA_hours": ra.hours, "Dec_degrees": dec.degrees}

    return result
