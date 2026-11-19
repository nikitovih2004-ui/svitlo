# Сборка из корня проекта:
#   docker build -f deploy/Dockerfile -t svitlo .
# Запуск (логгер должен быть доступен из контейнера; data — том для sqlite):
#   docker run -d --name svitlo --restart unless-stopped --env-file .env -v svitlo-data:/app/data svitlo
# Отчёт о точности прогноза: docker exec svitlo python -m svitlo accuracy
# .env передаётся через --env-file (в образ он не копируется); BATTERY_KWH — там же.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 DATA_DIR=/app/data
WORKDIR /app

COPY requirements.txt .
# tzdata — на случай, если в образе нет системной базы часовых поясов (zoneinfo)
RUN pip install --no-cache-dir -r requirements.txt tzdata \
 && useradd --system --uid 10001 svitlo && mkdir -p /app/data && chown svitlo /app/data

COPY svitlo ./svitlo
COPY widgets ./widgets

USER svitlo
VOLUME ["/app/data"]
CMD ["python", "-m", "svitlo"]
