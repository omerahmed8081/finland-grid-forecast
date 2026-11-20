"""DuckDB-backed prediction storage and forecast generation."""
# ruff: noqa: E501

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import duckdb
import holidays
import joblib
import pandas as pd

from gridcast.config import (
    FINGRID_CONSUMPTION_FORECAST_DATASET_ID,
    REPO_ROOT,
    load_settings,
)
from gridcast.ingest.fingrid import FingridClient, ingest_fingrid
from gridcast.models.backtest import FEATURES, HELSINKI

UTC = timezone.utc  # noqa: UP017


def database_path() -> Path:
    """Return the shared warehouse path, allowing container overrides."""
    import os

    configured = os.environ.get("GRIDCAST_DATABASE_PATH")
    return Path(configured) if configured else REPO_ROOT / "data" / "gridcast.duckdb"


def ensure_prediction_table(connection: duckdb.DuckDBPyConnection) -> None:
    """Create the shared forecast history table."""
    connection.execute("""create table if not exists forecast_predictions (
        forecast_origin_utc timestamptz not null, target_hour_utc timestamptz not null,
        model_mwh double, seasonal_naive_mwh double, fingrid_mwh double,
        primary key (forecast_origin_utc, target_hour_utc))""")


def create_next_day_forecast(db_path: Path | None = None) -> date:
    """Generate the next local-day forecast from the saved origin-safe model."""
    path = db_path or database_path()
    now = datetime.now(UTC).astimezone(HELSINKI)
    issue = now.replace(hour=13, minute=0, second=0, microsecond=0)
    if now < issue:
        raise RuntimeError(
            "Next-day forecasts are available after 13:00 Helsinki time."
        )
    day = issue.date() + timedelta(days=1)
    model = joblib.load(REPO_ROOT / "data" / "day_ahead_model.joblib")
    with duckdb.connect(str(path)) as connection:
        ensure_prediction_table(connection)
        try:
            settings = load_settings()
            client = FingridClient(settings.api_key, settings.cache_path)
            target_start = datetime.combine(day, time.min, tzinfo=HELSINKI).astimezone(
                UTC
            )
            target_end = datetime.combine(
                day + timedelta(days=1), time.min, tzinfo=HELSINKI
            ).astimezone(UTC)
            ingest_fingrid(
                connection,
                client,
                target_start,
                target_end,
                dataset_id=FINGRID_CONSUMPTION_FORECAST_DATASET_ID,
                chunk_days=1,
            )
        except RuntimeError:
            pass
        observed = connection.execute(
            """select hour_utc, consumption_mwh as target,
            hour_of_day as hour, weekday, month, is_finnish_holiday as holiday,
            population_weighted_temperature_c temperature
            from marts.features where hour_utc <= ? order by hour_utc""",
            [issue.astimezone(UTC)],
        ).df()
        observed["hour_utc"] = pd.to_datetime(observed.hour_utc, utc=True)
        observed = observed.set_index("hour_utc", drop=False)
        recent = observed.target.tail(24).mean()
        recent_weather = observed.temperature.dropna().iloc[-1]
        holiday_dates = holidays.country_holidays("FI", years=[day.year])
        rows = []
        for target_utc in pd.date_range(
            datetime.combine(day, time.min, tzinfo=HELSINKI),
            datetime.combine(day + timedelta(days=1), time.min, tzinfo=HELSINKI),
            freq="h",
            inclusive="left",
        ).tz_convert("UTC"):
            target_local = target_utc.tz_convert(HELSINKI)
            prev_week = pd.Timestamp(
                target_local.to_pydatetime() - timedelta(days=7)
            ).tz_convert("UTC")
            seasonal = float(observed.loc[prev_week].target)
            weather_at = target_utc - pd.Timedelta(hours=24)
            if weather_at <= pd.Timestamp(issue):
                temp = connection.execute(
                    """select sum(w.temperature_c*p.population)/nullif(sum(p.population),0)
                    from raw.fmi_weather w join staging.population_weights p on lower(w.place)=p.place
                    where w.observed_at=? and lower(w.parameter)='t2m'""",
                    [weather_at.to_pydatetime()],
                ).fetchone()[0]
                temp = float(temp) if temp is not None else float(recent_weather)
            else:
                temp = float(recent_weather)
            rows.append(
                {
                    "target_hour_utc": target_utc,
                    "seasonal_week": seasonal,
                    "origin_mean_24h": recent,
                    "hour": target_local.hour,
                    "weekday": target_local.isoweekday(),
                    "month": target_local.month,
                    "holiday": int(target_local.date() in holiday_dates),
                    "temperature": temp,
                }
            )
        frame = pd.DataFrame(rows)
        frame["model_mwh"] = model.predict(frame[FEATURES])
        frame["seasonal_naive_mwh"] = frame.seasonal_week
        forecasts = connection.execute(
            """select date_trunc('hour', start_time) hour_utc,
            avg(value) fingrid_mwh from raw.fingrid_165
            where start_time >= ? and start_time < ? group by 1""",
            [
                frame.target_hour_utc.min().to_pydatetime(),
                (frame.target_hour_utc.max() + pd.Timedelta(hours=1)).to_pydatetime(),
            ],
        ).df()
        forecasts["hour_utc"] = pd.to_datetime(forecasts.hour_utc, utc=True)
        frame = frame.merge(
            forecasts, left_on="target_hour_utc", right_on="hour_utc", how="left"
        )
        for row in frame.itertuples(index=False):
            connection.execute(
                """insert into forecast_predictions values (?, ?, ?, ?, ?)
                on conflict do update set model_mwh=excluded.model_mwh,
                seasonal_naive_mwh=excluded.seasonal_naive_mwh, fingrid_mwh=excluded.fingrid_mwh""",
                [
                    issue.astimezone(UTC),
                    row.target_hour_utc.to_pydatetime(),
                    row.model_mwh,
                    row.seasonal_naive_mwh,
                    row.fingrid_mwh,
                ],
            )
    return day


