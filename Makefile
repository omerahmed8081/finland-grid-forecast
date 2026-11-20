PYTHON ?= python

.PHONY: install ingest transform train backtest lint test

install:
	$(PYTHON) -m pip install -e ".[dev]"

ingest:
	$(PYTHON) -m gridcast.ingest.cli

transform:
	$(PYTHON) -m gridcast.transform.seeds
	dbt build --project-dir dbt --profiles-dir dbt

train:
	$(PYTHON) -m gridcast.models.train

backtest:
	$(PYTHON) -m gridcast.models.backtest

lint:
	ruff check .
	ruff format --check .

test:
	pytest
