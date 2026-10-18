FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_DEFAULT_TIMEOUT=120 \
    UV_SYSTEM_PYTHON=1 \
    PORT=8080 \
    DATA_DIR=/data \
    DATABASE_URL=sqlite:////data/bonus.db

WORKDIR /app

# Слой зависимостей отдельно — при неизменном requirements.txt кэшируется.
COPY requirements.txt .

# uv ставит пакеты заметно быстрее pip; кэш BuildKit переживает повторные сборки.
# Официальный PyPI: зеркало Яндекса на билде Timeweb отдавало пустой индекс.
RUN --mount=type=cache,target=/root/.cache/pip \
    --mount=type=cache,target=/root/.cache/uv \
    pip install --retries 8 uv \
    && uv pip install --system --retries 8 -r requirements.txt

COPY bonus ./bonus

RUN mkdir -p /data

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health')"

CMD ["sh", "-c", "uvicorn bonus.main:app --host 0.0.0.0 --port ${PORT:-8080} --proxy-headers --forwarded-allow-ips=*"]
