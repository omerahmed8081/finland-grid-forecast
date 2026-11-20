"""Streamlit dashboard using the same DuckDB tables as the API."""
# ruff: noqa: E501

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import duckdb
import plotly.express as px
import streamlit as st

from gridcast.api.service import (
    create_next_day_forecast,
    database_path,
    ensure_prediction_table,
)

st.set_page_config(page_title="Finland Grid Forecast", layout="wide")
st.title("Finland electricity consumption forecast")
db_path = database_path()
try:
    with duckdb.connect(str(db_path)) as connection:
        ensure_prediction_table(connection)
    if st.button("Generate tomorrow's forecast"):
        create_next_day_forecast(db_path)
        st.success("Forecast saved to the shared DuckDB warehouse.")
    with duckdb.connect(str(db_path), read_only=True) as connection:
        tomorrow = datetime.now(ZoneInfo("Europe/Helsinki")).date() + timedelta(days=1)
        start = datetime.combine(
            tomorrow, datetime.min.time(), ZoneInfo("Europe/Helsinki")
        ).astimezone(ZoneInfo("UTC"))
        end = datetime.combine(
            tomorrow + timedelta(days=1),
            datetime.min.time(),
            ZoneInfo("Europe/Helsinki"),
        ).astimezone(ZoneInfo("UTC"))
        forecast = connection.execute(
            """select target_hour_utc, model_mwh,
            seasonal_naive_mwh, fingrid_mwh from forecast_predictions
            where target_hour_utc >= ? and target_hour_utc < ? order by target_hour_utc""",
            [start, end],
        ).fetchdf()
        history = connection.execute("""select a.hour_utc, a.consumption_mwh,
            p.model_mwh from marts.features a left join forecast_predictions p
            on a.hour_utc=p.target_hour_utc where a.hour_utc >= now()-interval '30 days'
            order by a.hour_utc""").fetchdf()
    if not forecast.empty:
        st.subheader(f"Tomorrow: {tomorrow}")
        st.plotly_chart(
            px.line(
                forecast,
                x="target_hour_utc",
                y=["model_mwh", "fingrid_mwh"],
                labels={
                    "target_hour_utc": "UTC hour",
                    "value": "MWh/h",
                    "variable": "Forecast",
                },
            ),
            use_container_width=True,
        )
    st.subheader("Last 30 days")
    st.plotly_chart(
        px.line(
            history,
            x="hour_utc",
            y=["consumption_mwh", "model_mwh"],
            labels={"hour_utc": "UTC hour", "value": "MWh/h", "variable": "Series"},
        ),
        use_container_width=True,
    )
    scored = history.dropna(subset=["model_mwh", "consumption_mwh"])
    if not scored.empty:
        error = (scored.consumption_mwh - scored.model_mwh).abs()
        c1, c2 = st.columns(2)
        c1.metric("Rolling 30-day MAE", f"{error.mean():.0f} MWh/h")
        c2.metric(
            "Rolling 30-day MAPE",
            f"{(error / scored.consumption_mwh).mean() * 100:.2f}%",
        )
except Exception as error:
    st.error(f"Unable to read the forecast warehouse: {error}")

st.markdown("### About")
st.write(
    "Forecasting Finland's electricity consumption with Fingrid Open Data and FMI weather observations. Fingrid data © Fingrid, CC BY 4.0; weather © FMI Open Data."
)
