"""Toronto Island Ferry Crowd Forecaster API.

Reads only files published by the worker (data/serving/*.json); never touches DuckDB.

    uvicorn api.main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from api import explore, logic
from common.config import DATA, ROOT, SITE_URL
from common.timeutil import now_local

SERVING = DATA / "serving"
WEB = ROOT / "web"
ATTRIBUTION = [
    "Contains information licensed under the Open Government Licence - Toronto.",
    "Data Source: Environment and Climate Change Canada.",
    "Weather forecasts and reanalysis by Open-Meteo.com (CC BY 4.0).",
]
DISCLAIMER = "Predictions, not guarantees. Not affiliated with or endorsed by the City of Toronto."

app = FastAPI(title="Toronto Ferry Forecasts", version="1.2",
              description=DISCLAIMER + " " + " ".join(ATTRIBUTION) + " All times are Toronto time (EST/EDT).")
app.include_router(explore.router)


def _read(name: str) -> dict:
    p = SERVING / name
    if not p.exists():
        raise HTTPException(503, f"{name} not published yet")
    return json.loads(p.read_text())


def _json(data, max_age=300) -> JSONResponse:
    return JSONResponse(data, headers={"Cache-Control": f"public, max-age={max_age}"})


def _day(fc: dict, date: str | None) -> dict:
    if date is None:
        return fc["days"][0]
    for d in fc["days"]:
        if d["date"] == date:
            return d
    raise HTTPException(404, f"no forecast for {date}; available: {[d['date'] for d in fc['days']]}")


def _meta(fc: dict) -> dict:
    return {"generated_at": fc["generated_at"], "model_version": fc["model_version"],
            "weather_issued_at": fc["weather_issued_at"], "ferry_last_interval_local": fc["ferry_last_interval_local"],
            "attribution": ATTRIBUTION, "disclaimer": DISCLAIMER}


@app.get("/api/forecast")
@app.get("/forecast", include_in_schema=False)
def forecast(days: int = Query(7, ge=1, le=8)):
    """Hourly island-bound boardings (06:00-23:59) with 10th-90th percentile range and busyness level."""
    fc = _read("forecast.json")
    out = []
    for d in fc["days"][:days]:
        out.append({**{k: v for k, v in d.items() if k != "hours"}, "confidence": logic.confidence(d["lead"]),
                    "hours": [{**h, "level_name": logic.LEVELS[h["level"]]} for h in d["hours"]]})
    return _json({**_meta(fc), "levels": fc["levels"], "same_day_factor": fc["same_day_factor"], "days": out})


@app.get("/api/best-times")
@app.get("/best-times", include_in_schema=False)
def best_times(date: str | None = None):
    """Quietest (and busiest) hour-long departure windows to the Island for a date."""
    fc = _read("forecast.json")
    return _json({**logic.best_times(_day(fc, date)), **_meta(fc)})


@app.get("/api/now")
@app.get("/now", include_in_schema=False)
def now():
    """Latest observed hour vs typical for this time of year (lags ~1 hour)."""
    fc = _read("forecast.json")
    return _json({**logic.now_vs_typical(fc), **_meta(fc)}, max_age=120)


@app.get("/api/return-estimate")
@app.get("/return-estimate", include_in_schema=False)
def return_estimate(date: str | None = None):
    """Rough estimate of when return (island -> city) lines are heaviest. An estimate, not data."""
    fc = _read("forecast.json")
    return _json({**logic.return_estimate(_day(fc, date)), **_meta(fc)})


@app.get("/api/schedule")
def schedule():
    """Ferry schedule periods, today's period, upcoming changes, fleet and hourly capacity."""
    return _json(_read("schedule.json"), max_age=3600)


@app.get("/api/outlook")
def outlook(days: int = Query(180, ge=8, le=180)):
    """Long-range outlook (8-180 days ahead): typical boardings per day and hour for the date, from season,
    weekday, holidays and the multi-year trend. No weather; wide ranges."""
    o = _read("outlook.json")
    o["days"] = [d for d in o["days"] if d["lead"] <= days]
    return _json(o, max_age=3600)


@app.get("/api/accuracy")
def accuracy():
    """Recent live accuracy (forecasts scored against actuals)."""
    p = SERVING / "accuracy.json"
    return _json(json.loads(p.read_text()) if p.exists() else {"by_lead": {}, "recent_next_day": []}, max_age=3600)


@app.get("/api/health")
@app.get("/health", include_in_schema=False)
def health():
    """Data freshness for every source."""
    p = SERVING / "health.json"
    h = json.loads(p.read_text()) if p.exists() else {"ok": False, "checks": {}, "note": "health not computed yet"}
    fcp = SERVING / "forecast.json"
    h["api_time"] = now_local().isoformat()
    h["forecast_published"] = fcp.exists()
    return JSONResponse(h, status_code=200 if h.get("ok") else 503, headers={"Cache-Control": "no-cache"})


# ---------------------------------------------------------------- site + cache busting
# Static assets are referenced with a content hash (?v=...), so every deploy produces new URLs
# that no browser or Cloudflare edge has cached; versioned assets can then be cached for a year.
ASSETS = ["style.css", "app.js", "vendor/echarts.min.js"]


def _asset_versions() -> dict[str, str]:
    out = {}
    for a in ASSETS:
        f = WEB / a
        if f.exists():
            out[a] = hashlib.sha256(f.read_bytes()).hexdigest()[:10]
    return out


VERSIONS = _asset_versions()


def _index_html() -> str:
    html = (WEB / "index.html").read_text(encoding="utf-8")
    for a, v in VERSIONS.items():
        html = html.replace(f'"/static/{a}"', f'"/static/{a}?v={v}"')
    return html.replace("__SITE_URL__", SITE_URL)


INDEX_HTML = _index_html() if (WEB / "index.html").exists() else ""


@app.get("/", include_in_schema=False)
def index():
    # short-lived so a new deploy (new asset hashes) reaches visitors within a minute
    return HTMLResponse(INDEX_HTML, headers={"Cache-Control": "public, max-age=60, must-revalidate"})


@app.get("/robots.txt", include_in_schema=False)
def robots():
    body = "\n".join(["User-agent: *", "Allow: /", "Disallow: /api/download", "Disallow: /docs", "",
                      f"Sitemap: {SITE_URL}/sitemap.xml", ""])
    return Response(body, media_type="text/plain", headers={"Cache-Control": "public, max-age=86400"})


@app.get("/sitemap.xml", include_in_schema=False)
def sitemap():
    # one canonical page; ?range= and #section views are the same document. lastmod = latest forecast.
    p = SERVING / "forecast.json"
    lastmod = json.loads(p.read_text())["generated_at"][:10] if p.exists() else str(now_local().date())
    body = "\n".join([
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
        f"  <url><loc>{SITE_URL}/</loc><lastmod>{lastmod}</lastmod><changefreq>hourly</changefreq><priority>1.0</priority></url>",
        "</urlset>", ""])
    return Response(body, media_type="application/xml", headers={"Cache-Control": "public, max-age=3600"})


@app.middleware("http")
async def static_cache_headers(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        if request.query_params.get("v"):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            response.headers["Cache-Control"] = "public, max-age=300"
    return response


if Path(WEB).exists():
    app.mount("/static", StaticFiles(directory=WEB), name="static")
