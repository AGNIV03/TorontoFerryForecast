# Toronto Ferry Forecasts

Live at https://ferries.agniv.site (set `SITE_URL` in `.env` for another domain; it drives the canonical URL, `sitemap.xml` and `robots.txt`).

Predicts how busy the Toronto Island ferries will be, **hour by hour, up to 7 days ahead**.
It learns from the City of Toronto's ferry ticket counts (every 15 minutes since May 2015)
combined with historical weather, and runs on live weather forecasts.

* Classical machine learning (LightGBM) trained and run on your own server. No paid APIs,
  no API keys, no AI/LLM services. Running cost is $0 apart from hosting.
* Open source: MIT licence for the code. The data keeps its own licences (see below).
* **Independent project: not affiliated with or endorsed by the City of Toronto.
  Predictions, not guarantees.**

How accurate is it? See [`reports/model_report.md`](reports/model_report.md) (the
backtest on 2024-2026) and the live accuracy log the site publishes daily.

## What it shows

* **Schedule banner:** the ferry schedule in force (routes, frequency, departures in winter),
  how many people the boats can carry per hour, the fleet, and the next schedule change.
* **Today:** expected island-bound boardings per hour with a likely range (10th-90th percentile),
  background bands showing how full the scheduled boats get, the quietest times to go, the latest
  counted hour against normal, and a rough estimate of when return lines will be heaviest.
* **Next 7 days:** a zoomable hour-by-hour chart coloured by busyness, day strips with best and
  worst hours, and daily totals with ranges. Confidence drops visibly with lead time.
* **Months ahead (up to 6 months):** a calendar coloured by the busiest hour or by total, week-by-week
  and month-by-month expectations with ranges, and the hourly profile of any day you click.
* **Explore the data:** 11+ years of history by hour, day, week, month or year over any date range
  (with weather alongside), year-by-year totals and growth, year-over-year overlays, a weekday x hour
  heatmap per schedule period, a calendar heatmap for any year, holidays by year, the effect of
  temperature and rain, and CSV downloads.
* **How it works:** recent live accuracy, the method in plain words, practical tips.

Charts use [Apache ECharts](https://echarts.apache.org/) 5.5.1 (Apache-2.0), served from
`web/vendor/`, so there is no third-party script CDN.

What the numbers are, and aren't:

* **Boardings** = tickets scanned at the mainland terminal = people heading *to* the Island.
  This is what we forecast.
* Counts are **terminal-wide**: Centre Island, Hanlan's Point and Ward's Island combined.
  The City doesn't publish per-route counts, so neither do we.
* **Return trips need no ticket and are not counted by anyone.** The return estimate is a
  model built on assumed stay lengths, labelled as such (see below).
* There is no data on queue length, boat capacity or cancellations.

## Architecture

```
ingest/      fetchers: every response is archived raw (append-only) before parsing
features/    cleaning (ferry), stitched observed weather, calendar
model/       dataset builder, LightGBM training, backtest, weather-model selection,
             live prediction, weekly retrain with promotion gate
api/         FastAPI app (reads only published JSON; never the database)
web/         single-page mobile-first site (vanilla HTML/CSS/JS, no build step)
ops/         scheduler, health checks + alerts, daily accuracy scoring, bootstrap
config/      hand-editable events.csv, school calendar, return-trip assumptions
reports/     data_quality.md, weather_model_selection.md, model_report.md (+ generators)
data/        raw/ (archive), processed/ (DuckDB), models/, serving/, forecast_log/, logs/
```

Two containers share `./data`:

* **worker** runs `ops.scheduler`. Jobs run one at a time (DuckDB has a single writer):

  | job | when (Toronto time) | what |
  |---|---|---|
  | `ferry_hourly` | hh:25 | newest ferry counts -> hourly table -> new forecast |
  | `weather_forecast` | every 3 h at :05 | archive the live forecast (7 models) -> new forecast |
  | `weather_daily` | 04:40 | ECCC observations, Open-Meteo reanalysis + archived past forecasts |
  | `events_daily` | 03:10 | archive the City's Festivals & Events feed |
  | `score_daily` | 05:50 | score past forecasts against actuals -> public accuracy log |
  | `ferry_full_dump` | Sun 03:15 | full ferry CSV dump (catches revisions) |
  | `retrain_weekly` | Mon 02:00 | retrain; promote only if it doesn't regress |
  | `health` | hh:55 | feed freshness/schema/sanity checks -> alerts |

* **api** (`uvicorn api.main:app`) serves the site and the JSON API on port 8000,
  bound to the host's loopback only. Expose it with a Cloudflare tunnel.

### Raw archive

Every fetch from every source is saved before parsing, gzip-compressed, under
`data/raw/<source>/<YYYY>/<MM>/<DD>/<fetched_utc>_<sha8>.<ext>.gz`, with one line per fetch
in `data/raw/<source>/manifest.jsonl` (URL, parameters, HTTP status, size, SHA-256). Files are
never overwritten or deleted. **Back up `data/raw/`**: the feeds can disappear, and the
archive is the product's insurance. Every table in `data/processed/ferry.duckdb` can be
rebuilt from it (`python -m ops.bootstrap --no-fetch`).

Disk use: roughly 7 GB/year, almost all of it the daily events feed (~18 MB gzipped per day).
Everything else is a few hundred MB per year.

## API

| endpoint | returns |
|---|---|
| `GET /api/forecast?days=7` | hourly predictions (06:00-23:59) with p10/p90 range, busyness level, typical value, daily totals with range |
| `GET /api/best-times?date=YYYY-MM-DD` | quietest and busiest hour-long departure windows |
| `GET /api/now` | latest complete counted hour vs typical for this time of year (lags ~1 h; labelled) |
| `GET /api/return-estimate?date=` | rough return-line window + relative return-flow curve (estimate) |
| `GET /api/accuracy` | recent live accuracy |
| `GET /api/schedule` | schedule periods, today's period, upcoming changes, fleet, hourly capacity |
| `GET /api/outlook?days=180` | long-range outlook: typical boardings per day and hour, with ranges |
| `GET /api/history?start=&end=&agg=hour\|day\|week\|month\|year` | cleaned history with weather (hourly limited to 400 days) |
| `GET /api/years` | yearly totals and growth |
| `GET /api/compare-years?agg=week\|month\|day` | one series per year for overlays |
| `GET /api/heatmap?period=&years=` | average boardings by weekday x hour |
| `GET /api/calendar?year=` | daily totals for one year |
| `GET /api/holidays` | each holiday, each year, against normal days nearby |
| `GET /api/weather-effect?period=&daytype=` | daily high and rain against boardings |
| `GET /api/download?dataset=daily\|hourly&start=&end=` | CSV download |
| `GET /api/health` | freshness for every source (HTTP 503 when anything is unhealthy) |

The unprefixed paths in the original spec (`/forecast`, `/best-times`, `/now`,
`/return-estimate`, `/health`) are aliases. Interactive docs are at `/docs`.

### Busyness levels

Busyness is **boardings as a share of what the ferries can carry in that hour**
(`config/capacity.json`, `config/busyness.json` scheme `capacity`). Capacity comes from the
City's vessel capacities (Sam McBride 908, Thomas Rennie 908, William Inglis 395, Ongiara 216
passengers) times the departures per hour of the schedule in force, at a practical load factor
of 80%:

