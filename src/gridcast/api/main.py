"""FastAPI endpoints for the shared DuckDB forecast store."""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import duckdb
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

from gridcast.api.service import (
    create_next_day_forecast,
    database_path,
    forecast_for_date,
    recent_metrics,
)

app = FastAPI(title="Finland Grid Forecast", version="0.1.0")


class HealthResponse(BaseModel):
    status: str


class ForecastRow(BaseModel):
    timestamp_utc: str
    model_mwh: float | None
    seasonal_naive_mwh: float | None
    fingrid_mwh: float | None


class MetricsResponse(BaseModel):
    mae_mwh: float | None
    mape_percent: float | None


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    """Report warehouse connectivity."""
    with duckdb.connect(str(database_path())) as connection:
        connection.execute("select 1")
    return HealthResponse(status="ok")


@app.get("/forecast", response_model=list[ForecastRow])
def forecast(day: date = Query(alias="date")) -> list[ForecastRow]:
    """Return one Helsinki calendar day's UTC-stamped forecast rows."""
    rows = forecast_for_date(day)
    tomorrow = datetime.now(ZoneInfo("Europe/Helsinki")).date() + timedelta(days=1)
    if not rows and day == tomorrow:
        try:
            create_next_day_forecast()
        except RuntimeError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        rows = forecast_for_date(day)
    if not rows:
        raise HTTPException(status_code=404, detail="No forecast stored for this date")
    return [ForecastRow(**row) for row in rows]


@app.get("/metrics", response_model=MetricsResponse)
def metrics() -> MetricsResponse:
    """Return rolling 30-day metrics where forecast and actual overlap."""
    return MetricsResponse(**recent_metrics())
