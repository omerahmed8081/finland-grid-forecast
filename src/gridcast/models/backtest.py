"""Forecast-origin-aligned LightGBM backtest and MLflow logging."""
# ruff: noqa: E501

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import duckdb
import joblib
import matplotlib.pyplot as plt
import mlflow
import pandas as pd
from lightgbm import LGBMRegressor
from sklearn.linear_model import Ridge
from sklearn.metrics import mean_absolute_error, mean_squared_error
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from gridcast.config import REPO_ROOT

HELSINKI = ZoneInfo("Europe/Helsinki")
UTC = timezone.utc  # noqa: UP017
FEATURES = [
    "seasonal_week",
    "consumption_lag_48h",
    "origin_mean_24h",
    "origin_mean_168h",
    "hour",
    "weekday",
    "month",
    "holiday",
    "temperature",
    "heating_degree_hours",
]
ORACLE_FEATURES = [
    *FEATURES[:-2],
    "oracle_temperature",
    "oracle_heating_degree_hours",
]


def load_data(db_path: Path) -> pd.DataFrame:
    """Read target and target-time calendar/weather fields."""
    with duckdb.connect(str(db_path), read_only=True) as con:
        frame = con.execute("""select hour_utc, consumption_mwh as target,
            hour_of_day as hour, weekday, month, is_finnish_holiday as holiday,
            consumption_lag_48h, consumption_mean_168h,
            population_weighted_temperature_c temperature,
            heating_degree_hours
            from marts.features order by hour_utc""").df()
    frame["hour_utc"] = pd.to_datetime(frame.hour_utc, utc=True)
    frame = frame.set_index("hour_utc", drop=False)
    with duckdb.connect(str(db_path), read_only=True) as con:
        exists = con.execute("""select count(*) from information_schema.tables
            where table_schema='raw' and table_name='fingrid_165'""").fetchone()[0]
        if exists:
            forecast = con.execute("""select start_time, avg(value) as value
                from raw.fingrid_165 group by start_time""").df()
            forecast["hour_utc"] = pd.to_datetime(
                forecast.start_time, utc=True
            ).dt.floor("h")
            frame.attrs["fingrid"] = forecast.groupby("hour_utc").value.mean()
    return frame


def make_origin_rows(data: pd.DataFrame, origin: datetime) -> pd.DataFrame:
    """Build 24 forecast rows using only target data known at the issue time."""
    origin_ts = pd.Timestamp(origin).tz_convert("UTC")
    # Consumption stamped at the issue hour covers the hour that is just
    # beginning, so only rows strictly before the issue are observable.
    known = data.loc[data.index < origin_ts]
    recent = known.loc[known.index >= origin_ts - pd.Timedelta(hours=24), "target"]
    origin_mean_168 = known.iloc[-1].consumption_mean_168h
    known_weather = known.loc[known.temperature.notna(), "temperature"]
    day = origin.astimezone(HELSINKI).date() + timedelta(days=1)
    rows: list[dict[str, object]] = []
    local_start = pd.Timestamp(datetime.combine(day, time.min, tzinfo=HELSINKI))
    local_end = pd.Timestamp(
        datetime.combine(day + timedelta(days=1), time.min, tzinfo=HELSINKI)
    )
    target_hours = pd.date_range(
        local_start, local_end, freq="h", inclusive="left"
    ).tz_convert("UTC")
    for target_utc in target_hours:
        target_local = target_utc.tz_convert("Europe/Helsinki").to_pydatetime()
        previous_week = pd.Timestamp(target_local - timedelta(days=7)).tz_convert("UTC")
        if target_utc not in data.index or previous_week not in data.index:
            continue
        calendar = data.loc[target_utc]
        lag_48_time = target_utc - pd.Timedelta(hours=48)
        if lag_48_time > origin_ts:
            continue
        weather_time = target_utc - pd.Timedelta(hours=24)
        # marts.features stores temperature shifted forward 24 hours. Its row
        # at t therefore represents weather observed at t-24, when available.
        temperature = (
            calendar.temperature
            if weather_time <= origin_ts
            else (known_weather.iloc[-1] if not known_weather.empty else float("nan"))
        )
        oracle_row = (
            data.loc[target_utc + pd.Timedelta(hours=24)]
            if target_utc + pd.Timedelta(hours=24) in data.index
            else None
        )
        oracle_temperature = (
            float(oracle_row.temperature) if oracle_row is not None else float("nan")
        )
        forecast_series = data.attrs.get("fingrid")
        fingrid_value = (
            float(forecast_series.get(target_utc, float("nan")))
            if forecast_series is not None
            else float("nan")
        )
        rows.append(
            {
                "hour_utc": target_utc,
                "target": calendar.target,
                "seasonal_week": data.loc[previous_week].target,
                "consumption_lag_48h": data.loc[lag_48_time].target,
                "origin_mean_24h": recent.mean(),
                "origin_mean_168h": origin_mean_168,
                "hour": calendar.hour,
                "weekday": calendar.weekday,
                "month": calendar.month,
                "holiday": int(calendar.holiday),
                "temperature": temperature,
                "heating_degree_hours": max(0.0, 18.0 - temperature),
                "oracle_temperature": oracle_temperature,
                "oracle_heating_degree_hours": max(0.0, 18.0 - oracle_temperature),
                "fingrid_forecast": fingrid_value,
            }
        )
    return pd.DataFrame(rows)


