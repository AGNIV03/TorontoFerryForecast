"""Feed freshness + schema/sanity checks -> data/serving/health.json, alerts on change.

A source is "stale" when its last successful fetch (or its newest data) is older than its
threshold. Alerts go to the log (always), to data/logs/alerts.log and, if SMTP is
configured (ALERT_EMAIL_TO, SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, SMTP_FROM),
by email. An alert fires when a check turns bad and repeats every 24 h while it stays bad (weekly for
"warning" checks: known problems already mitigated, which do not fail overall health).

    python -m ops.health
"""

from __future__ import annotations

import json
import logging
import os
import smtplib
from datetime import datetime, timedelta
from email.message import EmailMessage

import pandas as pd

from common import db
from common.archive import iter_manifest
from common.config import CONFIG_DIR, DATA, TZ
from common.timeutil import iso_local, label, now_local
from common.timeutil import setup_logging

log = logging.getLogger(__name__)
SERVING = DATA / "serving"
LOGS = DATA / "logs"
STATE = LOGS / "health_state.json"

# source -> (manifest name, max hours since last successful fetch)
FETCH_LIMITS = {
    "ferry": ("ferry_api", 6),
    "weather_forecast": ("om_forecast", 6),
    "weather_observations": ("eccc_hourly", 30),
    "weather_reanalysis": ("om_archive", 30),
    "weather_past_forecasts": ("om_previous_runs", 30),
    "events": ("events_feed", 30),
}


def _last_fetch(source: str):
    ok = err = None
    for e in iter_manifest(source):
        if e.get("status") == 200:
            ok = e["fetched_at"]
        else:
            err = e
    return (pd.Timestamp(ok) if ok else None), err


def checks(now: datetime | None = None) -> dict:
    now = pd.Timestamp(now or now_local())
    out = {}
    for name, (src, limit) in FETCH_LIMITS.items():
        last, err = _last_fetch(src)
        age = (now - last).total_seconds() / 3600 if last is not None else None
        out[name] = {"last_successful_fetch": iso_local(last),
                     "hours_since_fetch": None if age is None else round(age, 1),
                     "ok": age is not None and age <= limit, "limit_hours": limit}
        if err:
            out[name]["last_error_status"] = err.get("status")
    with db.connect(read_only=True) as con:
        last_int = con.execute("SELECT max(ts_local) FROM ferry_15min").fetchone()[0]
        last_obs = con.execute("SELECT max(ts_utc) FROM eccc_hourly WHERE climate_id='6158359'").fetchone()[0]
        bb_precip = con.execute("SELECT sum(precip_mm) FROM eccc_hourly WHERE climate_id='6158359' "
                                "AND ts_utc > (SELECT max(ts_utc) FROM eccc_hourly) - INTERVAL 24 HOUR").fetchone()[0]
    now_wall = now.tz_convert(TZ).tz_localize(None)
    data_age = (now_wall - pd.Timestamp(last_int)).total_seconds() / 3600
    # the ferry runs 06:30-23:45; overnight there are no rows, so only judge during the day
    daytime = 10 <= now_wall.hour <= 23
    out["ferry"]["newest_interval_local"] = str(last_int)
    out["ferry"]["hours_since_newest_interval"] = round(data_age, 1)
    out["ferry"]["ok"] = out["ferry"]["ok"] and (not daytime or data_age <= 6)
    obs_age = (now - pd.Timestamp(last_obs)).total_seconds() / 3600
    out["weather_observations"]["newest_observation"] = iso_local(last_obs)
    out["weather_observations"]["ok"] = out["weather_observations"]["ok"] and obs_age <= 96
    out["billy_bishop_rain_gauge"] = {"precip_last_24h_mm": None if bb_precip is None else round(float(bb_precip), 1),
                                      "ok": bb_precip is None or bb_precip < 200, "severity": "warning",
                                      "note": "stuck-gauge check; failed since 2026-09-30 (QC falls back to downtown)"}
    sc = pd.read_csv(CONFIG_DIR / "school_calendar.csv")
    need = (now_wall + pd.Timedelta(days=200)).year
    out["school_calendar"] = {"covers_through": int(sc.year.max()), "ok": int(sc.year.max()) >= need,
                              "severity": "warning", "note": "add next year's dates to config/school_calendar.csv"}
    fc = SERVING / "forecast.json"
    if fc.exists():
        f = json.loads(fc.read_text())
        age = (now - pd.Timestamp(f["generated_at"])).total_seconds() / 3600
        out["forecast_output"] = {"generated_at": f["generated_at"], "model_version": f["model_version"],
                                  "ok": age <= 3, "hours_since": round(age, 1)}
    jobs = LOGS / "jobs.jsonl"
    if jobs.exists():
        recent = [json.loads(line) for line in jobs.read_text(encoding="utf-8").splitlines() if line.strip()]
        failing = {}
        for j in recent:
            failing[j["job"]] = j if j["rc"] != 0 else None
        bad = {k: v for k, v in failing.items() if v}
        out["jobs"] = {"ok": not bad, "failing": {k: {"at": v["at"], "error": v.get("tail", "")[-400:]}
                                                  for k, v in bad.items()}}
    # "warning" checks (known, mitigated problems) are reported but don't fail overall health
    critical_ok = all(v.get("ok", True) for v in out.values() if v.get("severity") != "warning")
    return {"checked_at": iso_local(now), "timezone": "America/Toronto (EST/EDT)", "ok": critical_ok,
            "warnings": [k for k, v in out.items() if v.get("severity") == "warning" and not v.get("ok", True)],
            "checks": out}


