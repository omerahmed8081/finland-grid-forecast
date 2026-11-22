"""FMI forecast snapshots use saved XML fixtures and never call the service."""

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import duckdb

from gridcast.ingest.fmi import (
    FmiClient,
    FmiFetchResult,
    snapshot_fmi_next_day_forecast,
)

FIXTURE = Path(__file__).parent / "fixtures" / "fmi_timevaluepair.xml"
UTC = timezone.utc  # noqa: UP017


class FakeResponse:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self) -> bytes:
        return self.body


def test_forecast_query_uses_official_harmonie_stored_query(tmp_path: Path) -> None:
    client = FmiClient(tmp_path / "cache")
    with patch(
        "gridcast.ingest.fmi.urlopen", return_value=FakeResponse(FIXTURE.read_bytes())
    ) as mocked:
        response = client.fetch_forecast_range(
            ("helsinki", "tampere"),
            datetime(2024, 1, 1, tzinfo=UTC),
            datetime(2024, 1, 2, tzinfo=UTC),
        )
    query = parse_qs(urlparse(mocked.call_args.args[0].full_url).query)
    assert query["storedquery_id"] == [
        "fmi::forecast::harmonie::surface::point::timevaluepair"
    ]
    assert response.downloaded
    assert len(response.observations) == 4
    assert response.observations[0]["parameter"] == "temperature"


def test_snapshot_persists_origin_and_forecast_valid_times(tmp_path: Path) -> None:
    class StubClient:
        def fetch_forecast_range(self, places, start, end):
            assert places == ("helsinki",)
            assert start < end
            return FmiFetchResult(
                [
                    {
                        "place": "helsinki",
                        "observed_at": datetime(2024, 1, 2, tzinfo=UTC),
                        "temperature_c": -4.0,
                        "parameter": "temperature",
                    }
                ],
                True,
            )

    connection = duckdb.connect(str(tmp_path / "forecast.duckdb"))
    try:
        result = snapshot_fmi_next_day_forecast(
            connection,
            StubClient(),
            now=datetime(2024, 1, 1, 13, tzinfo=UTC),
            places=("helsinki",),
        )
        rows = connection.execute(
            "select forecast_origin_utc, forecast_for_utc, temperature_c "
            "from raw.fmi_weather_forecasts"
        ).fetchall()
    finally:
        connection.close()
    assert result.rows_written == 1
    assert rows[0][2] == -4.0
