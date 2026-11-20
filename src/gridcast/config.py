"""Project settings and official source identifiers."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# Dataset 124 is Fingrid's "Electricity consumption in Finland" series:
# https://data.fingrid.fi/en/datasets/124
FINGRID_DATASET_ID = 124
# The GetDatasetData operation is documented in Fingrid's developer portal:
# https://developer-data.fingrid.fi/api-details#api=avoindata-api&operation=GetDatasetData
FINGRID_API_BASE_URL = "https://data.fingrid.fi/api"
FINGRID_PAGE_SIZE = 20_000
FINGRID_MIN_REQUEST_INTERVAL_SECONDS = 2.0

# FMI documents this WFS URL, stored query, repeated place parameters, and the
# temperature, timestep, starttime, and endtime arguments here:
# https://en.ilmatieteenlaitos.fi/open-data-manual-wfs-examples-and-guidelines
FMI_WFS_URL = "http://opendata.fmi.fi/wfs"
FMI_WEATHER_STORED_QUERY = "fmi::observations::weather::timevaluepair"
FMI_TEMPERATURE_PARAMETER = "t2m"
FMI_HOURLY_TIMESTEP_MINUTES = 60
FMI_PLACES = ("helsinki", "tampere", "turku", "oulu", "jyvaskyla")
FMI_CHUNK_DAYS = 7
FMI_REQUEST_WORKERS = 4

# 2025 population counts for the five weather-weighted municipalities.
# Statistics Finland, Finland in Figures population tables:
# https://stat.fi/fi/tilastot/tietoa-teemoittain/suomi-lukuina/vaesto-ja-yhteiskunta
WEATHER_PLACE_POPULATIONS = {
    "helsinki": 694_392,
    "tampere": 263_337,
    "turku": 209_633,
    "oulu": 217_469,
    "jyvaskyla": 149_895,
}
WEATHER_POPULATION_REFERENCE_YEAR = 2025

HISTORY_YEARS = 3
INGESTION_CHUNK_DAYS = 180
HTTP_TIMEOUT_SECONDS = 60
HTTP_MAX_ATTEMPTS = 5


@dataclass(frozen=True)
class Settings:
    """Runtime settings, with the API key hidden from repr output."""

    api_key: str = field(repr=False)
    repo_root: Path
    database_path: Path
    cache_path: Path
    request_timeout_seconds: int = HTTP_TIMEOUT_SECONDS
    http_max_attempts: int = HTTP_MAX_ATTEMPTS


def _parse_env_file(path: Path) -> dict[str, str]:
    """Read simple KEY=VALUE entries without printing their values."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values

    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, value = stripped.split("=", maxsplit=1)
        values[name.strip()] = value.strip().strip('"').strip("'")
    return values


def load_settings(
    repo_root: Path = REPO_ROOT,
    environ: Mapping[str, str] | None = None,
) -> Settings:
    """Load the Fingrid key and local storage paths."""
    env_path = repo_root / ".env"
    if not env_path.is_file() and (repo_root / ".env.txt").is_file():
        # Some Windows editors append .txt to a dotfile; keep .env as preferred.
        env_path = repo_root / ".env.txt"

    values = _parse_env_file(env_path)
    values.update(dict(os.environ if environ is None else environ))
    api_key = values.get("FINGRID_API_KEY") or values.get("API_KEY")
    if not api_key:
        raise RuntimeError(
            "FINGRID_API_KEY is missing; set it in .env or the process environment."
        )

    data_path = repo_root / "data"
    return Settings(
        api_key=api_key,
        repo_root=repo_root,
        database_path=data_path / "gridcast.duckdb",
        cache_path=data_path / "raw_cache",
    )