| schedule period | practical capacity/h | Moderate from | Busy from | Very busy from |
|---|---|---|---|---|
| winter (Oct 14 - Apr 14, Ward's Island only) | 600 | 120 | 240 | 390 |
| spring (Apr 15 - May 12) / fall (Sep 16 - Oct 13) | 1,680 | 336 | 672 | 1,092 |
| summer (May 13 - Sep 15) | 2,720 | 544 | 1,088 | 1,768 |
| peak summer (school break to Labour Day) | 3,880 | 776 | 1,552 | 2,522 |

Quiet = plenty of room; Moderate = steady, still walk-on; Busy = lines at the gates, boats
filling; Very busy = boats near full, you may wait for the next one. The busiest hour on record
(3,737 boardings, 2026-07-11) is ~96% of peak-summer practical capacity. Dated overrides cover
disruptions (e.g. Sam McBride out of service after the 2022-08-20 dock crash). **Update
`config/capacity.json` when the new 1,300-passenger electric ferries enter service (2027) or the
schedule changes.** The percentile schemes (`all`, `season`, `season_hour`) remain available.

### Return-trip estimate

Each hour's forecast boardings are spread over a stay-length distribution (log-normal,
median 4 h, sigma 0.55, minimum 45 min; configurable in `config/return_model.json`).
The resulting return-flow curve gives the window where returns are at least 85% of the
day's return peak. Everyone still on the Island is assumed to leave by the last boat,
which is reported separately rather than as a peak. **No ground truth exists**, so the
site shows a sentence ("expect heavy return lines roughly 17:00-19:00"), never a number.

## Setup (Debian server, Docker Compose, Cloudflare tunnel)

```bash
git clone <this repo> ferry-forecaster && cd ferry-forecaster
cp .env.example .env            # optional: SMTP for alerts, tunnel token
mkdir -p data && sudo chown 10001 data   # the containers run as uid 10001
docker compose build
# first run: backfill everything (~1 h, mostly polite throttling of Open-Meteo), train, publish
docker compose run --rm worker python -m ops.bootstrap
docker compose up -d
curl -s localhost:8000/api/health | head
```

If you already have a copy of `data/raw/` (e.g. from another machine), copy it into `./data/raw/`
first: bootstrap skips sources that are already archived, and
`python -m ops.bootstrap --no-fetch` rebuilds purely from the archive.

