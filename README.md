# Toon-o-Matic 🎬

Une usine headless qui produit chaque jour de **1 à 4 épisodes de 10 à 15 minutes** d'une web-série animée 3D, en **deux versions écrites séparément** : une en français (humour de rue, absurde, caustique) et une en anglais (deadpan britannique, ou rythme américain). Chaque version a ses voix, ses sous-titres incrustés, des sous-titres traduits dans l'autre langue (fichier séparé) et un **extrait vertical de 59 s ou moins** pour Shorts, TikTok et Reels.

```
[ Idée / Tendance ] → 1. Écriture (LLM)  → 2. Voix (TTS) → 3. Animation 3D (Blender headless)
                     → 4. Montage + sous-titres (FFmpeg) → QA / score de risque
                     → Revue humaine [Valider] [Refaire] [Rejeter] → Publication
                                 ↑                                        │
                                 └── leçons + KPI réinjectés dans les prompts
```

Chaque bloc utilise un **fournisseur interchangeable**. Le mode `mock` fait tourner toute la chaîne **sans GPU, sans Blender et sans clé API**. Il sert aux tests, à la CI et à valider l'installation.

## Démarrage rapide

```bash
pip install -e ".[dev]"            # Python ≥ 3.10 + ffmpeg dans le PATH
toonomatic produce --count 1 --minutes 1   # épisode de test (mock, environ 2 min)
toonomatic serve-review            # http://127.0.0.1:8765 → Valider / Refaire / Rejeter
toonomatic list
pytest -q
```

Production réelle (voir `deploy/prod.example.yaml`) :

```bash
pip install -e ".[claude,xtts,youtube]"
cp deploy/prod.example.yaml config/prod.yaml && cp deploy/.env.example .env
toonomatic -c config/prod.yaml produce --count 4
```

Pour changer de fournisseur ponctuellement : `TOONOMATIC_LLM=claude|ollama|mock`, `TOONOMATIC_TTS=xtts|bark|elevenlabs|mock` et `TOONOMATIC_RENDERER=blender|preview`.

## Les 4 blocs

| Bloc | Fournisseurs | Fichiers |
|---|---|---|
| **1. Écriture** | Claude (`claude-opus-5-5`, sortie JSON contrainte par schéma, prompt système mis en cache), Ollama (Llama, Mistral… en local), mock | `toonomatic/writing/`, `config/showrunners/*.md` |
| **2. Voix** | XTTS v2 (clonage multilingue), Bark (rires, soupirs, hésitations), ElevenLabs (cloud), mock | `toonomatic/voice/tts.py` |
| **3. Animation** | Blender ≥ 4.2 en `-b` (Eevee, ou Cycles CPU en repli), animatique FFmpeg (`preview`) | `toonomatic/animation/` |
| **4. Montage** | FFmpeg : assemblage, ducking de la musique, normalisation du volume (loudnorm −16 LUFS), sous-titres ASS stylisés, recadrage 9:16 | `toonomatic/editing/` |

### 1. Écriture : deux showrunners, pas une traduction
1. **Prémisse commune** : situation, distribution et découpage en environ 9 scènes pour 12 min (`episode.minutes_per_scene`). Elle fixe les décors et les personnages de chaque scène, ce qui permet de réutiliser les mêmes assets 3D dans les deux langues.
2. **Un agent par langue** écrit ses propres dialogues, scène par scène, avec un résumé glissant pour garder la cohérence sur 15 minutes. Les prompts sont dans `config/showrunners/` : `fr.md`, `en_uk.md` et `en_us.md`.
3. **Ajustement de la durée** : si l'estimation est sous `min_minutes`, la scène la plus courte est allongée. Si elle dépasse `max_minutes`, les scènes les plus bavardes sont resserrées, en gardant toujours la chute.
4. Le LLM ne peut choisir **que** parmi les actions et émotions déclarées dans `config/cast.yaml` (énumérations dans le schéma JSON). C'est ce qui rend la 3D pilotable par le texte.
5. La meilleure scène (auto-évaluée) est marquée `highlight` et devient l'extrait vertical.

### 2. Voix
Chaque réplique est synthétisée avec la voix du personnage dans la langue voulue. Les balises `[rire]`, `[soupir]` et `[laughs]` sont interprétées par Bark (et par ElevenLabs v3), et retirées pour les autres moteurs. Les répliques sont ensuite posées sur une timeline avec des pauses qui dépendent de l'émotion. On en tire **l'enveloppe d'amplitude image par image**, qui pilote le lip-sync.

### 3. Animation 3D (`animation/blender_scene.py`)
Une shot list JSON par scène donne le décor, les acteurs, les performances (action, émotion, enveloppe) et un **découpage caméra automatique** (plan large, deux-plans ou gros plan sur le locuteur, avec fusion des plans trop courts). Le script bpy :
- importe `assets/library/sets/<décor>.blend` (collection `SET`) et `characters/<id>.blend` ; s'ils manquent, il génère des **avatars et décors placeholder** ;
- pose les **Actions NLA** pré-enregistrées (`talk_hands`, `angry`, `facepalm`…), les shape keys d'émotion `emo_*` et le lip-sync `mouth_open` ;
- coupe entre les caméras avec des marqueurs, puis rend une **séquence PNG** que FFmpeg encode. Ce format marche avec Blender 4.x et 5.x, et un rendu interrompu reprend là où il s'est arrêté.
- Les scènes sont rendues **en parallèle** (`parallel_jobs`, une par GPU).
- `animate_on_twos: true` active l'animation « en 2 » : un style cartoon assumé, et **deux fois moins d'images à rendre**.

