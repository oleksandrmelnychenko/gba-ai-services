.PHONY: install dev test integration lint eval calibration smoke worker up down

install:
	python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"

dev:
	.venv/bin/uvicorn app.api.main:app --reload --host 0.0.0.0 --port 8000

test:
	.venv/bin/pytest -q -m "not integration"

integration:
	.venv/bin/pytest -q -m integration

lint:
	.venv/bin/ruff check app tests

eval:
	.venv/bin/python -m app.services.eval.harness --k 10

calibration:
	.venv/bin/python -m app.services.eval.harness --k 10 --baseline --limit 120

smoke:
	.venv/bin/python scripts/smoke_test.py

worker:
	.venv/bin/python -m app.services.recommendations.worker --top-n 50

up:
	docker compose up -d --build

down:
	docker compose down
