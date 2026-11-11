# Image COMMAND (+ brouillons CPU) : légère, pour Render ou toute plateforme à conteneurs.
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY . .
RUN pip install --no-cache-dir ".[claude,cloud]"
ENV PYTHONUNBUFFERED=1 TOONOMATIC_HOME=/app
CMD ["sh", "-c", "toonomatic -c config/cloud.yaml serve --host 0.0.0.0 --port ${PORT:-8080}"]
