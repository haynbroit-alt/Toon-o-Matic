#!/bin/sh
# Télécharge les voix Piper (open source, CPU) dans assets/piper/. Tolérant : une voix absente
# n'empêche rien, l'usine se rabat sur la voix par défaut de la langue, puis sur des bips.
# Vérifiez la licence de chaque voix avant une exploitation commerciale (fichiers MODEL_CARD du dépôt).
set -u
DEST="${1:-assets/piper}"
BASE="https://huggingface.co/rhasspy/piper-voices/resolve/main"
mkdir -p "$DEST"
for V in fr/fr_FR/siwis/medium/fr_FR-siwis-medium fr/fr_FR/tom/medium/fr_FR-tom-medium \
         fr/fr_FR/gilles/low/fr_FR-gilles-low en/en_GB/alan/medium/en_GB-alan-medium \
         en/en_GB/alba/medium/en_GB-alba-medium \
         en/en_GB/northern_english_male/medium/en_GB-northern_english_male-medium; do
  N=$(basename "$V")
  for E in onnx onnx.json; do
    [ -s "$DEST/$N.$E" ] && continue
    if ! curl -fsSL --retry 3 -m 300 -o "$DEST/$N.$E" "$BASE/$V.$E"; then
      echo "voix indisponible : $N.$E" >&2; rm -f "$DEST/$N.$E"
    fi
  done
done
ls -la "$DEST" || true
exit 0