def build_examples(data: pd.DataFrame, first: date, last: date) -> pd.DataFrame:
    """Create historic day-ahead examples with features known at 13:00."""
    examples = []
    day = first
    while day <= last:
        origin = datetime.combine(day, time(13), tzinfo=HELSINKI)
        rows = make_origin_rows(data, origin)
        if not rows.empty:
            examples.append(rows)
        day += timedelta(days=1)
    return pd.concat(examples, ignore_index=True)


def choose_non_worse_correction_scale(
    actual: pd.Series,
    fingrid_forecast: pd.Series,
    residual_estimate: pd.Series,
) -> tuple[float, tuple[float, float, float], tuple[float, float, float]]:
    """Select residual scale with validation MAE, RMSE, and MAPE no worse than raw."""
    raw_metrics = _forecast_metrics(actual, fingrid_forecast)
    best_scale = 0.0
    best_metrics = raw_metrics
    for step in range(1, 21):
        scale = step / 20
        corrected_metrics = _forecast_metrics(
            actual, fingrid_forecast + scale * residual_estimate
        )
        no_worse = all(
            candidate <= raw + 1e-9
            for raw, candidate in zip(raw_metrics, corrected_metrics)
        )
        if no_worse and corrected_metrics[0] < best_metrics[0]:
            best_scale = scale
            best_metrics = corrected_metrics
    return best_scale, raw_metrics, best_metrics


def _forecast_metrics(
    actual: pd.Series, predicted: pd.Series
) -> tuple[float, float, float]:
    """Calculate MAE, RMSE, and MAPE for an aligned forecast series."""
    return (
        mean_absolute_error(actual, predicted),
        mean_squared_error(actual, predicted) ** 0.5,
        (abs((actual - predicted) / actual).mean()) * 100,
    )


