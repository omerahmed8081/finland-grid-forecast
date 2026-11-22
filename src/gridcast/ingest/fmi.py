"""FMI WFS hourly temperature client and incremental DuckDB ingestion."""

from __future__ import annotations

import logging
import time
import unicodedata
import xml.etree.ElementTree as ET
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import duckdb

from gridcast.config import (
    FMI_CHUNK_DAYS,
    FMI_FORECAST_STORED_QUERY,
    FMI_FORECAST_TEMPERATURE_PARAMETER,
    FMI_HOURLY_TIMESTEP_MINUTES,
    FMI_PLACES,
    FMI_REQUEST_WORKERS,
    FMI_TEMPERATURE_PARAMETER,
    FMI_WEATHER_STORED_QUERY,
    FMI_WFS_URL,
    HTTP_MAX_ATTEMPTS,
    HTTP_TIMEOUT_SECONDS,
)
from gridcast.ingest.fingrid import parse_utc
from gridcast.ingest.storage import (
    add_coverage,
    ensure_raw_schema,
    find_missing_ranges,
    get_coverage,
    insert_rows_from_frame,
)

UTC = timezone.utc  # noqa: UP017
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class FmiFetchResult:
    """Hourly temperature observations and network download indicator."""

    observations: list[dict[str, Any]]
    downloaded: bool


@dataclass(frozen=True)
class FmiIngestionResult:
    """Summary of an FMI weather ingestion run."""

    rows_written: int
    requests_downloaded: int


@dataclass(frozen=True)
class FmiForecastSnapshotResult:
    """Summary of an archived next-day FMI forecast snapshot."""

    rows_written: int
    forecast_date: date
    downloaded: bool


class FmiClient:
    """Retrieve cached or live hourly FMI temperature observations."""

    def __init__(
        self,
        cache_dir: Path,
        *,
        base_url: str = FMI_WFS_URL,
        timeout_seconds: int = HTTP_TIMEOUT_SECONDS,
        max_attempts: int = HTTP_MAX_ATTEMPTS,
    ) -> None:
        self._cache_dir = cache_dir
        self._base_url = base_url
        self._timeout_seconds = timeout_seconds
        self._max_attempts = max_attempts

    def fetch_range(
        self,
        places: tuple[str, ...],
        start_time: datetime,
        end_time: datetime,
    ) -> FmiFetchResult:
        """Fetch hourly observations for multiple requested places in one call."""
        if not places:
            raise ValueError("At least one FMI place is required.")
        params = [
            ("service", "WFS"),
            ("version", "2.0.0"),
            ("request", "GetFeature"),
            ("storedquery_id", FMI_WEATHER_STORED_QUERY),
        ]
        params.extend(("place", place) for place in places)
        params.extend(
            (
                ("parameters", FMI_TEMPERATURE_PARAMETER),
                ("timestep", str(FMI_HOURLY_TIMESTEP_MINUTES)),
                ("starttime", _format_utc(start_time)),
                ("endtime", _format_utc(end_time)),
            )
        )
        url = f"{self._base_url}?{urlencode(params)}"
        cache_dir = self._cache_dir / "fmi"
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_path = cache_dir / _cache_name(places, start_time, end_time)
        if cache_path.is_file():
            content = cache_path.read_bytes()
            downloaded = False
        else:
            content = self._request(url)
            cache_path.write_bytes(content)
            downloaded = True
        return FmiFetchResult(
            observations=parse_observations(content, places),
            downloaded=downloaded,
        )

    def fetch_forecast_range(
        self,
        places: tuple[str, ...],
        start_time: datetime,
        end_time: datetime,
    ) -> FmiFetchResult:
        """Fetch current HARMONIE forecast values for the requested times."""
        if not places:
            raise ValueError("At least one FMI place is required.")
        params = [
            ("service", "WFS"),
            ("version", "2.0.0"),
            ("request", "GetFeature"),
            ("storedquery_id", FMI_FORECAST_STORED_QUERY),
        ]
        params.extend(("place", place) for place in places)
        params.extend(
            [
                ("parameters", FMI_FORECAST_TEMPERATURE_PARAMETER),
                ("timestep", str(FMI_HOURLY_TIMESTEP_MINUTES)),
                ("starttime", _format_utc(start_time)),
                ("endtime", _format_utc(end_time)),
            ]
        )
        url = f"{self._base_url}?{urlencode(params)}"
        cache_dir = self._cache_dir / "fmi" / "forecasts"
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_path = cache_dir / _cache_name(places, start_time, end_time)
        if cache_path.is_file():
            content = cache_path.read_bytes()
            downloaded = False
        else:
            content = self._request(url)
            cache_path.write_bytes(content)
            downloaded = True
        observations = parse_observations(content, places)
        for observation in observations:
            observation["parameter"] = FMI_FORECAST_TEMPERATURE_PARAMETER
        return FmiFetchResult(observations, downloaded)

    def _request(self, url: str) -> bytes:
        """Issue a retrying public FMI WFS request."""
        for attempt in range(self._max_attempts):
            request = Request(url, headers={"User-Agent": "gridcast/0.1"})
            try:
                with urlopen(request, timeout=self._timeout_seconds) as response:
                    return response.read()
            except HTTPError as error:
                retryable = error.code == 429 or error.code in {500, 502, 503, 504}
                if not retryable:
                    raise RuntimeError(
                        f"FMI request failed with HTTP {error.code}."
                    ) from None
                if attempt + 1 == self._max_attempts:
                    raise RuntimeError(
                        f"FMI request failed after {self._max_attempts} attempts."
                    ) from None
                _backoff(attempt)
            except (URLError, TimeoutError) as error:
                if attempt + 1 == self._max_attempts:
                    raise RuntimeError(
                        f"FMI request failed after {self._max_attempts} attempts."
                    ) from error
                _backoff(attempt)
        raise AssertionError("Unreachable retry loop exit.")


