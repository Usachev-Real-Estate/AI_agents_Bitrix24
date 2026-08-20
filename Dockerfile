# Build stage: compile wheels so the runtime image carries no toolchain.
FROM python:3.11-slim AS build

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip wheel --no-cache-dir --wheel-dir /wheels -r requirements.txt


FROM python:3.11-slim

WORKDIR /app

COPY --from=build /wheels /wheels
COPY requirements.txt .
RUN pip install --no-cache-dir --no-index --find-links=/wheels -r requirements.txt \
    && rm -rf /wheels

COPY src/ ./src/

# Run as non-root user
RUN useradd -m auditor && chown -R auditor:auditor /app && \
    mkdir -p /app/logs /app/data && chown auditor:auditor /app/logs /app/data
USER auditor

CMD ["python", "src/main.py"]