def run_backtest(db_path: Path = REPO_ROOT / "data" / "gridcast.duckdb") -> Path:
    """Train before a 12-month holdout and score 13:00 next-day forecasts."""
    data = load_data(db_path)
    latest = data.index.max().tz_convert(HELSINKI).date()
    test_first = latest - timedelta(days=364)
    training_target_first = test_first - timedelta(days=730)
    training = build_examples(
        data,
        training_target_first - timedelta(days=1),
        test_first - timedelta(days=2),
    )
    training = training.dropna(subset=FEATURES + ["target"])
    model = LGBMRegressor(
        n_estimators=250, learning_rate=0.04, num_leaves=31, verbosity=-1
    )
    model.fit(training[FEATURES], training.target)
    (REPO_ROOT / "data").mkdir(exist_ok=True)
    joblib.dump(model, REPO_ROOT / "data" / "day_ahead_model.joblib")
    scored = build_examples(
        data, test_first - timedelta(days=1), latest - timedelta(days=1)
    )
    scored = scored.dropna(subset=FEATURES + ["target"]).copy()
    scored["model"] = model.predict(scored[FEATURES])
    oracle_training = training.dropna(subset=ORACLE_FEATURES + ["target"])
    oracle = LGBMRegressor(
        n_estimators=250, learning_rate=0.04, num_leaves=31, verbosity=-1
    )
    oracle.fit(oracle_training[ORACLE_FEATURES], oracle_training.target)
    scored["oracle_upper_bound"] = float("nan")
    oracle_valid = scored[ORACLE_FEATURES].notna().all(axis=1)
    scored.loc[oracle_valid, "oracle_upper_bound"] = oracle.predict(
        scored.loc[oracle_valid, ORACLE_FEATURES]
    )
    scored["seasonal_naive"] = scored.seasonal_week
    correction_features = [*FEATURES, "fingrid_forecast"]
    correction_train = training.dropna(subset=correction_features + ["target"]).copy()
    scored["fingrid_corrected"] = float("nan")
    validation_start = test_first - timedelta(days=180)
    validation_start_utc = pd.Timestamp(
        datetime.combine(validation_start, time.min, tzinfo=HELSINKI)
    ).tz_convert("UTC")
    development = correction_train.loc[
        correction_train.hour_utc < validation_start_utc
    ].copy()
    validation = correction_train.loc[
        correction_train.hour_utc >= validation_start_utc
    ].copy()
    validation_results: dict[str, float] = {}
    correction_scale = 0.0
    if len(development) >= 72 and not validation.empty:
        validation_model = make_pipeline(StandardScaler(), Ridge(alpha=50.0))
        development["fingrid_error"] = development.target - development.fingrid_forecast
        validation_model.fit(
            development[correction_features], development.fingrid_error
        )
        validation_residuals = pd.Series(
            validation_model.predict(validation[correction_features]),
            index=validation.index,
        )
        (
            correction_scale,
            validation_raw_metrics,
            validation_corrected_metrics,
        ) = choose_non_worse_correction_scale(
            validation.target,
            validation.fingrid_forecast,
            validation_residuals,
        )
        validation_results = {
            "raw_mae": validation_raw_metrics[0],
            "raw_rmse": validation_raw_metrics[1],
            "raw_mape": validation_raw_metrics[2],
            "corrected_mae": validation_corrected_metrics[0],
            "corrected_rmse": validation_corrected_metrics[1],
            "corrected_mape": validation_corrected_metrics[2],
            "rows": float(len(validation)),
        }
        correction = make_pipeline(StandardScaler(), Ridge(alpha=50.0))
        correction_train["fingrid_error"] = (
            correction_train.target - correction_train.fingrid_forecast
        )
        correction.fit(
            correction_train[correction_features], correction_train.fingrid_error
        )
        available = scored.dropna(subset=correction_features)
        scored.loc[available.index, "fingrid_corrected"] = (
            available.fingrid_forecast
            + correction_scale * correction.predict(available[correction_features])
        )
    with duckdb.connect(str(db_path)) as con:
        con.execute("""create table if not exists forecast_predictions (
            forecast_origin_utc timestamptz not null, target_hour_utc timestamptz not null,
            model_mwh double, seasonal_naive_mwh double, fingrid_mwh double,
            primary key (forecast_origin_utc, target_hour_utc))""")
        for row in scored.itertuples(index=False):
            local_target = row.hour_utc.to_pydatetime().astimezone(HELSINKI)
            origin = datetime.combine(
                local_target.date() - timedelta(days=1), time(13), tzinfo=HELSINKI
            )
            con.execute(
                """insert into forecast_predictions values (?, ?, ?, ?, ?)
                on conflict do update set model_mwh=excluded.model_mwh,
                seasonal_naive_mwh=excluded.seasonal_naive_mwh, fingrid_mwh=excluded.fingrid_mwh""",
                [
                    origin.astimezone(UTC),
                    row.hour_utc.to_pydatetime(),
                    row.model,
                    row.seasonal_naive,
                    None if pd.isna(row.fingrid_forecast) else row.fingrid_forecast,
                ],
            )
    report_dir = REPO_ROOT / "reports"
    report_dir.mkdir(exist_ok=True)
    mlflow.set_tracking_uri(
        f"sqlite:///{(REPO_ROOT / 'data' / 'mlflow.db').as_posix()}"
    )
    metrics: dict[str, tuple[float, float, float]] = {}
    with mlflow.start_run(run_name="day-ahead-12-month-backtest"):
        mlflow.log_params(
            {
                "training_examples": len(training),
                "holdout_days": 365,
                "issue_time": "13:00 Europe/Helsinki",
                "fingrid_error_training_rows": len(correction_train),
                "fingrid_error_development_rows": len(development),
                "fingrid_error_validation_rows": len(validation),
                "fingrid_error_correction_scale": correction_scale,
            }
        )
        mlflow.lightgbm.log_model(model, "model")
        importance = pd.DataFrame(
            {"feature": FEATURES, "importance": model.feature_importances_}
        ).sort_values("importance")
        ax = importance.plot.barh(
            x="feature",
            y="importance",
            legend=False,
            title="LightGBM feature importance",
        )
        ax.get_figure().savefig(
            report_dir / "feature_importance.png", bbox_inches="tight"
        )
        mlflow.log_artifact(str(report_dir / "feature_importance.png"))
        plt.close(ax.get_figure())
        mlflow.lightgbm.log_model(oracle, "oracle_upper_bound_model")
        if validation_results:
            mlflow.sklearn.log_model(correction, "fingrid_error_correction_model")
            mlflow.log_metrics(
                {
                    "fingrid_validation_raw_mae": validation_results["raw_mae"],
                    "fingrid_validation_raw_rmse": validation_results["raw_rmse"],
                    "fingrid_validation_raw_mape": validation_results["raw_mape"],
                    "fingrid_validation_corrected_mae": validation_results[
                        "corrected_mae"
                    ],
                    "fingrid_validation_corrected_rmse": validation_results[
                        "corrected_rmse"
                    ],
                    "fingrid_validation_corrected_mape": validation_results[
                        "corrected_mape"
                    ],
                }
            )
        for name in [
            "model",
            "oracle_upper_bound",
            "seasonal_naive",
            "fingrid_forecast",
            "fingrid_corrected",
        ]:
            valid = scored.dropna(subset=[name])
            if valid.empty:
                continue
            values = (
                mean_absolute_error(valid.target, valid[name]),
                mean_squared_error(valid.target, valid[name]) ** 0.5,
                (abs((valid.target - valid[name]) / valid.target).mean()) * 100,
            )
            metrics[name] = values
            mlflow.log_metrics(
                {
                    f"{name}_{metric}": value
                    for metric, value in zip(["mae", "rmse", "mape"], values)
                }
            )
    for key, groupby, title in [
        ("hour", "hour", "Error by hour of day"),
        ("month", "month", "Error by month"),
    ]:
        scored["absolute_error"] = abs(scored.target - scored.model)
        chart = (
            scored.groupby(groupby).absolute_error.mean().plot(title=title, marker="o")
        )
        chart.get_figure().savefig(
            report_dir / f"backtest_by_{key}.png", bbox_inches="tight"
        )
        plt.close(chart.get_figure())
    lines = [
        "# Day-ahead backtest",
        "",
        f"12-month holdout beginning {test_first}; {len(scored)} target hours.",
        "",
        "| Model | MAE (MWh/h) | RMSE | MAPE |",
        "|---|---:|---:|---:|",
    ]
    display_names = {
        "model": "leakage-safe LightGBM",
        "oracle_upper_bound": "actual-weather oracle (upper-bound scenario)",
        "seasonal_naive": "seasonal naive",
        "fingrid_forecast": "Fingrid published forecast",
        "fingrid_corrected": "Fingrid residual correction",
    }
    lines.extend(
        f"| {display_names[name]} | {mae:.1f} | {rmse:.1f} | {mape:.2f}% |"
        for name, (mae, rmse, mape) in metrics.items()
    )
    if "fingrid_forecast" not in metrics:
        lines.extend(
            [
                "",
                "Fingrid comparison is pending historical dataset 165 ingestion; "
                "it is not reported as a measured result.",
            ]
        )
    lines += [
        "",
        "Each origin is 13:00 Europe/Helsinki, after Fingrid's daily 12:00 "
        "publication, and predicts the next local day. The primary model uses "
        "origin-safe load/calendar features and weather known at issue time. "
        "The oracle row uses observed target-day weather as a hypothetical "
        "upper-bound scenario; it is not a deployable forecast or a guarantee "
        "that forecast weather alone could achieve the same score.",
        f"Fingrid error correction is a separate Ridge residual model retrained on {len(correction_train)} pre-holdout forecast hours.",
        (
            f"Residual scale was selected on the final pre-holdout validation window "
            f"({validation_start} through {test_first - timedelta(days=1)}; "
            f"{int(validation_results['rows'])} aligned hours; trained on "
            f"{len(development)} earlier hours). Validation metrics: "
            f"raw Fingrid: {validation_results['raw_mae']:.1f} MAE / "
            f"{validation_results['raw_rmse']:.1f} RMSE / "
            f"{validation_results['raw_mape']:.2f}% MAPE; corrected: "
            f"{validation_results['corrected_mae']:.1f} / "
            f"{validation_results['corrected_rmse']:.1f} / "
            f"{validation_results['corrected_mape']:.2f}% (scale "
            f"{correction_scale:.2f}). The scale is selected only from candidates "
            "no worse on all three metrics, with zero correction as a safe option."
            if validation_results
            else "Residual correction validation was unavailable because there were "
            "not enough aligned pre-holdout rows."
        ),
        "The primary model uses consumption_lag_48h, the 168-hour mart mean "
        "available at issue time, and heating_degree_hours alongside its existing "
        "features. The 168-hour value is taken from the latest completed mart row "
        "at the origin, so target-day rolling values cannot leak into the forecast.",
        "Fingrid's raw forecast remains stronger than the leakage-safe primary "
        "LightGBM on the holdout. The residual correction is a separate model "
        "that uses historical Fingrid errors; this result does not establish "
        "that it will outperform Fingrid on future periods.",
        "",
        "The oracle-versus-primary MAE difference measures this model's response "
        "to actual target-day weather; actual weather is not available at issue time. FMI's "
        "open point forecast service does not retain a historical forecast archive "
        "([FMI forecast service documentation](https://github.com/fmidev/smartmet-sofp-backend)), "
        "so archived snapshots are saved daily for live predictions instead. "
        "Fingrid dataset 165 is published at 12:00 the previous day "
        "([catalogue](https://data.fingrid.fi/en/datasets/165)).",
        "",
        "| Hour | MAE |",
        "|---:|---:|",
    ]
    if "model" in metrics and "oracle_upper_bound" in metrics:
        main_mae = metrics["model"][0]
        oracle_mae = metrics["oracle_upper_bound"][0]
        change = (main_mae - oracle_mae) / main_mae * 100
        lines.insert(
            lines.index("| Hour | MAE |"),
            f"The actual-weather oracle scenario reduced MAE by {change:.1f}% "
            f"({main_mae:.1f} to {oracle_mae:.1f} MWh/h) in this holdout.",
        )
    lines.extend(
        f"| {hour} | {error:.1f} |"
        for hour, error in scored.assign(error=abs(scored.target - scored.model))
        .groupby("hour")
        .error.mean()
        .items()
    )
    lines += ["", "| Month | MAE |", "|---:|---:|"]
    lines.extend(
        f"| {month} | {error:.1f} |"
        for month, error in scored.assign(error=abs(scored.target - scored.model))
        .groupby("month")
        .error.mean()
        .items()
    )
    lines += [
        "",
        "Plots: `backtest_by_hour.png`, `backtest_by_month.png`, "
        "`feature_importance.png`.",
    ]
    path = report_dir / "backtest.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


if __name__ == "__main__":
    print(run_backtest())
