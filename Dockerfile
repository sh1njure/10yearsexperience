# PrestaShop Supplier Importer — container image.
# Small single-stage build; the app is pure Python + static assets.
FROM python:3.11-slim

# Faster, quieter, no .pyc clutter.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install dependencies first so the layer is cached across code changes.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Application code and assets.
COPY app ./app
COPY static ./static
COPY data ./data

# SQLite (history + profiles) lives here; mount a volume to persist it.
ENV DATABASE_PATH=/app/appdata/app.sqlite3
RUN mkdir -p /app/appdata

# Run as a non-root user.
RUN useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# Bind 0.0.0.0 inside the container; the host maps it to 127.0.0.1 only
# (see docker-compose.yml) so the tool stays reachable from localhost only.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
