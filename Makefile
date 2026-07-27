.PHONY: install fixtures discover ingest transform report all test lint clean

install:
	python3 -m venv .venv
	.venv/bin/pip install -U pip
	.venv/bin/pip install -e ".[dev]"

fixtures:
	.venv/bin/python tests/make_fixtures.py

discover:  ; .venv/bin/fitbit discover
ingest:    ; .venv/bin/fitbit ingest
transform: ; .venv/bin/fitbit transform
report:    ; .venv/bin/fitbit report
all:       ; .venv/bin/fitbit all

test:
	.venv/bin/pytest -q

lint:
	.venv/bin/ruff check src tests

clean:
	rm -rf data reports fixtures
	find . -type d -name __pycache__ -exec rm -rf {} +
