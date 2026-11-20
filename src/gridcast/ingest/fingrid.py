"""Fingrid GetDatasetData client and incremental DuckDB ingestion."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from hashlib import sha256
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import duckdb

from gridcast.config import (
    FINGRID_API_BASE_URL,
    FINGRID_DATASET_ID,
    FINGRID_MIN_REQUEST_INTERVAL_SECONDS,
    FINGRID_PAGE_SIZE,
    HTTP_MAX_ATTEMPTS,
    HTTP_TIMEOUT_SECONDS,
    INGESTION_CHUNK_DAYS,
)
from gridcast.ingest.storage import (
    add_coverage,
    ensure_raw_schema,
    find_missing_ranges,
    get_coverage,
    insert_rows_from_frame,
)

UTC = timezone.utc  # noqa: UP017


@dataclass(frozen=True)
class FetchResult:
    """Records fetched from one requested range and network page count."""

    records: list[dict[str, Any]]
    pages_downloaded: int


@dataclass(frozen=True)
class IngestionResult:
    """Summary of a dataset ingestion run."""

    rows_written: int
    pages_downloaded: int


def parse_utc(value: str) -> datetime:
    """Parse an ISO timestamp and normalize it to timezone-aware UTC."""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Expected a timezone-aware timestamp from Fingrid.")
    return parsed.astimezone(UTC)


def _format_utc(value: datetime) -> str:
    """Format a datetime as the ISO UTC timestamp expected by Fingrid."""
    if value.tzinfo is None:
        raise ValueError("Fingrid request timestamps must include a timezone.")
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")


class FingridClient:
    """Small HTTP client with paging, retry, pacing, and raw response caching."""

    def __init__(
        self,
        api_key: str,
        cache_dir: Path,
        *,
        base_url: str = FINGRID_API_BASE_URL,
        page_size: int = FINGRID_PAGE_SIZE,
        timeout_seconds: int = HTTP_TIMEOUT_SECONDS,
        max_attempts: int = HTTP_MAX_ATTEMPTS,
        minimum_request_interval: float = FINGRID_MIN_REQUEST_INTERVAL_SECONDS,
    ) -> None:
        self._api_key = api_key
        self._cache_dir = cache_dir
        self._base_url = base_url.rstrip("/")
        self._page_size = page_size
        self._timeout_seconds = timeout_seconds
        self._max_attempts = max_attempts
        self._minimum_request_interval = minimum_request_interval
        self._last_request_at: float | None = None

    def fetch_range(
        self,
        start_time: datetime,
        end_time: datetime,
        dataset_id: int = FINGRID_DATASET_ID,
    ) -> FetchResult:
        """Fetch all pages for a half-open dataset interval."""
        if end_time <= start_time:
            return FetchResult(records=[], pages_downloaded=0)

        records: list[dict[str, Any]] = []
        pages_downloaded = 0
        page = 1
        while True:
            page_records, has_next_page, downloaded = self._fetch_page(
                dataset_id, start_time, end_time, page
            )
            records.extend(page_records)
            pages_downloaded += int(downloaded)
            if not has_next_page:
                break
            page += 1
        return FetchResult(records=records, pages_downloaded=pages_downloaded)

    def _fetch_page(
        self,
        dataset_id: int,
        start_time: datetime,
        end_time: datetime,
        page: int,
    ) -> tuple[list[dict[str, Any]], bool, bool]:
        """Load one response page from cache or Fingrid."""
        params = {
            "startTime": _format_utc(start_time),
            "endTime": _format_utc(end_time),
            "page": str(page),
            "pageSize": str(self._page_size),
            "format": "json",
            "sortBy": "startTime",
            "sortOrder": "asc",
        }
        url = f"{self._base_url}/datasets/{dataset_id}/data?{urlencode(params)}"
        cache_path = self._cache_path(url, dataset_id)
        if cache_path.is_file():
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
            records, has_next_page = self._records_from_payload(
                payload, self._page_size
            )
            return records, has_next_page, False

        body = self._request(url)
        payload = json.loads(body.decode("utf-8"))
        records, has_next_page = self._records_from_payload(payload, self._page_size)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(payload), encoding="utf-8")
        return records, has_next_page, True

    def _cache_path(self, url: str, dataset_id: int) -> Path:
        """Build a filesystem-safe deterministic raw response cache path."""
        digest = sha256(url.encode("utf-8")).hexdigest()
        return self._cache_dir / "fingrid" / f"dataset_{dataset_id}_{digest}.json"

    @staticmethod
    def _records_from_payload(
        payload: Any, page_size: int
    ) -> tuple[list[dict[str, Any]], bool]:
        """Validate the GetDatasetData envelope and report its paging state."""
        pagination: dict[str, Any] = {}
        if isinstance(payload, dict):
            records = payload.get("data")
            metadata = payload.get("pagination", {})
            if not isinstance(metadata, dict):
                raise ValueError("Fingrid pagination metadata must be a JSON object.")
            pagination = metadata
        elif isinstance(payload, list):
            records = payload
        else:
            raise ValueError("Fingrid response must be a JSON list or envelope.")
        if not isinstance(records, list):
            raise ValueError("Fingrid response data must be a JSON list.")
        if not all(isinstance(item, dict) for item in records):
            raise ValueError("Fingrid response rows must be JSON objects.")

        next_page = pagination.get("nextPage")
        current_page = pagination.get("currentPage")
        last_page = pagination.get("lastPage")
        if next_page is not None:
            has_next_page = True
        elif current_page is not None and last_page is not None:
            has_next_page = int(current_page) < int(last_page)
        else:
            has_next_page = len(records) >= page_size
        return records, has_next_page

    def _request(self, url: str) -> bytes:
        """Issue a paced request and retry transient HTTP or network errors."""
        for attempt in range(self._max_attempts):
            self._pace_request()
            request = Request(url, headers={"x-api-key": self._api_key})
            try:
                with urlopen(request, timeout=self._timeout_seconds) as response:
                    return response.read()
            except HTTPError as error:
                if not self._is_retryable_status(error.code):
                    raise RuntimeError(
                        f"Fingrid request failed with HTTP {error.code}."
                    ) from None
                if attempt + 1 == self._max_attempts:
                    raise RuntimeError(
                        f"Fingrid request failed after {self._max_attempts} attempts."
                    ) from None
                self._sleep_before_retry(attempt, error.headers.get("Retry-After"))
            except (URLError, TimeoutError) as error:
                if attempt + 1 == self._max_attempts:
                    raise RuntimeError(
                        f"Fingrid request failed after {self._max_attempts} attempts."
                    ) from error
                self._sleep_before_retry(attempt, None)
        raise AssertionError("Unreachable retry loop exit.")

    @staticmethod
    def _is_retryable_status(status: int) -> bool:
        return status == 429 or status in {500, 502, 503, 504}

    def _pace_request(self) -> None:
        """Honor Fingrid's published two-second interval between requests."""
        if self._last_request_at is None:
            self._last_request_at = time.monotonic()
            return
        elapsed = time.monotonic() - self._last_request_at
        delay = self._minimum_request_interval - elapsed
        if delay > 0:
            time.sleep(delay)
        self._last_request_at = time.monotonic()

    @staticmethod
    def _sleep_before_retry(attempt: int, retry_after: str | None) -> None:
        """Use Retry-After when valid, otherwise exponential backoff."""
        delay: float | None = None
        if retry_after:
            try:
                delay = max(0.0, float(retry_after))
            except ValueError:
                try:
                    retry_time = parsedate_to_datetime(retry_after)
                    delay = max(
                        0.0,
                        (
                            retry_time - datetime.now(retry_time.tzinfo or UTC)
                        ).total_seconds(),
                    )
                except (TypeError, ValueError, OverflowError):
                    delay = None
        delay = max(2.0, delay if delay is not None else 2.0**attempt)
        time.sleep(delay)


