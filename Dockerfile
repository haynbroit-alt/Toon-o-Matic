# Image COMMAND (+ brouillons CPU) : légère, pour Render ou toute plateforme à conteneurs.
FROM python:3.12-slim
# libcairo2 : dessin vectoriel des personnages (théâtre de marionnettes 2D) ; outils de compilation pour pycairo
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core curl ca-certificates \
        libcairo2 libcairo2-dev pkg-config gcc \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY . .
RUN pip install --no-cache-dir ".[claude,cloud,piper,vector]" \
    && apt-get purge -y gcc libcairo2-dev pkg-config && apt-get autoremove -y
# Voix gratuites (Piper, CPU) : de vraies voix sans clé. Si le téléchargement échoue, l'usine marche quand même.
RUN sh deploy/fetch_piper_voices.sh assets/piper
ENV PYTHONUNBUFFERED=1 TOONOMATIC_HOME=/app
CMD ["sh", "-c", "toonomatic -c config/cloud.yaml serve --host 0.0.0.0 --port ${PORT:-8080}"]
