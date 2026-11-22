"""Origin-safe feature construction tests with synthetic hourly data."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from gridcast.models.backtest import FEATURES, make_origin_rows


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
