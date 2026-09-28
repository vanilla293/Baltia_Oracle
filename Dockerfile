# Baltia Oracle в Docker.
#   docker compose up -d            — собрать и запустить в фоне
#   docker compose logs -f          — смотреть лог
# Локальное распознавание голоса (faster-whisper) в образ не входит; включить:
#   в docker-compose.yml LOCAL_WHISPER: "1", затем docker compose up -d --build
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# сначала зависимости — чтобы правка кода не пересобирала их заново
COPY requirements.txt requirements-voice.txt ./
RUN pip install --no-cache-dir -r requirements.txt

ARG LOCAL_WHISPER=0
RUN if [ "$LOCAL_WHISPER" = "1" ]; then pip install --no-cache-dir -r requirements-voice.txt; fi

COPY . .

# база, файл сессии userbot, модели whisper — всё здесь; снаружи монтируется ./data
VOLUME /app/data

# SIGINT — бот завершается аккуратно (как по Ctrl+C), а не убивается через 10 секунд
STOPSIGNAL SIGINT

CMD ["python", "-m", "oracle"]
