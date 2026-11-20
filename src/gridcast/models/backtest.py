"""Forecast-origin-aligned LightGBM backtest and MLflow logging."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import duckdb
import matplotlib.pyplot as plt
import mlflow
import pandas as pd
from lightgbm import LGBMRegressor
from sklearn.metrics import mean_absolute_error, mean_squared_error

from gridcast.config import REPO_ROOT

HELSINKI = ZoneInfo("Europe/Helsinki")
FEATURES = [
    "seasonal_week",
    "origin_mean_24h",
    "hour",
    "weekday",
    "month",
    "holiday",
    "temperature",
]


def load_data(db_path: Path) -> pd.DataFrame:
    """Read target and target-time calendar/weather fields."""
    with duckdb.connect(str(db_path), read_only=True) as con:
        frame = con.execute("""select hour_utc, consumption_mwh as target,
            hour_of_day as hour, weekday, month, is_finnish_holiday as holiday,
            population_weighted_temperature_c temperature
            from marts.features order by hour_utc""").df()
    frame["hour_utc"] = pd.to_datetime(frame.hour_utc, utc=True)
    return frame.set_index("hour_utc", drop=False)


def make_origin_rows(data: pd.DataFrame, origin: datetime) -> pd.DataFrame:
    """Build 24 forecast rows using only target data known at the issue time."""
    origin_ts = pd.Timestamp(origin).tz_convert("UTC")
    known = data.loc[data.index <= origin_ts]
    recent = known.loc[known.index > origin_ts - pd.Timedelta(hours=24), "target"]
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
        weather_time = target_utc - pd.Timedelta(hours=24)
        # marts.features stores temperature shifted forward 24 hours. Its row
        # at t therefore represents weather observed at t-24, when available.
        temperature = (
            calendar.temperature
            if weather_time <= origin_ts
            else (known_weather.iloc[-1] if not known_weather.empty else float("nan"))
        )
        rows.append(
            {
                "hour_utc": target_utc,
                "target": calendar.target,
                "seasonal_week": data.loc[previous_week].target,
                "origin_mean_24h": recent.mean(),
                "hour": calendar.hour,
                "weekday": calendar.weekday,
                "month": calendar.month,
                "holiday": int(calendar.holiday),
                "temperature": temperature,
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


def run_backtest(db_path: Path = REPO_ROOT / "data" / "gridcast.duckdb") -> Path:
    """Train before a 12-month holdout and score 10:00 next-day forecasts."""
    data = load_data(db_path)
    latest = data.index.max().date()
    test_first = latest - timedelta(days=365)
    training = build_examples(
        data, test_first - timedelta(days=730), test_first - timedelta(days=1)
    )
    training = training.dropna(subset=FEATURES + ["target"])
    model = LGBMRegressor(
        n_estimators=250, learning_rate=0.04, num_leaves=31, verbosity=-1
    )
    model.fit(training[FEATURES], training.target)
    scored = build_examples(data, test_first, latest - timedelta(days=1))
    scored = scored.dropna(subset=FEATURES + ["target"]).copy()
    scored["model"] = model.predict(scored[FEATURES])
    scored["seasonal_naive"] = scored.seasonal_week
    scored["fingrid_forecast"] = float("nan")
    # Dataset 165 is populated below when its versioned raw table has been ingested.
    with duckdb.connect(str(db_path), read_only=True) as con:
        if con.execute(
            "select count(*) from information_schema.tables "
            "where table_name = 'fingrid_165'"
        ).fetchone()[0]:
            raw = con.execute("select start_time, value from raw.fingrid_165").df()
            raw["hour_utc"] = pd.to_datetime(raw.start_time, utc=True).dt.floor("h")
            hourly = raw.groupby("hour_utc").value.mean()
            scored["fingrid_forecast"] = scored.hour_utc.map(hourly)
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
        for name in ["model", "seasonal_naive", "fingrid_forecast"]:
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
    lines.extend(
        f"| {name} | {mae:.1f} | {rmse:.1f} | {mape:.2f}% |"
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
        "Each origin is 13:00 Europe/Helsinki and predicts the next local day. "
        "Features use the prior week's same-hour load and the mean of the 24 "
        "hours observed by issue time; no target-relative future lag or actual "
        "future weather is used.",
        "",
        "| Hour | MAE |",
        "|---:|---:|",
    ]
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
