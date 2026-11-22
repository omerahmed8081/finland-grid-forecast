PYTHON ?= python

.PHONY: install ingest forecast-weather transform train backtest serve dashboard lint test

install:
	$(PYTHON) -m pip install -e ".[dev]"

ingest:
	$(PYTHON) -m gridcast.ingest.cli

forecast-weather:
	$(PYTHON) -m gridcast.ingest.forecast_job

transform:
	$(PYTHON) -m gridcast.transform.seeds
	dbt build --project-dir dbt --profiles-dir dbt

train:
	$(PYTHON) -m gridcast.models.train

backtest:
	$(PYTHON) -m gridcast.models.backtest

serve:
	uvicorn gridcast.api.main:app --host 0.0.0.0 --port 8000

dashboard:
	streamlit run dashboard/app.py

lint:
	ruff check .
	ruff format --check .

test:
	pytest
