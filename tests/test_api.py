"""API contract tests use only local DuckDB and monkeypatched functions."""

from fastapi.testclient import TestClient

import gridcast.api.main as api


def test_health_uses_configured_database(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "database_path", lambda: tmp_path / "health.duckdb")
    with TestClient(api.app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_forecast_returns_not_found_when_date_is_not_stored(monkeypatch):
    monkeypatch.setattr(api, "forecast_for_date", lambda _day: [])
    with TestClient(api.app) as client:
        response = client.get("/forecast", params={"date": "2025-01-01"})
    assert response.status_code == 404


def test_forecast_and_metrics_contract(monkeypatch):
    monkeypatch.setattr(
        api,
        "forecast_for_date",
        lambda _day: [
            {
                "timestamp_utc": "2026-11-21T00:00:00+00:00",
                "model_mwh": 10_000.0,
                "seasonal_naive_mwh": 9_900.0,
                "fingrid_mwh": 10_100.0,
            }
        ],
    )
    monkeypatch.setattr(
        api, "recent_metrics", lambda: {"mae_mwh": 12.0, "mape_percent": 0.12}
    )
    with TestClient(api.app) as client:
        forecast = client.get("/forecast", params={"date": "2026-11-21"})
        metrics = client.get("/metrics")
    assert forecast.status_code == 200
    assert forecast.json()[0]["fingrid_mwh"] == 10_100.0
    assert metrics.json() == {"mae_mwh": 12.0, "mape_percent": 0.12}
