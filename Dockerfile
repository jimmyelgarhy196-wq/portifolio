# GMG Investment Intelligence — production image
#
# Two stages so the runtime image carries no compiler and no build cache.
#
#   docker build -t gmg .
#   docker run --env-file .env -p 8000:8000 gmg

# --- Build -------------------------------------------------------------------
FROM python:3.11-slim AS build

ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1

# lxml and psycopg need a compiler and headers; the runtime image will not.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential libxml2-dev libxslt1-dev libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY requirements.txt .
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && /opt/venv/bin/pip install -r requirements.txt

# --- Runtime -----------------------------------------------------------------
FROM python:3.11-slim

# libxml2/libxslt runtimes for lxml; libpq5 for psycopg. No build tools.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libxml2 libxslt1.1 libpq5 curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 gmg

COPY --from=build /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    EGX_ENV=production \
    EGX_HOST=0.0.0.0 \
    EGX_PORT=8000

WORKDIR /app
COPY --chown=gmg:gmg backend/ ./backend/
COPY --chown=gmg:gmg frontend/ ./frontend/
COPY --chown=gmg:gmg config/ ./config/
COPY --chown=gmg:gmg scripts/ ./scripts/

# Writable only where it must be: the SQLite fallback and manual CSV imports.
RUN mkdir -p /app/data /app/database && chown -R gmg:gmg /app/data /app/database

COPY --chown=gmg:gmg docker-entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

USER gmg
EXPOSE 8000

# The health endpoint reports the real quote provider and payment state, so a
# green container is one that can actually answer for itself.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/api/health || exit 1

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["gunicorn", "backend.api.app:app", \
     "-k", "uvicorn.workers.UvicornWorker", \
     "--bind", "0.0.0.0:8000", \
     "--workers", "4", \
     "--timeout", "60", \
     "--access-logfile", "-", \
     "--error-logfile", "-"]
