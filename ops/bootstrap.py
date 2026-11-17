"""First-run setup on a new server: backfill (skipping sources already archived), build
tables, select weather models, train + promote, publish the first forecast.

    python -m ops.bootstrap            # fetch what's missing, then build everything
    python -m ops.bootstrap --no-fetch # rebuild from an existing data/raw copy only
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys

from common.archive import iter_manifest
from common.config import ROOT
from common.timeutil import setup_logging

log = logging.getLogger("bootstrap")


def sh(*args: str):
    log.info("$ python -m %s", " ".join(args))
    subprocess.run([sys.executable, "-m", *args], cwd=ROOT, check=True)


def have(source: str) -> bool:
    return any(e.get("status") == 200 for e in iter_manifest(source))


def main(fetch: bool = True):
    if fetch:
        if not have("ferry_dump"):
            sh("ingest.ferry", "dump")
        sh("ingest.ferry", "update")
        # full hourly record of every station (Billy Bishop 2009-, downtown 2002-, Island A 1957-2006)
        sh("ingest.eccc", "backfill", "--skip-archived")
        sh("ingest.eccc", "update")
        if not have("om_archive"):
            sh("ingest.openmeteo", "archive", "--start", "2015-01-01")
        sh("ingest.openmeteo", "previous-runs", "--start", "2024-01-15")  # skips archived chunks
        sh("ingest.openmeteo", "update")
        sh("ingest.openmeteo", "forecast")
        if not have("events_feed_historical"):
            sh("ingest.events", "historical")
        sh("ingest.events", "fetch")
    else:
        sh("ingest.ferry", "parse")
        sh("ingest.eccc", "parse")
        sh("ingest.openmeteo", "parse")
        sh("ingest.events", "parse", "--full")
    sh("features.ferry_clean")
    sh("features.weather")
    sh("model.weather_skill")
    # rolling-origin backtest -> interval calibration + day-total ranges used by the live forecast
    sh("model.backtest")
    sh("reports.make_model_report")
    sh("reports.make_data_quality")
    sh("model.outlook", "backtest")   # long-range outlook: ranges + report
    sh("model.outlook", "train")
    sh("model.retrain", "--force")
    sh("model.predict_live")
    sh("ops.publish")
    sh("ops.health")
    log.info("bootstrap complete")


if __name__ == "__main__":
    setup_logging()
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-fetch", action="store_true")
    main(fetch=not ap.parse_args().no_fetch)
