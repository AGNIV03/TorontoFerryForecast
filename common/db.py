"""DuckDB connection helper (single-file analytical store, rebuilt from the raw archive)."""

from __future__ import annotations

import duckdb

from common.config import DB_PATH


def connect(read_only: bool = False) -> duckdb.DuckDBPyConnection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(DB_PATH), read_only=read_only)
    con.execute("SET TimeZone='UTC'")
    return con


def table(name: str):
    with connect(read_only=True) as con:
        return con.execute(f"SELECT * FROM {name}").df()
