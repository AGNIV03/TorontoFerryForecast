"""Tiny in-container scheduler (no cron daemon needed).

Jobs run one at a time, in a subprocess each, so a crash never kills the scheduler and
DuckDB only ever has one writer. Every run is logged to data/logs/jobs.jsonl, which
ops.health reads to alert on failing jobs. Times are America/Toronto.

    python -m ops.scheduler            # run forever
    python -m ops.scheduler --once ferry_hourly   # run one job now
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

from common.config import DATA, ROOT, TZ
from common.timeutil import now_local, setup_logging

log = logging.getLogger("scheduler")
LOGS = DATA / "logs"
PY = [sys.executable, "-m"]


@dataclass
class Job:
    name: str
    steps: list[list[str]]
    minute: int
    hours: tuple[int, ...] | None = None    # None = every hour
    weekdays: tuple[int, ...] | None = None  # Mon=0
    timeout: int = 1800

    def due(self, t: datetime) -> bool:
        return (t.minute == self.minute and (self.hours is None or t.hour in self.hours)
                and (self.weekdays is None or t.weekday() in self.weekdays))


PREDICT = ["model.predict_live"]
JOBS = [
    # ferry counts are published hourly (~:15-:20 past); forecast refresh right after
    Job("ferry_hourly", [["ingest.ferry", "update"], ["features.ferry_clean"], PREDICT], minute=25),
    Job("weather_forecast", [["ingest.openmeteo", "forecast"], PREDICT], minute=5, hours=tuple(range(0, 24, 3))),
    Job("weather_daily", [["ingest.eccc", "update"], ["ingest.openmeteo", "update"], ["features.weather"],
                          ["ops.publish"]], minute=40, hours=(4,), timeout=3600),
    Job("events_daily", [["ingest.events", "fetch"]], minute=10, hours=(3,), timeout=3600),
    Job("score_daily", [["ops.score"]], minute=50, hours=(5,)),
    Job("ferry_full_dump", [["ingest.ferry", "dump"], ["features.ferry_clean"]], minute=15, hours=(3,), weekdays=(6,)),
    Job("retrain_weekly", [["model.retrain"], ["model.outlook", "train"], PREDICT, ["ops.publish"]],
        minute=0, hours=(2,), weekdays=(0,), timeout=7200),
    Job("health", [["ops.health"]], minute=55),
]


def run_job(job: Job) -> int:
    LOGS.mkdir(parents=True, exist_ok=True)
    rc, tail = 0, ""
    worst, worst_tail = 0, ""
    t0 = time.time()
    for step in job.steps:
        try:
            p = subprocess.run(PY + step, cwd=ROOT, capture_output=True, text=True, timeout=job.timeout)
            rc, tail = p.returncode, (p.stdout + p.stderr)[-2000:]
        except subprocess.TimeoutExpired:
            rc, tail = 124, f"timeout after {job.timeout}s"
        if rc != 0:
            log.error("job %s step %s failed rc=%s\n%s", job.name, step, rc, tail)
            # keep going: later steps rebuild from whatever is archived (a failed fetch must not
            # stop the forecast refresh or the publish step); the job is still recorded as failed
            if worst == 0:
                worst, worst_tail = rc, " ".join(step) + ": " + tail
    rc, tail = worst, worst_tail
    entry = {"at": now_local().isoformat(), "job": job.name, "rc": rc,
             "seconds": round(time.time() - t0, 1), "tail": tail if rc else ""}
    with open(LOGS / "jobs.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")
    log.info("job %s rc=%s in %.0fs", job.name, rc, entry["seconds"])
    return rc


def loop():
    log.info("scheduler started with %d jobs", len(JOBS))
    last_tick = None
    while True:
        now = datetime.now(TZ).replace(second=0, microsecond=0)
        if now != last_tick:
            # catch up any minute skipped while a long job was running
            t = (last_tick + timedelta(minutes=1)) if last_tick else now
            t = max(t, now - timedelta(hours=3))   # after a long pause, don't replay every missed hour
            due = []
            while t <= now:
                due += [job for job in JOBS if job.due(t) and job not in due]   # each job once per catch-up
                t += timedelta(minutes=1)
            for job in due:
                run_job(job)
            last_tick = now
        time.sleep(5)


if __name__ == "__main__":
    setup_logging()
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", help="run a single job by name and exit")
    a = ap.parse_args()
    if a.once:
        sys.exit(run_job(next(j for j in JOBS if j.name == a.once)))
    loop()
