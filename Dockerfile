# syntax=docker/dockerfile:1
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

# curl : pour le healthcheck HTTP de Coolify (les images sans curl/wget le font échouer).
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app

# Utilisateur non-root ; /data = volume persistant (SQLite). Créé ici pour que le volume nommé
# hérite de l'ownership appuser au premier montage.
RUN useradd -r -u 10001 appuser && mkdir -p /data && chown -R appuser /app /data
USER appuser

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8000/health || exit 1

# 1 worker suffit (I/O-bound, tout est async) → ~70-90 Mo RSS au repos.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
