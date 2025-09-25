from skyfield.api import load, Topos
from datetime import datetime, timedelta

def get_planet_positions(date_str, time_str, tz_offset, lat, lon):
    planets = load('de421.bsp')
    ts = load.timescale()

    local_dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
    utc_dt = local_dt - timedelta(hours=int(tz_offset))
    t = ts.utc(utc_dt.year, utc_dt.month, utc_dt.day, utc_dt.hour, utc_dt.minute)

    observer = Topos(latitude_degrees=lat, longitude_degrees=lon)
    astrometric = planets['earth'] + observer

    planet_names = {
        'Sun': planets['sun'],
        'Moon': planets['moon'],
        'Mercury': planets['mercury'],
        'Venus': planets['venus'],
        'Mars': planets['mars'],
        'Jupiter': planets['jupiter barycenter'],
        'Saturn': planets['saturn barycenter'],
        'Uranus': planets['uranus barycenter'],
        'Neptune': planets['neptune barycenter'],
        'Pluto': planets['pluto barycenter'],
    }

    results = {}

    for name, body in planet_names.items():
        ra, dec, _ = astrometric.at(t).observe(body).apparent().radec()
        results[name] = {
            "RA_hours": round(ra.hours, 4),
            "Dec_degrees": round(dec.degrees, 4)
        }

    return results
