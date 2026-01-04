# /transits_range Natal Overlay v1 (Astrology Bob)

This document describes the **v1 natal overlay** behavior for the `/transits_range` endpoint in Astrology Bob.

---

## What it does

`/transits_range` returns **hourly Swiss Ephemeris** planet positions and detected **major aspects** for a requested date/time window.

Optionally, it can **overlay** a natal chart onto each transit timestamp and return **transit-to-natal aspect hits**.

---

## Request fields (new)

### `natal_overlay` (bool)

- Default: `false`
- When `false` or omitted: behavior is unchanged (no natal overlay computation).

### `natal` (object, required only when `natal_overlay=true`)

Required if you enable natal overlay. If missing, the API returns HTTP 400.

Fields:

- `date`: `YYYY-MM-DD` (local date)
- `time`: `HH:MM` (24h local time)
- `tz`: number (local UTC offset hours, e.g. `-5`, `0`, `1`)
- `lat`: number
- `lon`: number

---

## Validation behavior

If:

- `natal_overlay=true`
- and `natal` is missing

Response:

- HTTP **400**
- body: `{"error": "natal required when natal_overlay=true"}`

No fallback guessing.

---

## Response additions when overlay is ON

When `natal_overlay=true` and `natal` is provided, each timestamp row in `positions` gains:

### `natal_aspects`: list of aspect objects

Each object includes:

- `transit_planet`: string
- `natal_planet`: string
- `aspect`: `conjunction | sextile | square | trine | opposition`
- `angle`: number (`0`, `60`, `90`, `120`, `180`)
- `orb`: number (rounded)
- `exact`: boolean (within exact orb threshold)
- `applying`: `true | false | null`

### About `applying: null`

`applying` is computed by comparing the current timestamp’s orb to the **next** timestamp’s orb (lookahead). For the **final timestamp** in the requested range, there is no “next sample,” so `applying` remains `null` intentionally.

This is expected and not a bug.

---

## Explicit non-scope (v1)

- `house_activations` are **not enabled** in v1 overlay output. (The overlay helper supports it, but the API passes `include_house_activations=False`.)

---

## Minimal example requests

### Overlay OFF (default behavior)

```json
{
  "start_date": "2026-01-04",
  "start_time": "00:00",
  "end_date": "2026-01-04",
  "end_time": "03:00",
  "tz": -5,
  "lat": 40.7128,
  "lon": -74.0060,
  "house_system": "porphyry",
  "natal_overlay": false
}
```

### Overlay ON (`natal_aspects` added)

```json
{
  "start_date": "2026-01-04",
  "start_time": "00:00",
  "end_date": "2026-01-04",
  "end_time": "03:00",
  "tz": -5,
  "lat": 40.7128,
  "lon": -74.0060,
  "house_system": "porphyry",
  "natal_overlay": true,
  "natal": {
    "date": "1986-03-17",
    "time": "22:23",
    "tz": 0,
    "lat": 51.5231724,
    "lon": -0.1437628
  }
}
```

---

## Note on `house_system`

`house_system` currently defaults to `"porphyry"` for `/transits_range`.

Even though v1 overlay does **not** output house activations, the natal chart builder still receives `house_system` during natal chart construction.

In other words:

- **v1 overlay output uses natal aspects only**
- `house_system` still influences natal chart computation (angles/cusps), even if that doesn’t surface in v1 overlay output.
