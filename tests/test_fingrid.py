import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request

import duckdb

from gridcast.ingest.fingrid import FingridClient, ingest_fingrid

FIXTURES = Path(__file__).parent / "fixtures"
UTC = timezone.utc  # noqa: UP017


class FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


def test_fingrid_client_pages_and_sends_api_key_header(tmp_path: Path) -> None:
    page_body = (FIXTURES / "fingrid_page.json").read_bytes()
    requests: list[tuple[str, str]] = []

    def fake_urlopen(request: Request, timeout: int) -> FakeResponse:
        del timeout
        url = request.full_url
        page = parse_qs(urlparse(url).query)["page"][0]
        requests.append((request.get_header("X-api-key"), page))
        payload = json.loads(page_body)
        payload["pagination"]["lastPage"] = 2
        payload["pagination"]["currentPage"] = int(page)
        payload["pagination"]["nextPage"] = 2 if page == "1" else None
        if page == "2":
            payload["data"] = []
        return FakeResponse(json.dumps(payload).encode("utf-8"))

    client = FingridClient(
        "test-key",
        tmp_path,
        page_size=2,
        minimum_request_interval=0,
    )
    with patch("gridcast.ingest.fingrid.urlopen", side_effect=fake_urlopen):
        result = client.fetch_range(
            datetime(2024, 1, 1, tzinfo=UTC),
            datetime(2024, 1, 2, tzinfo=UTC),
        )

    assert len(result.records) == 2
    assert result.pages_downloaded == 2
    assert requests == [("test-key", "1"), ("test-key", "2")]


def test_fingrid_retries_rate_limit_without_logging_key(tmp_path: Path) -> None:
    client = FingridClient(
        "secret-test-key",
        tmp_path,
        minimum_request_interval=0,
        max_attempts=2,
    )
    response = FakeResponse(b"[]")
    request = Request("https://data.fingrid.fi/api/test")
    rate_limit = HTTPError(
        request.full_url,
        429,
        "rate limited",
        {"Retry-After": "0"},
        None,
    )

    with (
        patch(
            "gridcast.ingest.fingrid.urlopen", side_effect=[rate_limit, response]
        ) as mocked_urlopen,
        patch("gridcast.ingest.fingrid.time.sleep") as mocked_sleep,
    ):
        body = client._request("https://data.fingrid.fi/api/test")

    assert body == b"[]"
    assert mocked_urlopen.call_count == 2
    mocked_sleep.assert_called_once_with(2.0)


def test_ingestion_records_coverage_and_second_run_skips_http(tmp_path: Path) -> None:
    fixture = json.loads((FIXTURES / "fingrid_page.json").read_text(encoding="utf-8"))
    client = FingridClient(
        "test-key",
        tmp_path / "cache",
        minimum_request_interval=0,
    )
    connection = duckdb.connect(":memory:")
    start = datetime(2024, 1, 1, tzinfo=UTC)
    end = datetime(2024, 1, 2, tzinfo=UTC)

    with patch(
        "gridcast.ingest.fingrid.urlopen",
        return_value=FakeResponse(json.dumps(fixture).encode("utf-8")),
    ) as mocked_urlopen:
        first = ingest_fingrid(connection, client, start, end)
        second = ingest_fingrid(connection, client, start, end)

    assert first.rows_written == 2
    assert first.pages_downloaded == 1
    assert second.rows_written == 0
    assert second.pages_downloaded == 0
    assert mocked_urlopen.call_count == 1
    assert connection.execute("SELECT count(*) FROM raw.fingrid_124").fetchone()[0] == 2
    connection.close()
