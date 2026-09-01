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
# Крон запускает QC-агента как scripts/run_client_state_qc_pilot.py — это
# единственный job, который живёт не в src/. Без этой строки контейнер его
# не находит, и отчёт РОПам не уходит вовсе: «can't open file». Проверить
# это на ручных прогонах нельзя — они идут через .venv на хосте, где
# scripts/ есть всегда.
#
# Копией, а не монтированием тома: образ должен быть самодостаточным.
# Смонтированный scripts/ разъезжается с кодом внутри образа — ровно тот
# класс расхождения, из-за которого образ и пересобирают.
COPY scripts/ ./scripts/

# Run as non-root user
RUN useradd -m auditor && chown -R auditor:auditor /app && \
    mkdir -p /app/logs /app/data && chown auditor:auditor /app/logs /app/data
USER auditor

# Справочно: веб-дашборд слушает 8080. Публикуется только на 127.0.0.1 хоста
# (см. docker-compose.yml), наружу его выставляет nginx с TLS.
EXPOSE 8080

CMD ["python", "src/main.py"]
