"""Build dbt seed tables from maintained population and holiday sources."""

from __future__ import annotations

import csv
from pathlib import Path

import holidays

from gridcast.config import WEATHER_PLACE_POPULATIONS

REPO_ROOT = Path(__file__).resolve().parents[3]
SEED_DIR = REPO_ROOT / "dbt" / "seeds"
HOLIDAY_START_YEAR = 2020
HOLIDAY_END_YEAR = 2040


def write_population_seed() -> None:
    """Write the versioned city population weights used by dbt models."""
    path = SEED_DIR / "population_weights.csv"
    with path.open("w", newline="", encoding="utf-8") as output:
        writer = csv.writer(output)
        writer.writerow(["place", "population"])
        writer.writerows(sorted(WEATHER_PLACE_POPULATIONS.items()))


def write_holiday_seed() -> None:
    """Write Finnish public holidays for the backfill and forecast horizon."""
    years = range(HOLIDAY_START_YEAR, HOLIDAY_END_YEAR + 1)
    public_holidays = holidays.country_holidays("FI", years=years)
    path = SEED_DIR / "finnish_holidays.csv"
    with path.open("w", newline="", encoding="utf-8") as output:
        writer = csv.writer(output)
        writer.writerow(["holiday_date", "holiday_name"])
        writer.writerows(
            (holiday_date.isoformat(), name)
            for holiday_date, name in sorted(public_holidays.items())
        )


def build_seeds() -> None:
    """Create all deterministic source seed files for dbt."""
    SEED_DIR.mkdir(parents=True, exist_ok=True)
    write_population_seed()
    write_holiday_seed()


if __name__ == "__main__":
    build_seeds()
