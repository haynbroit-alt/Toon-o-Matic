# Toon-o-Matic — notes pour les agents de code

- Python 3.11+, FFmpeg, Cairo pour le rendu 2D. Vérification avant commit (comme la CI) :
  `.claude/bin/journal verif bash -o pipefail -c 'python3 -m pytest -q'` (environ 10 minutes : la lancer une fois).
- Langue : identifiants en anglais ; docstrings, messages, configuration et docs en français.
- Dépôt public : aucune donnée personnelle, aucune clé. Les clés vivent dans l'environnement du processus.
- 0 € par défaut : aucun GPU loué, aucune API payante sans l'accord explicite du propriétaire. Rien n'est publié
  sans sa validation (`review`, puis `publish`).
- Voix gratuites : `piper` (Piper) et `pocket` (PocketTTS, moteur CPU de VoiceStudio, avec Piper pour les
  personnages sans `pocket_voice`). `mock` n'est pas une voix : les jalons de l'épisode la signalent « FACTICE ».
- Licences : vérifier celle de chaque voix ou modèle avant un usage commercial (MODEL_CARD, fiche du modèle).

## Fiabilité avant sobriété — la preuve prime sur le plafond de sortie
- Toute vérification passe par `.claude/bin/journal <étiquette> <commande>` : journal complet sur disque, résumé
  jamais tronqué, code de sortie exact en première et dernière ligne.
- Jamais `vérification | tail/head/grep` sans `pipefail` : le pipe renvoie le code du filtre et masque l'échec.
  Le crochet `.claude/hooks/garde_pipe.py` refuse ces commandes.
- « Réussi » exige : code 0 ET la ligne de bilan vue (« N passed »). Sinon écrire « non vérifié » et pourquoi.
- Une vidéo n'est « faite » que si elle a été sondée (`ffprobe` : durée, image, son) et regardée (planche
  d'images). Vérifier le moteur de voix réellement utilisé (`voice_engine` dans l'état de l'épisode).
- Fusion d'une PR seulement si tous les contrôles de la tête actuelle sont en `success` et les vérifications
  locales à 0.

## Sobriété — chaque session, sans exception
- Lire ciblé : Grep/Glob d'abord, puis Read avec `offset`/`limit`. Ne jamais relire un fichier déjà lu et inchangé.
- Ne jamais lire `runs/`, `vault/`, `assets/piper/` ni les bases `.sqlite`.
- Code minimal : le plus petit diff qui résout le problème ; pas d'abstraction, d'option ni de fichier « au cas où ».
- Carte : `toonomatic/pipeline.py` (chaîne d'un épisode), `ideas.py` (idées, tendances, anti-répétition),
  `writing/` (bureau d'écriture `desk.py`, contrôle `checker.py`, LLM), `voice/` (`tts.py` : fournisseurs de voix,
  `lipsync.py`), `animation/` (`puppet.py` 2D Cairo, Blender), `editing/` (montage, sous-titres), `director.py`
  (réalisateur automatique), `packaging.py` et `presence.py` (formats réseaux), `publish.py`, `social.py`
  (mesures), `command/` (serveur et PWA), `studio.py` ; configuration dans `config/`, séries dans `series/` ;
  tests dans `tests/`.
