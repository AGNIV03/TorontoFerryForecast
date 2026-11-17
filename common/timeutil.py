"""Eastern time (America/Toronto, EST/EDT) for everything a person reads.

Internal columns named *_utc and raw-archive file names stay in UTC (unambiguous across
the November fall-back hour); every timestamp written to JSON, logs, alerts, reports and the
archive manifest is Eastern with an explicit offset, e.g. 2026-11-16T19:39:58-05:00.
"""

from __future__ import annotations

import logging
from datetime import datetime

import pandas as pd

from common.config import TZ


def now_local() -> datetime:
    return datetime.now(TZ)


def iso_local(ts) -> str | None:
    """Any timestamp (aware, or naive meaning UTC) -> ISO string in Eastern time with offset."""
    if ts is None or (isinstance(ts, float) and pd.isna(ts)):
        return None
    t = pd.Timestamp(ts)
    if pd.isna(t):
        return None
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    return t.tz_convert(TZ).isoformat()


def label(ts=None) -> str:
    """'2026-11-16 19:39 EST' for reports and messages."""
    t = pd.Timestamp(ts) if ts is not None else pd.Timestamp(now_local())
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    return t.tz_convert(TZ).strftime("%Y-%m-%d %H:%M %Z")


class _EasternFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):  # noqa: N802
        return datetime.fromtimestamp(record.created, TZ).strftime("%Y-%m-%d %H:%M:%S %Z")


def setup_logging(level=logging.INFO):
    h = logging.StreamHandler()
    h.setFormatter(_EasternFormatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [h]
    root.setLevel(level)
    logging.getLogger("httpx").setLevel(logging.WARNING)
