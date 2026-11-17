"""Pure functions behind the API (operate on the published forecast.json)."""

from __future__ import annotations

import json
import math

from common.config import CONFIG_DIR

LEVELS = ["Quiet", "Moderate", "Busy", "Very busy"]
DEPART_HOURS = range(9, 19)   # realistic departure times for a visit (09:00-18:59)


def confidence(lead: int) -> str:
    return "high" if lead <= 1 else "medium" if lead <= 3 else "low"


def _fmt(h: float) -> str:
    hh, mm = int(h), int(round((h - int(h)) * 60))
    if mm == 60:
        hh, mm = hh + 1, 0
    return f"{hh:02d}:{mm:02d}"


def best_times(day: dict, n: int = 3) -> dict:
    hours = [h for h in day["hours"] if h["hour"] in DEPART_HOURS]
    ranked = sorted(hours, key=lambda h: h["pred"])
    def win(h):
        return {"start": _fmt(h["hour"]), "end": _fmt(h["hour"] + 1), "expected_boardings": round(h["pred"]),
                "range": [round(h["p10"]), round(h["p90"])], "level": LEVELS[h["level"]]}
    peak = max(hours, key=lambda h: h["pred"]) if hours else None
    return {"date": day["date"], "lead_days": day["lead"], "confidence": confidence(day["lead"]),
            "best": [win(h) for h in ranked[:n]],
            "worst": [win(h) for h in sorted(hours, key=lambda h: -h["pred"])[:n]],
            "peak_hour": win(peak) if peak else None}


def _lognorm_cdf(x: float, median: float, sigma: float) -> float:
    if x <= 0:
        return 0.0
    return 0.5 * (1 + math.erf((math.log(x) - math.log(median)) / (sigma * math.sqrt(2))))


def return_estimate(day: dict, cfg: dict | None = None) -> dict:
    """Spread each hour's predicted boardings over a stay-length distribution to get a
    relative return-flow curve; report the window where it is >= heavy_share_of_peak."""
    cfg = cfg or json.loads((CONFIG_DIR / "return_model.json").read_text())
    med, sig, mn = cfg["stay_hours_median"], cfg["stay_hours_sigma"], cfg["min_stay_hours"]
    last = cfg["last_departure_hour"]
    step = 0.25
    t_grid = [6 + i * step for i in range(int((last - 6) / step) + 1)]
    arrivals = []  # (time, people) at quarter-hour resolution
    for h in day["hours"]:
        for q in range(4):
            arrivals.append((h["hour"] + q * step + step / 2, h["pred"] / 4))
    flow = [0.0] * len(t_grid)
    on_island = [0.0] * len(t_grid)
    for ta, n in arrivals:
        for i, t in enumerate(t_grid):
            stay = t - ta
            if stay < mn:
                if stay >= 0:
                    on_island[i] += n
                continue
            c_prev = _lognorm_cdf(stay - step, med, sig) if stay - step >= mn else 0.0
            c_now = _lognorm_cdf(stay, med, sig)
            flow[i] += n * (c_now - c_prev)
            on_island[i] += n * (1 - c_now)
    # whoever is still there catches the last boats; reported separately so this
    # artificial end-of-day flush doesn't masquerade as the busiest return window
    last_boat_share = on_island[-1] / max(sum(h["pred"] for h in day["hours"]), 1)
    hourly = {}
    for t, f in zip(t_grid, flow):
        hourly[int(t)] = hourly.get(int(t), 0.0) + f
    hrs = sorted(h for h in hourly if h >= cfg.get("earliest_report_hour", 12))
    peak = max(hourly[h] for h in hrs) if hrs else 0
    heavy = [h for h in hrs if peak and hourly[h] >= cfg["heavy_share_of_peak"] * peak]
    if heavy:
        pk = max(hrs, key=lambda h: hourly[h])
        lo = hi = pk
        while lo - 1 in heavy:
            lo -= 1
        while hi + 1 in heavy:
            hi += 1
        window = {"start": _fmt(lo), "end": _fmt(hi + 1)}
    else:
        window = None
    total = sum(h["pred"] for h in day["hours"])
    busy_day = total >= 8000
    msg = None
    if window and busy_day:
        msg = f"Expect heavy return lines roughly {window['start']}-{window['end']}."
    elif window:
        msg = (f"Return lines should stay short. Most people head back roughly "
               f"{window['start']}-{window['end']}.")
    return {
        "date": day["date"], "estimate": True, "confidence": "low",
        "message": msg, "heaviest_window": window,
        "late_boats_note": ("The last boats of the night can also be crowded."
                            if busy_day and last_boat_share >= 0.1 else None),
        "relative_return_flow": {_fmt(h): round(hourly[h] / peak, 2) for h in hrs} if peak else {},
        "relative_people_on_island": {_fmt(t): round(v / max(on_island), 2)
                                      for t, v in zip(t_grid, on_island) if t == int(t)} if max(on_island) else {},
        "assumptions": {k: v for k, v in cfg.items() if not k.startswith("_")},
        "caveat": ("Returns need no ticket and are not counted anywhere. This is a model of when today's "
                   "visitors are likely to head back, assuming typical stay lengths. Treat it as a rough "
                   "warning, not a measurement."),
    }


def now_vs_typical(fc: dict) -> dict:
    today = next((d for d in fc["days"] if d["lead"] == 0), None)
    if not today:
        return {"available": False}
    seen = [h for h in today["hours"] if h["observed"] is not None]
    if not seen:
        return {"available": False, "reason": "no boardings recorded yet today"}
    h = seen[-1]
    typ = h["typical"]
    ratio = None if not typ else round(h["observed"] / typ, 2)
    if ratio is None:
        label = "unknown"
    elif ratio < 0.75:
        label = "quieter than usual"
    elif ratio <= 1.25:
        label = "about normal"
    else:
        label = "busier than usual"
    return {"available": True, "date": today["date"], "hour_start": _fmt(h["hour"]), "hour_end": _fmt(h["hour"] + 1),
            "observed_boardings": h["observed"], "typical_boardings": None if typ is None else round(typ),
            "ratio": ratio, "label": label, "predicted_boardings": round(h["pred"]),
            "lag_note": "City data is published hourly and lags by about an hour; this is the latest complete hour.",
            "ferry_last_interval_local": fc["ferry_last_interval_local"]}
