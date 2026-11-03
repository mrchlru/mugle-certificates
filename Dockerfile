FROM ghcr.io/astral-sh/uv:0.6.14 AS uvbin

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_SYSTEM_PYTHON=1 \
    UV_LINK_MODE=copy \
    PORT=8080 \
    DATA_DIR=/data \
    DATABASE_URL=sqlite:////data/bonus.db

WORKDIR /app

COPY --from=uvbin /uv /usr/local/bin/uv

RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

# Офлайн-установка: wheels в репозитории — сборка Timeweb не зависит от медленного PyPI.
COPY requirements.txt .
COPY wheels ./wheels
RUN uv pip install --system --link-mode=copy --no-index --find-links=/app/wheels -r requirements.txt \
    && rm -rf /app/wheels

COPY bonus ./bonus

RUN mkdir -p /data

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health')"

CMD ["sh", "-c", "uvicorn bonus.main:app --host 0.0.0.0 --port ${PORT:-8080} --proxy-headers --forwarded-allow-ips=${FORWARDED_ALLOW_IPS:-127.0.0.1}"]