def send_alert(subject: str, body: str):
    LOGS.mkdir(parents=True, exist_ok=True)
    with open(LOGS / "alerts.log", "a", encoding="utf-8") as fh:
        fh.write(f"{label()} {subject}\n{body}\n\n")
    log.error("ALERT %s: %s", subject, body)
    to, host = os.environ.get("ALERT_EMAIL_TO"), os.environ.get("SMTP_HOST")
    if not (to and host):
        return
    msg = EmailMessage()
    msg["Subject"] = f"[ferry-forecaster] {subject}"
    msg["From"] = os.environ.get("SMTP_FROM", os.environ.get("SMTP_USER", "ferry-forecaster@localhost"))
    msg["To"] = to
    msg.set_content(body)
    try:
        with smtplib.SMTP(host, int(os.environ.get("SMTP_PORT", "587")), timeout=30) as s:
            s.starttls()
            if os.environ.get("SMTP_USER"):
                s.login(os.environ["SMTP_USER"], os.environ.get("SMTP_PASSWORD", ""))
            s.send_message(msg)
    except Exception as exc:  # never let alerting crash the job
        log.error("email alert failed: %s", exc)


def run():
    h = checks()
    SERVING.mkdir(parents=True, exist_ok=True)
    tmp = SERVING / "health.json.tmp"
    tmp.write_text(json.dumps(h, indent=1, default=str))
    tmp.replace(SERVING / "health.json")
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    now = now_local()
    for name, c in h["checks"].items():
        prev = state.get(name, {"ok": True, "alerted_at": None})
        if not c.get("ok", True):
            last = prev.get("alerted_at")
            repeat = timedelta(hours=168 if c.get("severity") == "warning" else 24)
            if prev["ok"] or last is None or now - datetime.fromisoformat(last) > repeat:
                send_alert(f"{name} unhealthy", json.dumps(c, indent=1, default=str))
                prev = {"ok": False, "alerted_at": now.isoformat()}
            else:
                prev["ok"] = False
        else:
            if not prev["ok"]:
                send_alert(f"{name} recovered", json.dumps(c, indent=1, default=str))
            prev = {"ok": True, "alerted_at": None}
        state[name] = prev
    LOGS.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, indent=1))
    return h


if __name__ == "__main__":
    setup_logging()
    print(json.dumps(run()["ok"]))
