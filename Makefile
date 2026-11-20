PYTHON ?= python

.PHONY: install ingest lint test

install:
	$(PYTHON) -m pip install -e ".[dev]"

ingest:
	$(PYTHON) -m gridcast.ingest.cli

lint:
	ruff check .
	ruff format --check .

test:
	pytest
