"""Streamlit dashboard using the same DuckDB tables as the API."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import duckdb
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from gridcast.api.service import (
    create_next_day_forecast,
    database_path,
    ensure_prediction_table,
)

HELSINKI = ZoneInfo("Europe/Helsinki")
UTC = ZoneInfo("UTC")
SERIES_COLORS = {
    "Actual consumption": "#56B4E9",
    "Our model": "#E69F00",
    "Fingrid forecast": "#009E73",
}


def _utc_day_bounds(day: date) -> tuple[datetime, datetime]:
    """Return one Helsinki calendar day as a UTC half-open interval."""
    start = datetime.combine(day, time.min, HELSINKI).astimezone(UTC)
    end = datetime.combine(day + timedelta(days=1), time.min, HELSINKI).astimezone(UTC)
    return start, end


def _read_dashboard_data(
    db_path: Path, tomorrow: date
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Load latest forecasts and the aligned 30-day history from DuckDB."""
    start, end = _utc_day_bounds(tomorrow)
    with duckdb.connect(str(db_path), read_only=True) as connection:
        forecast = connection.execute(
            """select target_hour_utc, model_mwh, fingrid_mwh
            from forecast_predictions
            where target_hour_utc >= ? and target_hour_utc < ?
            qualify row_number() over (
                partition by target_hour_utc order by forecast_origin_utc desc
            ) = 1
            order by target_hour_utc""",
            [start, end],
        ).fetchdf()
        history = connection.execute(
            """with latest_predictions as (
                select target_hour_utc, model_mwh, fingrid_mwh
                from forecast_predictions
                where target_hour_utc >= now() - interval '30 days'
                qualify row_number() over (
                    partition by target_hour_utc order by forecast_origin_utc desc
                ) = 1
            )
            select a.hour_utc, a.consumption_mwh,
                p.model_mwh, p.fingrid_mwh
            from marts.features a left join latest_predictions p
                on a.hour_utc = p.target_hour_utc
            where a.hour_utc >= now() - interval '30 days'
            order by a.hour_utc"""
        ).fetchdf()
    return forecast, history


def _line_chart(
    frame: pd.DataFrame,
    x_column: str,
    series: list[tuple[str, str]],
    x_label: str,
) -> go.Figure:
    """Build a line chart with readable series names and fixed colors."""
    selected = frame[[x_column, *[column for column, _ in series]]].rename(
        columns={column: label for column, label in series}
    )
    chart_data = selected.melt(
        id_vars=x_column,
        var_name="series",
        value_name="consumption_mwh",
    )
    figure = px.line(
        chart_data,
        x=x_column,
        y="consumption_mwh",
        color="series",
        color_discrete_map=SERIES_COLORS,
        labels={
            x_column: x_label,
            "consumption_mwh": "Consumption (MWh/h)",
            "series": "",
        },
    )
    figure.update_layout(template="plotly_dark", legend_title_text="")
    return figure


st.set_page_config(page_title="Finland Grid Forecast", layout="wide")
st.markdown(
    """<style>
    [data-testid="stToolbar"], [data-testid="stAppDeployButton"],
    [data-testid="stDecoration"], #MainMenu, footer {
        display: none !important;
    }
    </style>""",
    unsafe_allow_html=True,
)
st.title("Finland electricity consumption forecast")
db_path = database_path()
tomorrow = datetime.now(HELSINKI).date() + timedelta(days=1)

st.subheader(f"Tomorrow’s forecast · {tomorrow:%A, %d %B}")
if st.button("Generate tomorrow’s forecast"):
    try:
        create_next_day_forecast(db_path)
        st.success("Forecast saved to the shared DuckDB warehouse.")
    except Exception as error:
        st.error(f"Unable to generate tomorrow’s forecast: {error}")

try:
    with duckdb.connect(str(db_path)) as connection:
        ensure_prediction_table(connection)
    forecast, history = _read_dashboard_data(db_path, tomorrow)

    if forecast.empty:
        st.info(
            "No forecast is saved for tomorrow yet. "
            "Generate one after 13:00 Helsinki time."
        )
    else:
        st.plotly_chart(
            _line_chart(
                forecast,
                "target_hour_utc",
                [
                    ("model_mwh", "Our model"),
                    ("fingrid_mwh", "Fingrid forecast"),
                ],
                "Hour (UTC)",
            ),
            use_container_width=True,
        )

    st.subheader("Last 30 days")
    st.plotly_chart(
        _line_chart(
            history,
            "hour_utc",
            [
                ("consumption_mwh", "Actual consumption"),
                ("model_mwh", "Our model"),
                ("fingrid_mwh", "Fingrid forecast"),
            ],
            "Hour (UTC)",
        ),
        use_container_width=True,
    )

    aligned = history.dropna(subset=["consumption_mwh", "model_mwh", "fingrid_mwh"])
    if aligned.empty:
        st.info("Rolling comparisons appear when actuals and both forecasts overlap.")
    else:
        actual = aligned.consumption_mwh
        model_error = (actual - aligned.model_mwh).abs()
        fingrid_error = (actual - aligned.fingrid_mwh).abs()
        model_mape = (model_error / actual).mean() * 100
        fingrid_mape = (fingrid_error / actual).mean() * 100
        metrics = st.columns(4)
        metrics[0].metric("Our model · 30-day MAE", f"{model_error.mean():.0f} MWh/h")
        metrics[1].metric("Our model · 30-day MAPE", f"{model_mape:.2f}%")
        metrics[2].metric("Fingrid · 30-day MAE", f"{fingrid_error.mean():.0f} MWh/h")
        metrics[3].metric("Fingrid · 30-day MAPE", f"{fingrid_mape:.2f}%")
        st.caption(
            "Winter months have higher error. See the "
            "[backtest errors by month](https://github.com/omerahmed8081/finland-grid-forecast/blob/main/reports/backtest.md)."
        )
except Exception as error:
    st.error(f"Unable to read the forecast warehouse: {error}")

st.markdown("### About")
st.write(
    "Forecasting Finland's electricity consumption with Fingrid Open Data and "
    "FMI weather observations. Fingrid data © Fingrid, CC BY 4.0; "
    "weather © FMI Open Data."
)
