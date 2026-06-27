.PHONY: install dev test integration lint smoke up down

install:
	python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"

dev:
	.venv/bin/uvicorn app.api.main:app --reload --host 0.0.0.0 --port 8003

test:
	.venv/bin/pytest -q -m "not integration"

integration:
	.venv/bin/pytest -q -m integration

lint:
	.venv/bin/ruff check app tests

smoke:
	.venv/bin/python scripts/smoke_test.py

up:
	docker compose up -d --build

down:
	docker compose down
