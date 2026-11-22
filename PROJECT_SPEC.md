# Project Spec: Finland Grid Forecast

**Day-ahead forecasting of Finnish electricity consumption: an end-to-end ML system with a data pipeline, SQL models, experiment tracking, an API, a dashboard and scheduled retraining.**

> This file is written to be handed to Codex. Put it in the repo root as `PROJECT_SPEC.md`, and copy the "Instructions for Codex" section into `AGENTS.md`. Build it one milestone at a time (see "Build plan").

---

## 1. Why this project

My portfolio (Omer Ahmed: MSc Data-Centric Engineering, LUT) already shows deep learning, NLP/LLMs, and backend work from two hackathons. What it does **not** show yet is the work Finnish data and ML teams do every day:

| Gap in my portfolio | What this project shows |
|---|---|
| No production-style ML lifecycle | Scheduled ingestion → features → training → evaluation → serving → monitoring |
| SQL only listed as a skill | Real SQL models (dbt + DuckDB) with tests |
| No MLOps / experiment tracking | MLflow runs, model registry, reproducible training |
| No deployed, live ML app | Public dashboard + API that update daily |
| No Finland-specific data work | Fingrid (national grid) and FMI (weather) open data |
| Time series only in a course | A real forecasting problem with a strong official baseline to beat |

**The headline story:** *"I built a system that forecasts Finland's hourly electricity consumption for the next day and compared it honestly with Fingrid's own published forecast."*

---

## 2. Problem definition

- **Target:** Finland's hourly electricity consumption (MWh/h) for the next 24–48 hours.
- **Forecast time:** every day at a fixed time (e.g. 10:00 Helsinki time), forecast the next day 00:00–23:00 local time.
- **Baselines (must be implemented and reported):**
  1. Seasonal naive: same hour last week.
  2. Fingrid's own published consumption forecast. Find the right dataset in the Fingrid catalogue (search "Electricity consumption forecast"). Do **not** hardcode an ID without checking it in the catalogue.
- **Metrics:** MAE, RMSE and MAPE on the hourly forecasts; also report error by hour of day and by month. All metrics are computed on a strict time-based hold-out (never shuffle).
- **Honesty rule:** if the model does not beat Fingrid's forecast, say so in the README and explain why. A clear, honest comparison is worth more than a fake win.

---

## 3. Data sources

| Source | What | Access |
|---|---|---|
| **Fingrid Open Data** (data.fingrid.fi) | Electricity consumption in Finland (dataset **124**, 15-min), real-time consumption (**193**), production (**192**), wind/solar/consumption forecasts | Free REST API; register for an API key and send it in the `x-api-key` header. Licence CC BY 4.0, so credit Fingrid in the README and dashboard. |
| **FMI Open Data** (Finnish Meteorological Institute) | Hourly temperature (and optionally wind, cloud cover) for several stations weighted toward population centres (Helsinki, Tampere, Turku, Oulu, Jyväskylä); FMI forecasts for future hours | Python package `fmiopendata` (WFS stored queries). Check the current stored-query list in FMI's open-data manual. |
| **Finnish public holidays** | Holiday flags | Python package `holidays` (country "FI") |
| *(Optional, stretch)* **ENTSO-E Transparency Platform** | Day-ahead spot prices, bidding zone FI | Requires a security token: register, then email transparency@entsoe.eu with "Restful API access" in the subject. Request it on day 1 because approval can take time. |

**Rules**
- Store all secrets in `.env` (git-ignored); commit `.env.example` with variable names only.
- Respect API rate limits: add retries with backoff, and cache raw responses to disk so re-runs don't re-download.
- Work in **UTC internally**; convert to `Europe/Helsinki` only for display and calendar features (DST matters).

---

## 4. Architecture

```
            ┌──────────────┐     ┌──────────────┐
            │  Fingrid API │     │   FMI WFS    │
            └──────┬───────┘     └──────┬───────┘
                   │  ingest (Python, idempotent, incremental)
                   ▼
            ┌───────────────────────────────┐
            │ DuckDB: raw.* tables          │
            └──────────────┬────────────────┘
                           │  dbt-duckdb (SQL models + tests)
                           ▼
            ┌───────────────────────────────┐
            │ staging.* → marts.features    │  hourly, UTC, one row per hour
            └──────────────┬────────────────┘
                           │  train / evaluate (MLflow tracking)
                           ▼
            ┌───────────────────────────────┐
            │ LightGBM model + baselines    │
            └──────┬─────────────────┬──────┘
                   │                 │
          FastAPI /forecast    Streamlit dashboard
                   │                 │
            GitHub Actions: daily ingest → predict → evaluate → publish
```