def forecast_for_date(
    day: date, db_path: Path | None = None
) -> list[dict[str, object]]:
    """Read forecast records by Helsinki calendar date."""
    path = db_path or database_path()
    local_start = datetime.combine(day, time.min, tzinfo=HELSINKI).astimezone(UTC)
    local_end = datetime.combine(
        day + timedelta(days=1), time.min, tzinfo=HELSINKI
    ).astimezone(UTC)
    with duckdb.connect(str(path)) as writable:
        ensure_prediction_table(writable)
    with duckdb.connect(str(path), read_only=True) as connection:
        rows = connection.execute(
            """select target_hour_utc, model_mwh,
            seasonal_naive_mwh, fingrid_mwh from forecast_predictions
            where target_hour_utc >= ? and target_hour_utc < ?
            qualify forecast_origin_utc = max(forecast_origin_utc)
                over (partition by target_hour_utc)
            order by target_hour_utc""",
            [local_start, local_end],
        ).fetchall()
    return [
        {
            "timestamp_utc": row[0].astimezone(UTC).isoformat(),
            "model_mwh": row[1],
            "seasonal_naive_mwh": row[2],
            "fingrid_mwh": row[3],
        }
        for row in rows
    ]


def recent_metrics(db_path: Path | None = None) -> dict[str, float | None]:
    """Compute 30-day MAE and MAPE from stored predictions and observed targets."""
    path = db_path or database_path()
    with duckdb.connect(str(path)) as writable:
        ensure_prediction_table(writable)
    with duckdb.connect(str(path), read_only=True) as connection:
        rows = connection.execute("""select a.consumption_mwh actual, p.model_mwh predicted
            from forecast_predictions p join marts.features a on a.hour_utc=p.target_hour_utc
            where p.target_hour_utc >= now() - interval '30 days' and p.model_mwh is not null
            qualify p.forecast_origin_utc=max(p.forecast_origin_utc)
                over (partition by p.target_hour_utc)""").fetchdf()
    if rows.empty:
        return {"mae_mwh": None, "mape_percent": None}
    error = (rows.actual - rows.predicted).abs()
    return {
        "mae_mwh": float(error.mean()),
        "mape_percent": float((error / rows.actual).mean() * 100),
    }