Conventions pour les modeleurs : voir `assets/library/*/README.md`.

### 4. Montage et localisation
Pour chaque langue, le montage produit :
- l'épisode 16:9 avec sous-titres stylisés incrustés (liseré coloré par personnage) ;
- une version sans sous-titres incrustés ;
- `subs.<langue>.srt` et le SRT traduit (`subs.en.srt` pour la version FR, et inversement) ;
- l'extrait 9:16.

## Revue humaine et contrôle du risque
`qa.py` calcule un score de risque à partir de plusieurs contrôles :
- durée hors plage ;
- images noires (`blackdetect`) ;
- répliques sans audio ;
- revue de contenu par LLM : personnes réelles, discours haineux, règles des plateformes.

Si le risque atteint `qa.min_risk_for_human`, la **revue humaine est obligatoire**. `qa.always_human_review: true` (par défaut) l'impose pour tous les épisodes : c'est recommandé tant que la confiance n'est pas établie.

| Canal | Commande |
|---|---|
| Interface web locale (vidéos, rapport QA, motif, choix de l'étape à refaire) | `toonomatic serve-review` |
| Bot Telegram (boutons inline ; répondre au message pour donner un motif) | `review.channel: telegram` + `toonomatic telegram-poll` |
| CLI | `toonomatic review redo <id> --reason "..." --from script` |

**Refaire** ne relance que ce qui est nécessaire : `premise`, `script`, `voice` ou `render`. Le motif saisi est ajouté à `data/lessons.md` et **réinjecté dans les prompts** des épisodes suivants. **Valider** déclenche la publication. Par défaut `publish.dry_run: true` : rien n'est envoyé, seul un `publish_manifest.json` est écrit.

## Orchestration
- **Étapes idempotentes et reprenables**, suivies dans SQLite (`runs/toonomatic.sqlite`). `toonomatic resume` reprend les épisodes interrompus, en échec (jusqu'à 3 tentatives) ou à refaire.
- `toonomatic daily` reprend la file puis lance la fournée du jour (`cadence.episodes_per_day`).
- Planification au choix : `deploy/crontab`, `deploy/toonomatic.timer` (systemd) ou `deploy/n8n-workflow.json` (n8n, nœud *Execute Command*).
- **Déclencheur** : banque de situations `data/situations.yaml`, sans répétition sur 60 jours et pondérée par les KPI. On peut ajouter des **flux RSS de tendances** (`ideas.trend_feeds`), au plus une tendance par fournée, avec interdiction de mettre en scène des personnes réelles.
- **Boucle d'apprentissage** : `toonomatic kpi-import stats.csv` (colonnes `episode_id,lang,views,avg_view_pct,…`). Les situations qui retiennent le mieux l'audience sont ensuite tirées plus souvent.

## Budget de calcul (ordre de grandeur, à mesurer sur votre matériel)
| | Calcul | Par jour (4 épisodes de 12 min, FR + EN) |
|---|---|---|
| Images à rendre | 12 × 60 × 24 = 17 280 par langue | ≈ 138 000 (≈ 69 000 « en 2 ») |
| Débit nécessaire | réparti sur 24 h | ≈ 1,6 image/s (0,8 « en 2 ») |
| Voix | 24 min d'audio par épisode | XTTS sur GPU : plus rapide que le temps réel |
| LLM | environ 25 appels par épisode (prémisse, 2 × scènes, titres, traductions, revue) | — |

Le rendu 3D est le goulot d'étranglement. Les leviers, dans l'ordre :
1. animation en 2 ;
2. décors simples et éclairage précalculé dans les `.blend` ;
3. plusieurs GPU avec `parallel_jobs` ;
4. rendu en 720p puis upscale.

Le renderer `preview` sert à **valider le script et les voix avant** de lancer le rendu 3D.

## Points d'attention
- **Voix clonées** : uniquement avec le consentement écrit des comédiens dont on clone la voix (`assets/voices/README.md`).
- **Contenu synthétique** : l'upload YouTube déclare `containsSyntheticMedia`. TikTok et Instagram exigent aussi une mention IA.
- **TikTok et Instagram** : leurs API de publication demandent une application validée par la plateforme. `publish.py` lève une erreur explicite ; branchez un connecteur (n8n, Make, Metricool…) une fois les accès obtenus.
- L'extrait vertical est un **recadrage central** du 16:9. Un recadrage qui suit le locuteur est une amélioration possible.

## Arborescence
```
config/        default.yaml (tous les réglages), cast.yaml (acteurs, voix, décors, actions), showrunners/
data/          situations.yaml (banque d'idées), lessons.md (mémoire des revues)
toonomatic/    writing/ voice/ animation/ editing/ qa.py review.py publish.py pipeline.py cli.py
deploy/        cron, systemd, n8n, configuration de prod d'exemple
runs/<id>/     premise.json, <lang>/script*.json, audio/, render/, *.mp4, *.srt, qa_report.json
```