def parse_observations(content: bytes, places: tuple[str, ...]) -> list[dict[str, Any]]:
    """Extract WML2 time-value pairs from an FMI timevaluepair response."""
    try:
        root = ET.fromstring(content)
    except ET.ParseError as error:
        raise ValueError("FMI returned invalid XML.") from error
    if any(_local_name(element.tag) == "Exception" for element in root.iter()):
        raise ValueError("FMI returned a WFS exception response.")

    observations: list[dict[str, Any]] = []
    members = [
        element for element in root.iter() if _local_name(element.tag) == "member"
    ]
    for member in members:
        series = [
            element
            for element in member.iter()
            if _local_name(element.tag) == "MeasurementTimeseries"
        ]
        if not series:
            continue
        station_name = _first_text(member, "name")
        place = _match_place(station_name, places)
        for time_series in series:
            observations.extend(_observations_from_series(time_series, place))
    return observations


def _observations_from_series(
    time_series: ET.Element, place: str
) -> list[dict[str, Any]]:
    """Parse hourly pairs from one FMI MeasurementTimeseries element."""
    observations: list[dict[str, Any]] = []
    for point in time_series.iter():
        if _local_name(point.tag) != "point":
            continue
        time_value = next(
            (child for child in point if _local_name(child.tag) == "MeasurementTVP"),
            None,
        )
        if time_value is None:
            continue
        timestamp_text = _child_text(time_value, "time")
        if timestamp_text is None:
            continue
        observations.append(
            {
                "place": place,
                "observed_at": parse_utc(timestamp_text),
                "temperature_c": _optional_temperature(
                    _child_text(time_value, "value")
                ),
                "parameter": FMI_TEMPERATURE_PARAMETER,
            }
        )
    return observations


def _first_text(parent: ET.Element, name: str) -> str | None:
    """Return the first descendant's text with the requested local tag name."""
    for element in parent.iter():
        if _local_name(element.tag) == name and element.text:
            return element.text.strip()
    return None


def _match_place(station_name: str | None, places: tuple[str, ...]) -> str:
    """Map FMI station metadata back to its requested city place."""
    if station_name:
        station_key = _normalize_place(station_name)
        for place in places:
            if station_key.startswith(_normalize_place(place)):
                return place
    if len(places) == 1:
        return places[0]
    raise ValueError("FMI station metadata does not match a requested place.")


def _normalize_place(value: str) -> str:
    """Normalize a location name for matching Finnish diacritics and spacing."""
    ascii_name = (
        unicodedata.normalize("NFKD", value).encode("ascii", errors="ignore").decode()
    )
    return "".join(
        character for character in ascii_name.casefold() if character.isalnum()
    )


def _local_name(tag: str) -> str:
    """Return an XML tag without its namespace."""
    return tag.rsplit("}", maxsplit=1)[-1]


def _child_text(parent: ET.Element, name: str) -> str | None:
    """Return the text of the first direct child with a local tag name."""
    child = next((item for item in parent if _local_name(item.tag) == name), None)
    return child.text.strip() if child is not None and child.text else None


def _optional_temperature(value: str | None) -> float | None:
    """Convert a valid numeric measurement, preserving FMI nil values."""
    if value is None or value.lower() in {"nan", "nil", "null"}:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _format_utc(value: datetime) -> str:
    """Format an FMI request timestamp in UTC."""
    if value.tzinfo is None:
        raise ValueError("FMI request timestamps must include a timezone.")
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _cache_name(
    places: tuple[str, ...], start_time: datetime, end_time: datetime
) -> str:
    """Build a filesystem-safe cache name for places and a time interval."""
    start_key = start_time.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    end_key = end_time.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    places_key = "_".join(sorted(places))
    return f"{places_key}_{start_key}_{end_key}.xml"


def _backoff(attempt: int) -> None:
    """Wait with exponential backoff between FMI retry attempts."""
    time.sleep(2.0**attempt)


