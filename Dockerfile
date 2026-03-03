FROM python:3.12-slim AS base

WORKDIR /app

# System deps
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl graphviz && \
    rm -rf /var/lib/apt/lists/*

# Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application code
COPY src/ ./src/
COPY prompts/ ./prompts/
COPY .env.example .env

# Environment
ENV PYTHONPATH=/app/src \
    PYTHONUNBUFFERED=1 \
    PORT=8088 \
    RUN_MODE=server \
    MOCK_MODE=false

EXPOSE 8088

# Health check
HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
    CMD curl -f http://localhost:8088/health || exit 1

# Run
CMD ["python", "-m", "orchestrator_app.main"]