---

## 5. Tech stack (keep it to these)

- **Python 3.11**, dependency management with **uv** (or pip + `requirements.txt` if uv causes trouble)
- **DuckDB** as the local warehouse, **dbt-duckdb** for SQL transformations and tests
- **pandas / polars**, **scikit-learn**, **LightGBM**
- **MLflow** (local file store) for experiment tracking
- **FastAPI** + **Pydantic** for the API, **Streamlit** + **Plotly** for the dashboard
- **pytest**, **ruff** (lint + format), **pre-commit**
- **Docker** + `docker-compose.yml` (api + dashboard)
- **GitHub Actions** for CI (lint + tests) and a scheduled daily pipeline

---

## 6. Repository layout

```
finland-grid-forecast/
├── README.md
├── PROJECT_SPEC.md
├── AGENTS.md
├── pyproject.toml
├── .env.example
├── Makefile                  # make ingest / transform / train / predict / serve / dashboard / test
├── docker-compose.yml
├── src/gridcast/
│   ├── config.py             # settings from env (pydantic-settings)
│   ├── ingest/
│   │   ├── fingrid.py        # client: paging, retries, caching
│   │   └── fmi.py
│   ├── features/build.py     # reads marts.features, returns X, y
│   ├── models/
│   │   ├── baselines.py      # seasonal naive, Fingrid forecast
│   │   ├── train.py          # time-series CV, MLflow logging
│   │   └── predict.py
│   ├── evaluate/backtest.py  # rolling-origin backtest, metrics tables + plots
│   ├── monitor/drift.py      # daily error tracking, data-quality checks
│   └── api/main.py           # FastAPI app
├── dbt/
│   ├── dbt_project.yml
│   └── models/{staging,marts}/   # *.sql + schema.yml with tests
├── dashboard/app.py
├── tests/
├── data/                     # git-ignored (DuckDB file, raw cache)
├── reports/                  # backtest results, figures (committed)
└── .github/workflows/
    ├── ci.yml
    └── daily.yml
```

---

## 7. Build plan (milestones)

Build **in this order**. Each milestone ends with passing tests, a commit, and an updated README section. Do not start the next milestone until the current one meets its acceptance criteria.

### M1: Skeleton and ingestion
- Repo scaffold, `pyproject.toml`, ruff, pytest, pre-commit, Makefile, `.env.example`.
- Fingrid client: fetch a dataset for a date range, handle pagination and rate limits, retry with backoff, cache raw JSON, load into `raw.fingrid_<dataset>` in DuckDB. **Incremental**: only fetch what is missing.
- FMI client: hourly temperature for the chosen stations into `raw.fmi_weather`.
- Backfill at least **3 years** of history.

**Done when:** `make ingest` runs twice in a row and the second run downloads nothing new; unit tests mock the HTTP calls (no network in tests).

### M2: SQL models with dbt
- `staging` models: clean types, convert to UTC, resample 15-min consumption to hourly, deduplicate.
- `marts.features`: one row per hour with the target, lags (24h, 48h, 168h), rolling means, calendar features (hour, weekday, month, Finnish holidays, DST flag), population-weighted temperature, heating degree hours.
- dbt tests: `not_null`, `unique` on the timestamp, accepted ranges (e.g. consumption between 3,000 and 16,000 MWh/h; tune after looking at the data), and no gaps in the hourly series.

**Done when:** `make transform` builds all models and `dbt test` passes.

### M3: Baselines, model, backtest
- Implement both baselines.
- LightGBM model on `marts.features`. **No leakage**: only use information available at forecast time (e.g. temperature for future hours must come from a *forecast*, or document clearly that actual temperature is used as an "oracle" upper bound and report both).
- Rolling-origin backtest over at least the last 12 months.
- Log every run to MLflow (params, metrics, feature importance plot, model artifact).
- Write `reports/backtest.md` with a metrics table (model vs. both baselines), error by hour and month, and 2–3 plots.

**Done when:** `make train` and `make backtest` produce the report with real numbers.

### M4: API and dashboard
- FastAPI: `GET /health`, `GET /forecast?date=YYYY-MM-DD`, `GET /metrics`. Pydantic schemas, OpenAPI docs, tests with `TestClient`.
- Streamlit dashboard: tomorrow's forecast vs. Fingrid's forecast, last 30 days of actual vs. predicted, rolling MAE/MAPE, a short "About" panel crediting Fingrid and FMI.
- `docker-compose up` starts both.

