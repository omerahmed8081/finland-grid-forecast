# Finland Grid Forecast

An end-to-end project for forecasting Finland's hourly electricity consumption. The live dashboard and measured forecast results are planned for later milestones and are not available yet.

## Results

No forecast has been evaluated yet; results will be reported after the backtesting milestone.

| Model | MAE | RMSE | MAPE |
|---|---:|---:|---:|
| Forecast model | — | — | — |
| Seasonal naive | — | — | — |
| Fingrid published forecast | — | — | — |

## M1 ingestion snapshot

The backfill covers 2023-11-20 19:00 UTC through 2026-11-20 19:00 UTC. DuckDB contains 104,831 Fingrid consumption records and 26,305 hourly FMI observations for each of the five configured places. An immediate repeat reported zero newly downloaded Fingrid pages and FMI responses.

## M1 architecture

```mermaid
flowchart LR
    Fingrid["Fingrid dataset 124"] --> Ingest["Incremental ingestion"]
    FMI["FMI hourly observations"] --> Ingest
    Ingest --> DuckDB["DuckDB raw schema"]
```

## Run ingestion

Use Python 3.11 or newer. Install the project and development tools, then copy `.env.example` to `.env` and set `FINGRID_API_KEY` to a registered Fingrid Open Data API key.

```sh
make install
make ingest
```

`make ingest` backfills at least three years of consumption and hourly weather observations into `data/gridcast.duckdb`. Raw HTTP responses are cached under `data/raw_cache`. A later run requests only time ranges that are not already recorded as successfully ingested. Fingrid requests use the `x-api-key` header and are paced to the API's published rate limit.

Useful checks:

```sh
make lint
make test
```

The local pre-commit hooks use the same Ruff checks. Install them with `pre-commit install` after `make install`.

## Design decisions and current limits

- All stored timestamps are timezone-aware UTC timestamps. Helsinki time is reserved for calendar features and display work in later milestones.
- Fingrid dataset 124 is the official consumption dataset. Its API coverage is recorded separately from returned measurements so empty periods are not repeatedly downloaded.
- FMI observations use the official `fmi::observations::weather::timevaluepair` WFS stored query with hourly `t2m` observations for Helsinki, Tampere, Turku, Oulu, and Jyväskylä. Requests cover one week (the service limit) and ask for all five places together.
- Transformations, forecasting, serving, and the dashboard have not been started; they belong to later milestones.

## Data credits

- Fingrid Open Data, electricity consumption dataset 124, licensed under CC BY 4.0. Source: [Fingrid Open Data](https://data.fingrid.fi/en/datasets/124).
- Finnish Meteorological Institute (FMI) Open Data weather observations. Source: [FMI Open Data WFS manual](https://en.ilmatieteenlaitos.fi/open-data-manual-fmi-wfs-services).
