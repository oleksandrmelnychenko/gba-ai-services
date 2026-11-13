# GBA AI Sales Cockpit (NBA) — API / scheduler image (non-root, slim).
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# pymssql needs FreeTDS at runtime; gcc only for the install.
RUN apt-get update && apt-get install -y --no-install-recommends \
        freetds-dev gcc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml ./
RUN pip install --upgrade pip && pip install \
        "fastapi>=0.115" "uvicorn[standard]>=0.34" "pydantic>=2.10" \
        "pydantic-settings>=2.7" "pymongo>=4.10" "sqlalchemy>=2.0.36" \
        "pymssql>=2.3.1" "httpx>=0.28" "structlog>=24.4" "apscheduler>=3.10"

COPY app ./app

RUN useradd --create-home --uid 10001 appuser
USER appuser

EXPOSE 8002
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8002/health').status==200 else 1)"

# default = API; the scheduler service overrides CMD (see docker-compose.yml).
CMD ["uvicorn", "app.api.main:app", "--host", "0.0.0.0", "--port", "8002"]
