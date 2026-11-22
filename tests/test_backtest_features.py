"""Origin-safe feature construction tests with synthetic hourly data."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from gridcast.models.backtest import (
    FEATURES,
    choose_non_worse_correction_scale,
    make_origin_rows,
)


def _features() -> pd.DataFrame:
    """Build a DST-spanning hourly mart-like frame without API calls."""
    index = pd.date_range("2023-10-01", "2024-04-05", freq="h", tz="UTC")
    target = pd.Series(range(len(index)), index=index, dtype=float)
    frame = pd.DataFrame(
        {
            "hour_utc": index,
            "target": target.to_numpy(),
            "consumption_lag_48h": target.shift(48).to_numpy(),
            "consumption_mean_168h": target.rolling(168).mean().to_numpy(),
            "hour": index.tz_convert("Europe/Helsinki").hour,
            "weekday": index.tz_convert("Europe/Helsinki").dayofweek + 1,
            "month": index.tz_convert("Europe/Helsinki").month,
            "holiday": 0,
            "temperature": -5.0,
            "heating_degree_hours": 23.0,
        },
        index=index,
    )
    return frame


def test_forecast_features_do_not_change_when_future_actuals_change() -> None:
    data = _features()
    origin = datetime(2024, 3, 30, 13, tzinfo=ZoneInfo("Europe/Helsinki"))
    changed = data.copy()
    origin_utc = pd.Timestamp(origin).tz_convert("UTC")
    # Include the row stamped at the origin: its interval is not complete yet.
    changed.loc[changed.index >= origin_utc, "target"] += 1_000_000

    first = make_origin_rows(data, origin)
    second = make_origin_rows(changed, origin)

    assert len(first) == 23  # Spring DST day has 23 actual UTC hours.
    assert "consumption_lag_48h" in FEATURES
    assert "origin_mean_168h" in FEATURES
    pd.testing.assert_frame_equal(first[FEATURES], second[FEATURES])


def test_validation_scale_never_worsens_raw_fingrid_mae() -> None:
    actual = pd.Series([100.0, 110.0, 120.0])
    fingrid = pd.Series([105.0, 105.0, 125.0])
    harmful_residual = pd.Series([40.0, -40.0, 40.0])

    scale, raw_metrics, corrected_metrics = choose_non_worse_correction_scale(
        actual, fingrid, harmful_residual
    )

    assert scale == 0.0
    assert corrected_metrics == raw_metrics


def test_validation_scale_uses_helpful_residuals_without_metric_regression() -> None:
    actual = pd.Series([100.0, 110.0, 120.0, 130.0])
    fingrid = pd.Series([105.0, 105.0, 125.0, 125.0])
    residual_estimate = actual - fingrid

    scale, raw_metrics, corrected_metrics = choose_non_worse_correction_scale(
        actual, fingrid, residual_estimate
    )

    assert scale > 0.0
    assert all(
        corrected <= raw for raw, corrected in zip(raw_metrics, corrected_metrics)
    )