**Done when:** both run locally with one command and the dashboard reads from the same DuckDB/predictions as the API.

### M5: Automation, monitoring, deployment
- `ci.yml`: ruff + pytest on every push and PR.
- `daily.yml`: scheduled run that ingests new data, rebuilds dbt models, predicts tomorrow, evaluates yesterday, and stores predictions and the daily metrics (e.g. as CSV/Parquet in a `data-artifacts` branch or as a release asset). API keys go in GitHub Secrets.
- Monitoring: track daily error; flag data-quality problems (missing hours, stale data) and error spikes in a `monitoring` table shown on the dashboard.
- Deploy the dashboard publicly (Streamlit Community Cloud or Hugging Face Spaces) reading the published artifacts.

**Done when:** the public dashboard updates automatically for 3 days in a row without manual work.

### M6 (stretch, only if credits remain): pick ONE
- **A. "Ask the grid":** a natural-language question box that turns questions into **read-only SQL** against the DuckDB marts with an LLM, shows the SQL, runs it and plots the answer. Must include a whitelist of tables, a row limit, and an eval set of ~20 questions with expected answers. *(Builds on my LLM experience.)*
- **B. Spot price forecasting** for FI using ENTSO-E prices, re-using the same pipeline.
- **C. Probabilistic forecasts:** quantile LightGBM (P10/P50/P90) with coverage reported in the backtest.

---

## 8. README requirements

The README is what recruiters read. It must contain, in this order:
1. One-sentence description + **link to the live dashboard** + a screenshot/GIF.
2. **Results table**: model vs. seasonal naive vs. Fingrid forecast (MAE, RMSE, MAPE) with the test period stated.
3. Architecture diagram (the one above, or a cleaner version).
4. How to run it (`make` commands, Docker).
5. Design decisions and **what didn't work** (2–5 honest bullets).
6. Data credits: Fingrid Open Data (CC BY 4.0) and FMI Open Data.

---

## 9. Instructions for Codex (copy into `AGENTS.md`)

- Work on **one milestone at a time**. Before writing code, write a short plan for the milestone and the files you will change.
- Keep functions small and typed; use type hints everywhere. Run `ruff` and `pytest` before declaring a task done.
- Tests must **not** call real APIs; mock HTTP responses with saved fixtures.
- Never commit secrets, `.env`, or the DuckDB file. Never print API keys in logs.
- Use UTC internally; convert to `Europe/Helsinki` only for calendar features and display.
- Prevent data leakage: every feature must be computable at forecast time. If unsure, ask or document the assumption.
- Do not invent dataset IDs, API endpoints or stored-query names: check them in the official docs/catalogue, and put them in `config.py` with a comment linking to the source.
- Prefer simple, readable solutions over clever ones. No new dependencies outside the stack in section 5 without a stated reason.
- After each milestone, update the README section for it and write a clear commit message explaining *why*.

---

## 10. Using the Codex credits well

- Give Codex **one milestone per task**, with the acceptance criteria pasted in. Big vague prompts burn credits.
- Do the API-key registration (Fingrid, optionally ENTSO-E) yourself first, and test one request manually.
- Review and run the code after each milestone; fix small things yourself instead of re-prompting.
- If credits run low, **M1–M4 + a good README is already a strong project**. M5 is what makes it stand out; M6 is a bonus.

---

## 11. For the resume / LinkedIn (fill in with REAL results only)

Title: **Finland Grid Forecast: Day-Ahead Electricity Consumption Forecasting (Personal Project)**

Bullet templates. Replace the brackets with your actual numbers after M3–M5:
- Built an end-to-end forecasting system for Finland's hourly electricity consumption using Fingrid and FMI open data: incremental ingestion, dbt SQL models on DuckDB with data tests, LightGBM, MLflow tracking, FastAPI and a live Streamlit dashboard.
- Achieved [X]% MAPE on a [12-month] rolling backtest vs. [Y]% for a seasonal-naive baseline and [Z]% for Fingrid's published forecast.
- Automated daily ingestion, prediction and evaluation with GitHub Actions; the dashboard has updated daily since [month year] with data-quality and error monitoring.

Skills to tag: Time Series Forecasting, SQL, dbt, MLOps, MLflow, LightGBM, FastAPI, Docker, GitHub Actions, Python.
