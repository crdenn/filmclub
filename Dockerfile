FROM python:3.12-slim

# Version is injected at build time (e.g. from a release tag) and surfaced in
# container labels, /readyz, and admin diagnostics.
ARG FILMCLUB_VERSION=0.9.0

LABEL org.opencontainers.image.title="Film Club Tracker" \
      org.opencontainers.image.description="Self-hosted weekly film club tracker" \
      org.opencontainers.image.version="${FILMCLUB_VERSION}" \
      org.opencontainers.image.licenses="AGPL-3.0-or-later" \
      org.opencontainers.image.source="https://github.com/crdenn/filmclub"

# Runtime environment
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/data \
    PORT=8000 \
    FILMCLUB_VERSION=${FILMCLUB_VERSION}

WORKDIR /app

# Install dependencies first for better layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code
COPY app ./app
COPY static ./static

# The SQLite file lives on a bind-mounted volume so backups are a file copy.
VOLUME ["/data"]
EXPOSE 8000

# Simple container healthcheck against the app's health endpoint.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz').status==200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