**Cloudflare tunnel**, either:

* on the host: point an existing `cloudflared` ingress rule at `http://localhost:8000`, or
* in compose: create a tunnel in the Cloudflare dashboard with the public hostname routed to
  `http://api:8000`, put the token in `.env` as `CLOUDFLARE_TUNNEL_TOKEN`, and run
  `docker compose --profile tunnel up -d`.

The API sets `Cache-Control: public, max-age=300` on forecast JSON, so Cloudflare can cache it.

**Alerts:** a feed that goes stale for more than 6 h (ferry, live forecast), a failing job
(including a schema change, which makes the ingester fail loudly), or a stuck rain gauge is
logged to `data/logs/alerts.log`, shown in `/api/health`, and emailed if SMTP is configured
in `.env`. It repeats every 24 h while the problem persists, plus once on recovery.

### Development

```bash
pip install uv && uv sync
uv run python -m ops.bootstrap          # or individual steps, see ops/bootstrap.py
uv run pytest
uv run uvicorn api.main:app --reload
uv run python -m model.backtest && uv run python -m reports.make_model_report
```

## Data sources and licences

Code: MIT (`LICENSE`). Data and bundled libraries keep their own licences: see `NOTICE.md`.

| source | used for | licence / attribution |
|---|---|---|
| City of Toronto Open Data: [Toronto Island Ferry Ticket Counts](https://open.toronto.ca/dataset/toronto-island-ferry-ticket-counts/) | target variable | Open Government Licence - Toronto. *"Contains information licensed under the Open Government Licence - Toronto."* |
| City of Toronto Open Data: Festivals & Events | event archive (future feature) | Open Government Licence - Toronto |
| Environment and Climate Change Canada, MSC GeoMet OGC API: hourly climate observations, Toronto City Centre (6158359) and Toronto City (6158355) | training weather | ECCC Data Servers End-use Licence. *"Data Source: Environment and Climate Change Canada."* |
| [Open-Meteo](https://open-meteo.com/): forecast API, Previous Runs API, historical archive (ERA5 / ECMWF IFS) | live forecasts, honest backtests, gap filling (cloud, gusts, radiation) | CC BY 4.0, attribution required. The free API is for **non-commercial** use (fair use ~10,000 calls/day; this app uses roughly 100-200 weighted calls/day). |

**If the project ever becomes commercial**, Open-Meteo's free tier no longer applies.
Switch the forecast feed to [ECMWF open data](https://www.ecmwf.int/en/forecasts/datasets/open-data)
(CC BY 4.0, commercial use allowed) or ECCC GeoMet directly (GEM/HRDPS), or take an
Open-Meteo commercial plan.

Don't use City of Toronto logos or imply endorsement.

## Models

* **Hourly forecast, 0-7 days** (`model/dataset.py`, `model/core.py`): LightGBM (Tweedie) on
  calendar and school breaks; **holiday and event history** (how much busier each occasion was than
  normal days in previous years, plus that uplift applied to the current level); recent levels
  (last 7 days, same weekday over 4 weeks, last year); a **multi-year trend projection** (a
  recency-weighted log-linear fit through the same weeks of the last 6 normal years, extrapolated to the
  target date) and year-over-year momentum; overnight online sales; and weather from the best blend of
  forecast models per lead time. Quantile models plus split-conformal calibration give the ranges.
  Training mixes observed-weather rows (2015 onwards) with rows whose weather is the archived real
  forecast for that lead time (2024 onwards), so the model learns how far to trust a forecast several
  days out. The day's high is also compared with the seasonal normal and the previous two weeks.
* **Long-range outlook, 8-180 days** (`model/outlook.py`): daily totals from the calendar,
  holiday history, last year's same days and momentum, modelled as deviations from the multi-year
  trend projection, and split into hours with the date's projected hourly profile. No weather. See
  `reports/outlook_report.md`: it matches or slightly beats climatology and is honest about wide ranges.
* **Busyness** compares boardings with the scheduled capacity (`config/capacity.json`).

## Design notes and known limitations

* **Weather is the biggest driver.** Without weather features, the model is no better than
  a climatology baseline. Accuracy several days out is therefore capped by weather-forecast
  skill. See the model report.
* **Unrecorded events and closures** (festivals, boat outages, storms, ice, closed beaches)
  are the main source of large misses. Add known events to `config/events.csv`.
* **Anomaly periods** (2015 ticketing roll-out, 2017 flood closure, 2020-21 pandemic) are
  excluded from training. See `reports/data_quality.md`.
* **The Billy Bishop rain gauge has reported garbage since 2026-09-30.** QC falls back to the
  downtown station automatically; the health check flags it.
* Out of scope for now, but the design leaves room for: beach water quality and lake
  conditions (an "should I go to the Island today?" planner), per-route forecasts (only if
  the City publishes per-gate data), and push/email alerts for users.
