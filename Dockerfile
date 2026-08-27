FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PULSEWATCH_DATABASE_PATH=/app/data/pulsewatch.db

WORKDIR /app

RUN addgroup --system pulsewatch && adduser --system --ingroup pulsewatch pulsewatch

COPY pyproject.toml README.md ./
COPY app ./app

RUN pip install --no-cache-dir . && mkdir -p /app/data && chown -R pulsewatch:pulsewatch /app

USER pulsewatch

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3)"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
