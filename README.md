# Toon-o-Matic 🎬

Une usine headless qui produit chaque jour de **1 à 4 épisodes de 10 à 15 minutes** d'une web-série animée 3D, en **deux versions écrites séparément** : une en français (humour de rue, absurde, caustique) et une en anglais (deadpan britannique, ou rythme américain). Chaque version a ses voix, ses sous-titres incrustés, des sous-titres traduits dans l'autre langue (fichier séparé) et un **extrait vertical de 59 s ou moins** pour Shorts, TikTok et Reels.

```
[ Idée / Tendance ] → 1. Écriture (LLM)  → 2. Voix (TTS) → 3. Animation 3D (Blender headless)
                     → 4. Montage + sous-titres (FFmpeg) → QA / score de risque
                     → Revue humaine [Valider] [Refaire] [Rejeter] → Publication
                                 ↑                                        │
                                 └── leçons + KPI réinjectés dans les prompts
```

Le système est organisé comme un petit studio : une **série** est un dossier autonome (`series/<slug>/`) qui contient sa bible, ses personnages persistants, son style visuel et sa mémoire. On peut lancer plusieurs séries sur la même usine : `toonomatic series-new ma-serie`, puis `toonomatic -s series/ma-serie produce`.

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
pip install -e ".[claude,chatterbox,youtube]"   # + binaire rhubarb dans le PATH
cp deploy/prod.example.yaml config/prod.yaml && cp deploy/.env.example .env
toonomatic -c config/prod.yaml produce --count 4
```

Pour changer de fournisseur ponctuellement : `TOONOMATIC_LLM=claude|ollama|mock`, `TOONOMATIC_TTS=chatterbox|xtts|bark|http|elevenlabs|mock` et `TOONOMATIC_RENDERER=blender|preview`.

## Les 4 blocs

| Bloc | Fournisseurs | Fichiers |
|---|---|---|
| **1. Écriture** | Claude (`claude-opus-5-5`, sortie JSON contrainte par schéma, prompt système mis en cache), Ollama (Llama, Mistral… en local), mock | `toonomatic/writing/`, `config/showrunners/*.md` |
| **2. Voix** | Chatterbox multilingue (curseur d'« exagération » émotionnelle), XTTS v2, Bark (rires, soupirs), **n'importe quel serveur auto-hébergé** via `http` (Fish Speech / OpenAudio, F5-TTS, CosyVoice, Zonos…), ElevenLabs en option, mock | `toonomatic/voice/tts.py` |
| **2b. Lip-sync** | Rhubarb Lip Sync (visèmes, reconnaissance phonétique indépendante de la langue → FR et EN), repli sur l'amplitude du son | `toonomatic/voice/lipsync.py` |
| **3. Animation** | Blender ≥ 4.2 en `-b` (Eevee, ou Cycles CPU en repli), animatique FFmpeg (`preview`) | `toonomatic/animation/` |
| **4. Montage** | FFmpeg : assemblage, ducking de la musique, normalisation du volume (loudnorm −16 LUFS), sous-titres ASS stylisés, recadrage 9:16 | `toonomatic/editing/` |

### 0. La série : bible, personnages persistants, mémoire
- `series/<slug>/series.yaml` : titre et pitch FR/EN, univers, ton, **règles de continuité intangibles**, style visuel, format d'épisode.
- `series/<slug>/cast.yaml` : chaque personnage a une identité **qui ne change pas d'un épisode à l'autre** : biographie FR/EN, traits de caractère, répliques fétiches, description d'apparence, planche de référence (model sheet), rig 3D, voix par langue et par moteur. On y trouve aussi les décors, la bibliothèque d'animations et les visèmes.
- `series/<slug>/memory/` : après chaque épisode validé, un « scripte » LLM extrait au plus 5 **faits de continuité** (running gag né, objet cassé, relation qui évolue…) et tient le journal des épisodes diffusés. Tout est réinjecté dans les prompts : les personnages se souviennent et la série construit sa mythologie (`toonomatic series-memory`).

### 1. Écriture : deux showrunners, pas une traduction
1. **Prémisse commune** : situation, distribution et découpage en environ 9 scènes pour 12 min (`episode.minutes_per_scene`). Elle fixe les décors et les personnages de chaque scène, ce qui permet de réutiliser les mêmes assets 3D dans les deux langues.
2. **Un agent par langue** écrit ses propres dialogues, scène par scène, avec un résumé glissant pour garder la cohérence sur 15 minutes. Les prompts sont dans `config/showrunners/` : `fr.md`, `en_uk.md` et `en_us.md`.
3. **Ajustement de la durée** : si l'estimation est sous `min_minutes`, la scène la plus courte est allongée. Si elle dépasse `max_minutes`, les scènes les plus bavardes sont resserrées, en gardant toujours la chute.
4. Le LLM ne peut choisir **que** parmi les actions et émotions déclarées dans `config/cast.yaml` (énumérations dans le schéma JSON). C'est ce qui rend la 3D pilotable par le texte.
5. La meilleure scène (auto-évaluée) est marquée `highlight` et devient l'extrait vertical.

### 2. Voix
Chaque réplique est synthétisée avec la voix du personnage dans la langue voulue. Les balises `[rire]`, `[soupir]` et `[laughs]` sont interprétées par Bark (et par ElevenLabs v3), et retirées pour les autres moteurs. Les répliques sont ensuite posées sur une timeline avec des pauses qui dépendent de l'émotion. On en tire **l'enveloppe d'amplitude image par image**, qui pilote le lip-sync.

### Style visuel (`styles/*.yaml`)
L'identité artistique est un **préréglage** choisi dans la bible de série et ajustable via `style_overrides`. La série d'exemple utilise **`caricature`**, construit d'après vos captures de référence :
- personnages caricaturaux : grosse tête, long cou, nez marqué, texture peinte, **accessoires signatures** (lunettes de soleil, moustache, casquette…) décrits dans `cast.yaml` (`signature`) ;
- ombrage doux et peu contrasté, **sans contour**, couleurs franches, ciel bleu vif ;
- **décors en plaques 2D** (voir ci-dessous) ;
- mise en scène **face à face** : en deux-plans, les personnages se tournent l'un vers l'autre de trois-quarts ou de profil ; les gros plans restent frontaux.

Autres préréglages :
- `toon` : cel-shading à paliers, ombres colorées, liseré lumineux, contour encré ;
- `soft3d` : rendu doux type long-métrage (AgX, profondeur de champ, flou de mouvement) ;
- `clay` : pâte à modeler, matières mates, à combiner avec l'animation « en 2 ».

Chaque préréglage règle la palette, les trois lumières, les focales et la profondeur de champ par type de plan, la gestion des couleurs, et le **jeu** (fréquence des clignements, amplitude de respiration, anticipation des gestes).

### Décors en plaques 2D (`animation/plates.py`)
Comme dans les références, le décor est une **image** et seuls les personnages sont en 3D. C'est le choix le plus rentable pour une cadence industrielle : un nouveau décor coûte une image, pas une modélisation.
- Blender rend les personnages **sur fond transparent**. FFmpeg les incruste sur la plaque, **recadrée par plan** : image entière en plan large, léger zoom en deux-plans, zoom fort centré sur le personnage en gros plan (le fond « grossit » comme dans les références).
- `toonomatic plates` génère les plaques manquantes à partir de `plate_prompt` (dans `cast.yaml`) et du `plate_style` du préréglage, via **votre serveur d'images** (`plates.provider: http`). Le format par défaut est celui de l'API Automatic1111 / Forge ; une passerelle ComfyUI, FLUX ou SDXL auto-hébergée fonctionne aussi. Les plaques déposées à la main dans `series/<slug>/plates/` ne sont jamais écrasées (sauf avec `--force`).
- `backgrounds: auto` utilise la plaque quand il n'y a pas de décor `.blend`. C'est utile pour un intérieur de voiture en vraie 3D, et tout le reste en plaques.

### 3. Animation 3D (`animation/blender_scene.py`)
Une shot list JSON par scène donne le décor, les acteurs, les performances (action, émotion, enveloppe) et un **découpage caméra automatique** (plan large, deux-plans ou gros plan sur le locuteur, avec fusion des plans trop courts). Le script bpy :
- importe `assets/library/sets/<décor>.blend` (collection `SET`) et `characters/<id>.blend` ; s'ils manquent, il génère des **avatars et décors placeholder** ;
- pose les **Actions NLA** pré-enregistrées (`talk_hands`, `angry`, `facepalm`…), en **alternant les variantes** (`talk.001`, `talk.002`…) et en démarrant le geste quelques images avant la parole (anticipation) ;
- joue les expressions `emo_*` et le **lip-sync par visèmes** (`viseme_A`…`viseme_X`, transitions d'une image), avec repli sur `mouth_open` ;
- ajoute le **jeu naturel** qui évite l'effet mannequin : clignements irréguliers, respiration, **regards** (les auditeurs tournent la tête vers celui qui parle, avec un léger décalage chacun ; le locuteur regarde son interlocuteur) via une contrainte *Damped Track* sur l'os `head` ;
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

### 5. Kit de publication YouTube et TikTok (`packaging.py`)
Pour chaque langue, dans `runs/<id>/package/<langue>/` :
- `youtube.json` : titre (avec le nom de la série), description avec **chapitres horodatés** conformes aux règles YouTube (premier à 0:00, au moins 3, chacun d'au moins 10 s), tags, langue audio, mention de contenu synthétique et de l'usage d'IA ;
- `thumbnail.jpg` : miniature 1280×720 tirée de la meilleure scène, titre de 4 mots maximum dont la taille s'adapte à la largeur ;
- `tiktok.json` et `tiktok_cover.jpg` : légende (2 200 caractères maximum) avec hashtags, mention « contenu généré par IA », couverture verticale.

L'upload YouTube applique aussi la miniature.

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
- **Inspiration visuelle** : le style d'une vidéo existante peut inspirer un préréglage (palette, cel-shading, cadrages), mais personnages, univers et designs restent originaux.
- **Contenu synthétique** : l'upload YouTube déclare `containsSyntheticMedia`. TikTok et Instagram exigent aussi une mention IA.
- **TikTok et Instagram** : leurs API de publication demandent une application validée par la plateforme. `publish.py` lève une erreur explicite ; branchez un connecteur (n8n, Make, Metricool…) une fois les accès obtenus.
- L'extrait vertical est un **recadrage central** du 16:9. Un recadrage qui suit le locuteur est une amélioration possible.

## Boîte à outils : au-delà des outils que tout le monde utilise
Les briques intégrées sont marquées ✅. Les autres sont des **pistes** à brancher sur les interfaces existantes (fournisseur TTS `http`, assets `.blend`, actions NLA).

| Besoin | Open source / peu connu | Commercial spécialisé |
|---|---|---|
| Scénario | ✅ Ollama (Mistral, Llama, Qwen en local) | ✅ Claude |
| Voix | ✅ Chatterbox, ✅ XTTS v2, ✅ Bark, Fish Speech / OpenAudio, F5-TTS, CosyVoice, Zonos (via ✅ `http`) | ElevenLabs (optionnel) |
| Lip-sync | ✅ Rhubarb Lip Sync | — |
| Création des personnages 3D | image → 3D : TRELLIS, Hunyuan3D-2 ; rigging : Rigify (Blender) | AccuRIG ; Character Creator / Headshot (Reallusion), adapté aux caricatures à texture peinte |
| Mouvements | capture depuis une simple vidéo : FreeMoCap ; animation générée à partir d'un texte : modèles de recherche (sortie BVH → Actions NLA) | Move.ai, Rokoko |
| Décors 2D | ✅ serveur d'images auto-hébergé (Automatic1111 / Forge, ComfyUI, FLUX, SDXL) via `http` | — |
| Rendu | ✅ Blender Eevee / Cycles en headless | — |
| Montage | ✅ FFmpeg, libass | — |
| Orchestration | ✅ cron / systemd, n8n | — |

**Créer un personnage** : concept (dessin, ou image générée puis retouchée) → planche de référence → modèle 3D (image → 3D, puis nettoyage dans Blender) → rig, shape keys `viseme_*`, `emo_*`, `blink` → bibliothèque d'Actions → `.blend` dans `assets/library/characters/` → fiche dans `cast.yaml`. Une fois fait, le personnage est **réutilisé à l'identique** dans tous les épisodes.

## De l'usine au studio : la feuille de route
1. **Pilote** : une série, trois personnages, revue humaine systématique, 1 épisode par jour. Mesurer la rétention (`kpi-import`).
2. **Identité** : figer le style, le générique, la musique, les répliques fétiches. Les règles de la bible empêchent la dérive.
3. **Cadence** : 2 à 4 épisodes par jour quand le taux de « Valider » au premier passage est élevé. Extraits verticaux quotidiens pour attirer l'audience vers les épisodes longs.
4. **Catalogue** : une deuxième série (`series-new`) qui réutilise décors et rigs ; la mémoire de chaque série reste séparée.
5. **Revenus** : programmes de monétisation des plateformes, puis licences des personnages, chaînes thématiques par langue, épisodes sponsorisés. Les droits sont clairs, car tout est original (personnages, univers, voix sous contrat).

## Arborescence
```
config/        default.yaml (tous les réglages), showrunners/ (prompts d'humour par langue)
series/<slug>/ series.yaml (bible), cast.yaml (personnages, voix, décors, actions), plates/ (décors 2D), memory/ (continuité)
styles/        caricature.yaml, toon.yaml, soft3d.yaml, clay.yaml (identité visuelle)
data/          situations.yaml (banque d'idées), lessons.md (mémoire des revues)
toonomatic/    writing/ voice/ animation/ editing/ qa.py review.py publish.py pipeline.py cli.py
deploy/        cron, systemd, n8n, configuration de prod d'exemple
runs/<id>/     premise.json, <lang>/script*.json, audio/, render/, *.mp4, *.srt, package/, qa_report.json
```
