---
name: deglingues-jour
description: Séquence quotidienne des Déglingués (une minute, FR et EN) - choisir un thème d'actualité, écrire le script, le faire fabriquer par l'usine, contrôler, remettre au propriétaire pour validation. À utiliser quand on demande « la séquence du jour » ou « exécute deglingues-jour ».
---

# Les Déglingués — séquence du jour

Rien n'est publié ici. La séquence est remise au propriétaire, qui valide ou refuse.

## 1. Préparer la machine (une fois par conteneur)
```bash
pip install -q -e ".[pocket,piper,vector]" pytest
sh deploy/fetch_piper_voices.sh assets/piper
```

## 2. Choisir le thème
- Lire les titres du jour : `https://news.google.com/rss?hl=fr&gl=FR&ceid=FR:fr`, `https://www.lemonde.fr/rss/une.xml`,
  `https://feeds.bbci.co.uk/news/world/rss.xml`.
- En tirer UN thème général (pénurie, réforme, panne, sommet, météo, prix, examen…). Jamais de nom propre, de parti,
  de marque, de drame humain, de victime ni d'affaire judiciaire : relire `series/les-deglingues/series.yaml` (règles).
- Ne pas reprendre un thème déjà dans `series/les-deglingues/situations.yaml`. Si l'actualité du jour ne donne
  rien d'utilisable, prendre un thème de la banque qui n'a pas encore servi.
- Ajouter le thème à la banque : `{id, situation, cast, sets}` (décors permis : `rue`, `salon`).

## 3. Écrire
- Obtenir la consigne exacte : `python3 .claude/skills/deglingues-jour/sequence.py brief <id>`.
- Écrire le JSON demandé (4 scènes, FR et EN écrits séparément, une chute par scène, le gag récurrent trois fois)
  dans un fichier, hors du dépôt.

## 4. Fabriquer et contrôler
```bash
.claude/bin/journal sequence python3 .claude/skills/deglingues-jour/sequence.py produire <id> <script.json> <dossier de sortie>
```
« Réussi » exige : code 0, la ligne `statut review`, `voice_engine` différent de `MockTTS`, puis `ffprobe` (durée
entre 45 et 100 s, image et son présents) et une planche de 6 images regardée. Sinon : « non vérifié » et pourquoi.

## 5. Remettre
Envoyer au propriétaire la vidéo FR, l'extrait vertical FR et la planche, avec le thème choisi, les notes du
réalisateur automatique et ce qui reste imparfait. Ne rien publier, ne rien programmer.
Commiter seulement la ligne ajoutée à `situations.yaml` (jamais les vidéos ni `runs/`).
