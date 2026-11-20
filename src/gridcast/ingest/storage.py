"""DuckDB helpers for raw ingestion and successful-range tracking."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import duckdb
import pandas as pd

UTC = timezone.utc  # noqa: UP017


def insert_rows_from_frame(
    connection: duckdb.DuckDBPyConnection,
    relation_name: str,
    column_names: list[str],
    rows: list[tuple[Any, ...]],
    insert_sql: str,
) -> None:
    """Insert a batch through DuckDB's vectorized Pandas integration."""
    if not rows:
        return
    frame = pd.DataFrame.from_records(rows, columns=column_names)
    connection.register(relation_name, frame)
    try:
        connection.execute(insert_sql)
    finally:
        connection.unregister(relation_name)


def ensure_raw_schema(connection: duckdb.DuckDBPyConnection) -> None:
    """Create the raw schema and the range ledger used for incremental loads."""
    connection.execute("SET TimeZone = 'UTC'")
    connection.execute("CREATE SCHEMA IF NOT EXISTS raw")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS raw.ingestion_coverage (
            source VARCHAR NOT NULL,
            data_id VARCHAR NOT NULL,
            start_time TIMESTAMPTZ NOT NULL,
            end_time TIMESTAMPTZ NOT NULL,
            PRIMARY KEY (source, data_id, start_time, end_time)
        )
        """
    )


def get_coverage(
    connection: duckdb.DuckDBPyConnection,
    source: str,
    data_id: str,
) -> list[tuple[datetime, datetime]]:
    """Return successful UTC request ranges for one source and identifier."""
    ensure_raw_schema(connection)
    rows = connection.execute(
        """
        SELECT CAST(start_time AS VARCHAR), CAST(end_time AS VARCHAR)
        FROM raw.ingestion_coverage
        WHERE source = ? AND data_id = ?
        ORDER BY start_time, end_time
        """,
        [source, data_id],
    ).fetchall()
    return [
        (_parse_utc_timestamp(row[0]), _parse_utc_timestamp(row[1])) for row in rows
    ]


def _parse_utc_timestamp(value: str) -> datetime:
    """Parse DuckDB's compact UTC offset in a Python-version-neutral way."""
    normalized = value.replace(" ", "T")
    if len(normalized) >= 3 and normalized[-3] in "+-" and normalized[-2:].isdigit():
        normalized = f"{normalized}:00"
    return datetime.fromisoformat(normalized).astimezone(UTC)


def find_missing_ranges(
    start_time: datetime,
    end_time: datetime,
    coverage: list[tuple[datetime, datetime]],
) -> list[tuple[datetime, datetime]]:
    """Subtract successful ranges from a requested half-open UTC interval."""
    if end_time <= start_time:
        return []

    missing: list[tuple[datetime, datetime]] = []
    cursor = start_time
    for covered_start, covered_end in sorted(coverage):
        if covered_end <= cursor:
            continue
        if covered_start >= end_time:
            break
        if covered_start > cursor:
            missing.append((cursor, min(covered_start, end_time)))
        cursor = max(cursor, covered_end)
        if cursor >= end_time:
            break
    if cursor < end_time:
        missing.append((cursor, end_time))
    return missing


def add_coverage(
    connection: duckdb.DuckDBPyConnection,
    source: str,
    data_id: str,
    start_time: datetime,
    end_time: datetime,
) -> None:
    """Record a request range only after its data has been stored."""
    connection.execute(
        """
        INSERT INTO raw.ingestion_coverage
            (source, data_id, start_time, end_time)
        VALUES (?, ?, ?, ?)
        ON CONFLICT DO NOTHING
        """,
        [source, data_id, start_time, end_time],
    )
