"""Daily CLI for archiving FMI's next-day temperature forecast."""

from __future__ import annotations

import os
from pathlib import Path

import duckdb

from gridcast.config import REPO_ROOT
from gridcast.ingest.fmi import FmiClient, snapshot_fmi_next_day_forecast


def run_forecast_snapshot(
    database_path: Path | None = None,
    cache_path: Path | None = None,
) -> int:
    """Persist one forecast vintage for use by the online predictor."""
    db_path = database_path or Path(
        os.environ.get("GRIDCAST_DATABASE_PATH", REPO_ROOT / "data" / "gridcast.duckdb")
    )
    cache_dir = cache_path or REPO_ROOT / "data" / "raw_cache"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with duckdb.connect(str(db_path)) as connection:
        result = snapshot_fmi_next_day_forecast(connection, FmiClient(cache_dir))
    print(
        f"Saved {result.rows_written} FMI forecast temperatures for "
        f"{result.forecast_date} (downloaded={result.downloaded})."
    )
    return result.rows_written


def main() -> None:
    """Run the daily forecast archive job."""
    run_forecast_snapshot()


if __name__ == "__main__":
    main()
