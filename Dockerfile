# Forensic Workbench: one image with the API, the built UI, FFmpeg and the bundled models.
# Build:  docker build -t forensic-workbench:0.3.0 .
# Run:    docker compose up   (see docker-compose.yml; no network access is needed at run time)

FROM node:22-bookworm-slim AS ui
WORKDIR /src/frontend
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci || npm install
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim-bookworm
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /app/backend
COPY backend/ ./
RUN pip install --no-cache-dir . \
 && useradd --system --uid 10001 --home /data forensic \
 && mkdir -p /data /import && chown forensic /data /import
COPY --from=ui /src/frontend/dist /app/frontend/dist
ENV FORENSIC_DATA_DIR=/data \
    FORENSIC_IMPORT_DIR=/import \
    FORENSIC_FRONTEND_DIST=/app/frontend/dist \
    FORENSIC_DETERMINISTIC=1
USER forensic
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/cases')" || exit 1
CMD ["uvicorn", "forensic.api:app", "--host", "0.0.0.0", "--port", "8000"]
