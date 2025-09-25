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
