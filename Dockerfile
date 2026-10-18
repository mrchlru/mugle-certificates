FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_DEFAULT_TIMEOUT=120 \
    PORT=8080 \
    DATA_DIR=/data \
    DATABASE_URL=sqlite:////data/bonus.db

WORKDIR /app

# Слой зависимостей отдельно — при неизменном requirements.txt кэшируется.
COPY requirements.txt .

# Кэш pip между сборками + зеркало ближе к RU (Timeweb), чтобы уложиться в лимит ~28 мин.
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install \
      --retries 8 \
      -i https://mirror.yandex.ru/mirrors/pypi/simple/ \
      --trusted-host mirror.yandex.ru \
      -r requirements.txt

COPY bonus ./bonus

RUN mkdir -p /data

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health')"

CMD ["sh", "-c", "uvicorn bonus.main:app --host 0.0.0.0 --port ${PORT:-8080} --proxy-headers --forwarded-allow-ips=*"]
