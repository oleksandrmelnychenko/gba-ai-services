API_HOST ?= 127.0.0.1

.PHONY: install dev test integration lint plan up down

install:
	python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"

dev:
	.venv/bin/uvicorn app.api.main:app --reload --host $(API_HOST) --port 8001

test:
	.venv/bin/pytest -q -m "not integration"

integration:
	.venv/bin/pytest -q -m integration

lint:
	.venv/bin/ruff check app tests

up:
	docker compose up -d --build

down:
	docker compose down
