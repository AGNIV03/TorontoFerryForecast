"""HTTP helper: every fetch is archived raw before it is returned."""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import httpx

from common.archive import RawRecord, save_raw
from common.config import USER_AGENT

log = logging.getLogger(__name__)

_client: httpx.Client | None = None


def client() -> httpx.Client:
    global _client
    if _client is None:
        _client = httpx.Client(
            headers={"User-Agent": USER_AGENT},
            timeout=httpx.Timeout(120.0, connect=20.0),
            follow_redirects=True,
        )
    return _client


def fetch(
    source: str,
    url: str,
    params: dict[str, Any] | None = None,
    *,
    ext: str = "json",
    retries: int = 4,
    backoff: float = 5.0,
    extra: dict[str, Any] | None = None,
) -> RawRecord:
    """GET url, archive the body (even for non-2xx), raise on HTTP error."""
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            r = client().get(url, params=params)
        except httpx.TransportError as exc:
            last_exc = exc
            log.warning("transport error %s (attempt %d): %s", url, attempt + 1, exc)
            time.sleep(backoff * (attempt + 1))
            continue
        if r.status_code == 429 or r.status_code >= 500:
            log.warning("HTTP %s from %s (attempt %d)", r.status_code, url, attempt + 1)
            last_exc = httpx.HTTPStatusError(f"{r.status_code}", request=r.request, response=r)
            time.sleep(backoff * (attempt + 1) * (6 if r.status_code == 429 else 1))
            continue
        valid = True
        if ext == "json" and r.status_code == 200 and len(r.content) < 50_000_000:
            try:
                json.loads(r.content)
            except ValueError:
                valid = False  # e.g. Open-Meteo "Unexpected error while streaming data" with HTTP 200
        rec = save_raw(source, r.content, url=str(r.url).split("?")[0], params=params,
                       status=r.status_code, ext=ext, extra={**(extra or {}), **({} if valid else {"valid": False})})
        r.raise_for_status()
        if not valid:
            last_exc = ValueError(f"invalid JSON body: {r.content[:120]!r}")
            log.warning("invalid JSON body from %s (attempt %d), archived and retrying", url, attempt + 1)
            time.sleep(backoff * (attempt + 1))
            continue
        return rec
    raise RuntimeError(f"giving up on {url}: {last_exc}")
