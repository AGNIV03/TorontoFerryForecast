"""Project-wide paths and constants."""

from __future__ import annotations

import os
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(os.environ.get("FERRY_ROOT", Path(__file__).resolve().parent.parent))
DATA = Path(os.environ.get("FERRY_DATA", ROOT / "data"))
RAW = DATA / "raw"
PROCESSED = DATA / "processed"
MODELS = DATA / "models"
REPORTS = ROOT / "reports"
CONFIG_DIR = ROOT / "config"
DB_PATH = PROCESSED / "ferry.duckdb"

TZ = ZoneInfo("America/Toronto")

# Jack Layton Ferry Terminal (Bay St & Queens Quay). Verified against OSM:
# terminal building ~43.6405, -79.3755.
TERMINAL_LAT = 43.6405
TERMINAL_LON = -79.3755

SITE_URL = os.environ.get("SITE_URL", "https://ferries.agniv.site").rstrip("/")
USER_AGENT = "toronto-ferry-forecaster/0.1 (open-source; +https://github.com/)"

# City of Toronto CKAN
CKAN = "https://ckan0.cf.opendata.inter.prod-toronto.ca"
FERRY_PACKAGE = "toronto-island-ferry-ticket-counts"
FERRY_RESOURCE = "0da005de-270d-49d1-b45b-32e2e777a381"
EVENTS_PACKAGE = "festivals-events"

# ECCC / MSC GeoMet OGC API
ECCC_API = "https://api.weather.gc.ca"

# Open-Meteo
OM_FORECAST = "https://api.open-meteo.com/v1/forecast"
OM_ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"
OM_HIST_FORECAST = "https://historical-forecast-api.open-meteo.com/v1/forecast"
OM_PREVIOUS_RUNS = "https://previous-runs-api.open-meteo.com/v1/forecast"
