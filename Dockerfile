FROM python:3.12-slim

WORKDIR /app

# git: code mode (trading/codemode.py) merges/reverts branches with plain,
# fixed-argument git commands from this service - no Node/npm/Claude Code
# CLI here, that stays isolated in the separate trading-codemode job.
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY trading/ trading/

ENV PORT=8080
EXPOSE 8080

CMD exec gunicorn --bind 0.0.0.0:${PORT} --workers 1 --threads 8 --timeout 0 trading.web:app
