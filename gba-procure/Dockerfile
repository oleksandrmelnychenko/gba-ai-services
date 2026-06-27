# GBA Procurement Service — API image (non-root, slim).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        freetds-dev gcc \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml ./
RUN pip install --upgrade pip && pip install \
        "fastapi>=0.115" "uvicorn[standard]>=0.34" "pydantic>=2.10" \
        "pydantic-settings>=2.7" "sqlalchemy>=2.0.36" "pymssql>=2.3.1" \
        "redis>=5.2" "structlog>=24.4" "numpy>=2.1"

COPY app ./app

RUN useradd --create-home --uid 10001 appuser
USER appuser

EXPOSE 8001
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8001/health').status==200 else 1)"

CMD ["uvicorn", "app.api.main:app", "--host", "0.0.0.0", "--port", "8001"]
