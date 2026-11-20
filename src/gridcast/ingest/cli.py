"""Command line entry point for the M1 data backfill."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import duckdb

from gridcast.config import (
    FMI_CHUNK_DAYS,
    HISTORY_YEARS,
    INGESTION_CHUNK_DAYS,
    Settings,
    load_settings,
)
from gridcast.ingest.fingrid import (
    FingridClient,
    _history_range,
    ingest_fingrid,
)
from gridcast.ingest.fmi import FmiClient, ingest_fmi_weather

UTC = timezone.utc  # noqa: UP017


def run_ingestion(settings: Settings, now: datetime | None = None) -> None:
    """Backfill consumption and hourly weather to the local DuckDB warehouse."""
    settings.database_path.parent.mkdir(parents=True, exist_ok=True)
    settings.cache_path.mkdir(parents=True, exist_ok=True)
    end_now = datetime.now(UTC) if now is None else now
    start_time, end_time = _history_range(end_now, HISTORY_YEARS)

    connection = duckdb.connect(str(settings.database_path))
    try:
        fingrid = FingridClient(
            settings.api_key,
            settings.cache_path,
            timeout_seconds=settings.request_timeout_seconds,
            max_attempts=settings.http_max_attempts,
        )
        fmi = FmiClient(
            settings.cache_path,
            timeout_seconds=settings.request_timeout_seconds,
            max_attempts=settings.http_max_attempts,
        )
        consumption = ingest_fingrid(
            connection,
            fingrid,
            start_time,
            end_time,
            chunk_days=INGESTION_CHUNK_DAYS,
        )
        weather = ingest_fmi_weather(
            connection,
            fmi,
            start_time.replace(minute=0, second=0, microsecond=0),
            end_time.replace(minute=0, second=0, microsecond=0),
            chunk_days=FMI_CHUNK_DAYS,
        )
    finally:
        connection.close()

    print(
        "Fingrid dataset 124: "
        f"{consumption.rows_written} rows stored; "
        f"{consumption.pages_downloaded} pages downloaded."
    )
    print(
        "FMI hourly weather: "
        f"{weather.rows_written} rows stored; "
        f"{weather.requests_downloaded} responses downloaded."
    )


def main() -> None:
    """Load local settings and run ingestion."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    settings = load_settings()
    run_ingestion(settings)


if __name__ == "__main__":
    main()