def ingest_fmi_weather(
    connection: duckdb.DuckDBPyConnection,
    client: FmiClient,
    start_time: datetime,
    end_time: datetime,
    *,
    places: tuple[str, ...] = FMI_PLACES,
    chunk_days: int = FMI_CHUNK_DAYS,
) -> FmiIngestionResult:
    """Incrementally ingest hourly readings with grouped place/time requests."""
    ensure_raw_schema(connection)
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS raw.fmi_weather (
            place VARCHAR NOT NULL,
            observed_at TIMESTAMPTZ NOT NULL,
            temperature_c DOUBLE,
            parameter VARCHAR NOT NULL,
            PRIMARY KEY (place, observed_at, parameter)
        )
        """
    )
    pending_ranges: dict[tuple[datetime, datetime], list[str]] = {}
    for place in places:
        coverage = get_coverage(connection, "fmi", place)
        missing_ranges = find_missing_ranges(start_time, end_time, coverage)
        for missing_start, missing_end in missing_ranges:
            cursor = missing_start
            while cursor < missing_end:
                chunk_end = min(cursor + timedelta(days=chunk_days), missing_end)
                pending_ranges.setdefault((cursor, chunk_end), []).append(place)
                cursor = chunk_end

    tasks = [
        (start, end, tuple(chunk_places))
        for (start, end), chunk_places in sorted(pending_ranges.items())
    ]
    rows_written = 0
    requests_downloaded = 0
    with ThreadPoolExecutor(max_workers=FMI_REQUEST_WORKERS) as executor:
        futures: dict[
            Future[FmiFetchResult], tuple[datetime, datetime, tuple[str, ...]]
        ] = {
            executor.submit(client.fetch_range, task[2], task[0], task[1]): task
            for task in tasks
        }
        for completed, future in enumerate(as_completed(futures), start=1):
            chunk_start, chunk_end, requested_places = futures[future]
            result = future.result()
            rows_written += _store_observations(
                connection,
                result.observations,
                requested_places,
                chunk_start,
                chunk_end,
            )
            requests_downloaded += int(result.downloaded)
            if completed % 25 == 0 or completed == len(tasks):
                LOGGER.info(
                    "FMI backfill requests completed: %d/%d", completed, len(tasks)
                )
    return FmiIngestionResult(rows_written, requests_downloaded)


def snapshot_fmi_next_day_forecast(
    connection: duckdb.DuckDBPyConnection,
    client: FmiClient,
    *,
    now: datetime | None = None,
    places: tuple[str, ...] = FMI_PLACES,
) -> FmiForecastSnapshotResult:
    """Fetch and persist one vintage of next-local-day forecast temperatures."""
    issue = datetime.now(UTC) if now is None else now.astimezone(UTC)
    local_day = issue.astimezone(ZoneInfo("Europe/Helsinki")).date() + timedelta(days=1)
    start_time = datetime.combine(
        local_day, datetime.min.time(), ZoneInfo("Europe/Helsinki")
    ).astimezone(UTC)
    end_time = datetime.combine(
        local_day + timedelta(days=1), datetime.min.time(), ZoneInfo("Europe/Helsinki")
    ).astimezone(UTC)
    result = client.fetch_forecast_range(places, start_time, end_time)
    if not result.observations:
        raise RuntimeError("FMI returned no temperature forecast values for next day.")
    connection.execute("CREATE SCHEMA IF NOT EXISTS raw")
    connection.execute("""CREATE TABLE IF NOT EXISTS raw.fmi_weather_forecasts (
        forecast_origin_utc TIMESTAMPTZ NOT NULL,
        forecast_for_utc TIMESTAMPTZ NOT NULL,
        place VARCHAR NOT NULL,
        temperature_c DOUBLE,
        parameter VARCHAR NOT NULL,
        PRIMARY KEY (forecast_origin_utc, forecast_for_utc, place, parameter))""")
    rows = [
        (
            issue,
            item["observed_at"],
            item["place"],
            item["temperature_c"],
            item["parameter"],
        )
        for item in result.observations
        if start_time <= item["observed_at"] < end_time
    ]
    connection.executemany(
        """INSERT INTO raw.fmi_weather_forecasts VALUES (?, ?, ?, ?, ?)
        ON CONFLICT DO UPDATE SET temperature_c=excluded.temperature_c""",
        rows,
    )
    return FmiForecastSnapshotResult(len(rows), local_day, result.downloaded)


def _store_observations(
    connection: duckdb.DuckDBPyConnection,
    observations: list[dict[str, Any]],
    places: tuple[str, ...],
    start_time: datetime,
    end_time: datetime,
) -> int:
    """Store one query response and mark its requested places as covered."""
    connection.execute("BEGIN TRANSACTION")
    try:
        rows = [
            (
                observation["place"],
                observation["observed_at"].isoformat(),
                observation["temperature_c"],
                observation["parameter"],
            )
            for observation in observations
        ]
        if rows:
            insert_rows_from_frame(
                connection,
                "incoming_fmi_weather",
                ["place", "observed_at", "temperature_c", "parameter"],
                rows,
                """
                INSERT INTO raw.fmi_weather
                    (place, observed_at, temperature_c, parameter)
                SELECT
                    place,
                    CAST(observed_at AS TIMESTAMPTZ),
                    temperature_c,
                    parameter
                FROM incoming_fmi_weather
                ON CONFLICT (place, observed_at, parameter) DO UPDATE SET
                    temperature_c = excluded.temperature_c
                """,
            )
        for place in places:
            add_coverage(connection, "fmi", place, start_time, end_time)
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise
    return len(observations)
