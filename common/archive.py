"""Append-only raw archive.

Every response from every source is written here *before* parsing:

    data/raw/<source>/<YYYY>/<MM>/<DD>/<fetched_utc>_<sha8>.<ext>.gz
    data/raw/<source>/manifest.jsonl      (one line per fetch)

Files are never overwritten or deleted. The payload is the exact response body,
gzip-compressed (decompress to get the original bytes). The manifest records the
URL, query parameters, HTTP status, byte size and SHA-256 of the uncompressed body.
"""

from __future__ import annotations

import gzip
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from common.config import RAW, TZ


@dataclass(frozen=True)
class RawRecord:
    source: str
    path: Path
    fetched_at: datetime
    sha256: str
    url: str
    params: dict[str, Any]

    def read(self) -> bytes:
        return gzip.decompress(self.path.read_bytes())


def save_raw(
    source: str,
    body: bytes,
    *,
    url: str,
    params: dict[str, Any] | None = None,
    status: int = 200,
    ext: str = "json",
    fetched_at: datetime | None = None,
    extra: dict[str, Any] | None = None,
) -> RawRecord:
    fetched_at = fetched_at or datetime.now(UTC)
    sha = hashlib.sha256(body).hexdigest()
    utc = fetched_at.astimezone(UTC)  # file names/folders in UTC: unambiguous, sortable
    stamp = utc.strftime("%Y%m%dT%H%M%S%fZ")
    day_dir = RAW / source / utc.strftime("%Y/%m/%d")
    day_dir.mkdir(parents=True, exist_ok=True)
    path = day_dir / f"{stamp}_{sha[:8]}.{ext}.gz"
    if path.exists():  # append-only: never clobber
        raise FileExistsError(path)
    tmp = path.with_suffix(path.suffix + ".part")
    with gzip.open(tmp, "wb", compresslevel=6) as fh:
        fh.write(body)
    tmp.rename(path)
    entry = {
        "fetched_at": fetched_at.astimezone(TZ).isoformat(),  # Eastern, with offset
        "source": source,
        "url": url,
        "params": params or {},
        "status": status,
        "bytes": len(body),
        "sha256": sha,
        "file": path.relative_to(RAW).as_posix(),
        **(extra or {}),
    }
    with open(RAW / source / "manifest.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")
    return RawRecord(source, path, fetched_at, sha, url, params or {})


def iter_manifest(source: str, usable_only: bool = False):
    """Manifest entries in fetch order. usable_only: HTTP 200 and not flagged invalid."""
    mf = RAW / source / "manifest.jsonl"
    if not mf.exists():
        return
    with open(mf, encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                e = json.loads(line)
                if usable_only and (e.get("status") != 200 or e.get("valid") is False):
                    continue
                yield e


def load_json(entry: dict[str, Any]):
    """Decode an archived JSON body; None (with a warning) if it isn't valid JSON."""
    try:
        return json.loads(load_raw(entry))
    except ValueError:
        import logging
        logging.getLogger(__name__).warning("skipping undecodable archived body %s", entry["file"])
        return None


def load_raw(entry: dict[str, Any]) -> bytes:
    return gzip.decompress((RAW / entry["file"]).read_bytes())


def latest(source: str, predicate=None) -> dict[str, Any] | None:
    out = None
    for e in iter_manifest(source):
        if predicate is None or predicate(e):
            out = e
    return out
