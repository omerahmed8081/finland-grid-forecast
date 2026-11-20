from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import duckdb

from gridcast.ingest.fmi import FmiClient, ingest_fmi_weather, parse_observations

FIXTURE = Path(__file__).parent / "fixtures" / "fmi_timevaluepair.xml"
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


def test_parse_fmi_timevaluepair_fixture() -> None:
    observations = parse_observations(FIXTURE.read_bytes(), ("helsinki", "tampere"))

    assert len(observations) == 4
    assert observations[0]["observed_at"] == datetime(2024, 1, 1, tzinfo=UTC)
    assert observations[0]["temperature_c"] == -5.2
    assert observations[1]["temperature_c"] == -4.8
    assert observations[0]["place"] == "helsinki"
    assert observations[2]["place"] == "tampere"
    assert observations[2]["temperature_c"] == -6.1


def test_fmi_ingestion_is_incremental_and_uses_mocked_http(tmp_path: Path) -> None:
    body = FIXTURE.read_bytes()
    client = FmiClient(tmp_path / "cache")
    connection = duckdb.connect(":memory:")
    start = datetime(2024, 1, 1, tzinfo=UTC)
    end = datetime(2024, 1, 2, tzinfo=UTC)

    with patch(
        "gridcast.ingest.fmi.urlopen", return_value=FakeResponse(body)
    ) as mocked_urlopen:
        first = ingest_fmi_weather(
            connection, client, start, end, places=("helsinki", "tampere")
        )
        second = ingest_fmi_weather(
            connection, client, start, end, places=("helsinki", "tampere")
        )

    assert first.rows_written == 4
    assert first.requests_downloaded == 1
    assert second.rows_written == 0
    assert second.requests_downloaded == 0
    assert mocked_urlopen.call_count == 1
    request = mocked_urlopen.call_args.args[0]
    query = parse_qs(urlparse(request.full_url).query)
    assert query["place"] == ["helsinki", "tampere"]
    connection.close()
