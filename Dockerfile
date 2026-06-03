FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY src/ ./src/

# Run as non-root user
RUN useradd -m auditor && chown -R auditor:auditor /app && \
    mkdir -p /app/logs && chown auditor:auditor /app/logs
USER auditor

CMD ["python", "src/main.py"]
