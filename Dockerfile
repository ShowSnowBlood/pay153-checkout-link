FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PAY153_HOST=0.0.0.0 \
    PAY153_PORT=18096

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends nodejs npm ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt package.json package-lock.json ./
RUN pip install --no-cache-dir -r requirements.txt \
    && npm ci --omit=dev \
    && useradd --create-home --uid 10001 pay153 \
    && mkdir -p /app/data /app/logs \
    && chown -R pay153:pay153 /app

COPY --chown=pay153:pay153 . .

USER pay153
EXPOSE 18096

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os, urllib.request; assert os.access('/app/data', os.W_OK) and os.access('/app/logs', os.W_OK); urllib.request.urlopen('http://127.0.0.1:18096/api/health', timeout=3)" || exit 1

CMD ["gunicorn", "--workers", "1", "--threads", "12", "--timeout", "600", "--bind", "0.0.0.0:18096", "app:app"]