def _history_range(now: datetime, years: int) -> tuple[datetime, datetime]:
    """Return the latest complete quarter-hour to three calendar years earlier."""
    if now.tzinfo is None:
        raise ValueError("Ingestion time must be timezone-aware.")
    end_time = now.astimezone(UTC).replace(
        minute=(now.astimezone(UTC).minute // 15) * 15,
        second=0,
        microsecond=0,
    )
    try:
        start_time = end_time.replace(year=end_time.year - years)
    except ValueError:
        start_time = end_time.replace(year=end_time.year - years, day=28)
    return start_time, end_time


def ingest_fingrid(
    connection: duckdb.DuckDBPyConnection,
    client: FingridClient,
    start_time: datetime,
    end_time: datetime,
    *,
    dataset_id: int = FINGRID_DATASET_ID,
    chunk_days: int = INGESTION_CHUNK_DAYS,
) -> IngestionResult:
    """Incrementally ingest dataset observations and successful request ranges."""
    ensure_raw_schema(connection)
    table_name = f"fingrid_{dataset_id}"
    connection.execute(
        f"""
        CREATE TABLE IF NOT EXISTS raw.{table_name} (
            dataset_id INTEGER NOT NULL,
            start_time TIMESTAMPTZ NOT NULL,
            end_time TIMESTAMPTZ NOT NULL,
            value DOUBLE,
            raw_record JSON NOT NULL,
            PRIMARY KEY (dataset_id, start_time, end_time)
        )
        """
    )
    coverage = get_coverage(connection, "fingrid", str(dataset_id))
    missing_ranges = find_missing_ranges(start_time, end_time, coverage)
    rows_written = 0
    pages_downloaded = 0

    for missing_start, missing_end in missing_ranges:
        cursor = missing_start
        while cursor < missing_end:
            chunk_end = min(cursor + timedelta(days=chunk_days), missing_end)
            result = client.fetch_range(cursor, chunk_end, dataset_id=dataset_id)
            connection.execute("BEGIN TRANSACTION")
            try:
                rows = [
                    (
                        int(record.get("datasetId", dataset_id)),
                        parse_utc(str(record["startTime"])).isoformat(),
                        parse_utc(str(record["endTime"])).isoformat(),
                        _optional_float(record.get("value")),
                        json.dumps(record),
                    )
                    for record in result.records
                ]
                if rows:
                    insert_rows_from_frame(
                        connection,
                        "incoming_fingrid",
                        [
                            "dataset_id",
                            "start_time",
                            "end_time",
                            "value",
                            "raw_record",
                        ],
                        rows,
                        f"""
                        INSERT INTO raw.{table_name}
                            (dataset_id, start_time, end_time, value, raw_record)
                        SELECT
                            dataset_id,
                            CAST(start_time AS TIMESTAMPTZ),
                            CAST(end_time AS TIMESTAMPTZ),
                            value,
                            CAST(raw_record AS JSON)
                        FROM incoming_fingrid
                        ON CONFLICT (dataset_id, start_time, end_time) DO UPDATE SET
                            value = excluded.value,
                            raw_record = excluded.raw_record
                        """,
                    )
                add_coverage(connection, "fingrid", str(dataset_id), cursor, chunk_end)
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
            rows_written += len(result.records)
            pages_downloaded += result.pages_downloaded
            cursor = chunk_end
    return IngestionResult(rows_written, pages_downloaded)


def _optional_float(value: Any) -> float | None:
    """Convert a JSON numeric value while preserving missing observations."""
    if value is None:
        return None
    return float(value)
