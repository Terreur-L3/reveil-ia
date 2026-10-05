# Réveil IA — podcast du matin + journal vocal

Deux choses qui s'alimentent l'une l'autre :

1. **Le journal vocal** : tu parles (ou tu écris) à ton bot Telegram, de 30 secondes à plus d'une heure.
   Il transcrit, retient ce qui compte (anniversaires, échéances, projets, goûts…) et te pose des questions
   quand tu n'as plus d'inspiration (bouton **💡 Inspi**).
2. **Le podcast du matin** (~2 min, ton positif, jamais dramatique) : les actus qui comptent et qui t'intéressent,
   puis tes infos perso (anniversaires, rappels). Il arrive chaque matin dans la conversation Telegram.

```
TOI (Telegram) ── vocal / texte / « 💡 Inspi » ──►  JOURNAL VOCAL   (GitHub Actions, toutes les 30 min)
                                                     Groq Whisper : transcription
                                                     Groq (IA)    : extraction des infos
                                                     └─► memoire/  (fichiers lisibles, dans ton dépôt privé)
                  ◄── « ✅ Noté : 3 infos retenues… » + question Inspi (texte + voix)

                                                     PODCAST DU MATIN  (GitHub Actions, ~5 h 30)
 flux RSS ─► Gemini note les actus ─► choix par le programme ─► Gemini écrit le texte
                       ▲ tes centres d'intérêt APPRIS dans memoire/      │
 infos perso (anniversaires, rappels) : insérées par le programme, jamais envoyées à une IA
                                                     ─► Edge TTS (voix) ─► mp3
                  ◄── le mp3 arrive dans Telegram  +  branche « podcast » (latest.mp3, latest.txt, choix.txt)
```

**Qui choisit tes actus ?** Au début, une liste *provisoire* choisie par Claude (fichier `moi.yaml`). Dès que ton
journal a appris au moins **3** de tes centres d'intérêt, cette liste est abandonnée toute seule et c'est TA mémoire
qui décide. Le fichier `choix.txt` (branche `podcast`) dit toujours laquelle est utilisée et pourquoi chaque actu
est passée ou non.

---

## Mise en route (dans l'ordre)

Tu as déjà fait les étapes de la v1 (dépôt privé `Terreur-L3/reveil-ia`, clé Gemini, secret `GEMINI_API_KEY`).
Reste à ajouter le journal vocal.

### Étape 0 — Mettre le dépôt à jour (Termux)

```
cd ~/storage/downloads/reveil-ia && git pull --rebase     # 1. récupère d'abord ce que le robot a pu écrire
cd ..  && unzip -o reveil-ia-v3.zip                       # 2. dézippe par-dessus (ton .git n'est pas touché)
cd reveil-ia && git add -A && git commit -m "v3 : journal vocal" && git push     # 3. envoie
```

Fais toujours le `git pull --rebase` **avant** de dézipper : le robot enregistre ta mémoire dans le dépôt, donc le
dépôt change même quand tu n'y touches pas. (`unzip` introuvable ? `pkg install unzip`.)

Cette première mise à jour remplace `config.yaml` et `moi.yaml` par les nouvelles versions : si tu y avais mis ton
prénom ou changé des flux, remets-les ensuite. **Pour les mises à jour suivantes**, ajoute ceci à la commande `unzip`
pour ne jamais écraser tes anniversaires, tes rappels ni tes préférences :
`-x 'reveil-ia/memoire/*' 'reveil-ia/moi.yaml'`.

### Étape 1 — Clé Groq (gratuite)

1. Va sur <https://console.groq.com>, crée un compte, puis **API Keys → Create API Key**. Copie la clé (`gsk_…`).
2. Recommandé : **Settings → Data Controls → Zero Data Retention**. Par défaut, Groq ne conserve déjà pas le contenu
   des requêtes (sauf pour dépanner une erreur ou enquêter sur un abus, jusqu'à 30 jours) ; cette option supprime
   même ces exceptions. Son contrat (art. 4.2) interdit aussi d'entraîner des modèles avec tes entrées et sorties.

### Étape 2 — Ton bot Telegram

1. Dans Telegram, ouvre **@BotFather** → `/newbot` → choisis un nom, puis un identifiant qui finit par `bot`.
   BotFather te donne un **jeton** (`123456:ABC…`). Ne le montre à personne.
2. Ouvre ton nouveau bot, appuie sur **Démarrer** (ça envoie `/start`) et écris-lui « salut ».
3. Trouve ton identifiant de conversation : dans le navigateur du téléphone, ouvre
   `https://api.telegram.org/bot<TON_JETON>/getUpdates` (remplace `<TON_JETON>`, en gardant le mot `bot` devant)
   et repère `"chat":{"id":123456789,…` : ce nombre est ton **TELEGRAM_CHAT_ID**.
   (Sans navigateur : `python -m src.telegram` l'affiche, si `TELEGRAM_BOT_TOKEN` est dans `.env`.)

Seule TA conversation est écoutée : si quelqu'un d'autre écrit à ton bot, il est ignoré sans réponse.

### Étape 3 — Les secrets GitHub

Dépôt → **Settings → Secrets and variables → Actions → New repository secret**
(<https://github.com/Terreur-L3/reveil-ia/settings/secrets/actions>). Crée :

| Nom | Valeur |
|---|---|
| `GROQ_API_KEY` | la clé de l'étape 1 |
| `TELEGRAM_BOT_TOKEN` | le jeton de BotFather |
| `TELEGRAM_CHAT_ID` | le nombre de l'étape 2 |
| `GEMINI_API_KEY` | déjà là (v1) |

### Étape 4 — Premier lancement du journal

Onglet **Actions → Journal vocal → Run workflow**
(<https://github.com/Terreur-L3/reveil-ia/actions>). Au bout d'une minute, le bot t'envoie un message de bienvenue
avec deux boutons en bas : **💡 Inspi** et **🧠 Mémoire**. Rien ? Envoie-lui `/start` dans Telegram, relance, et au besoin lis la section « Si ça coince ».

### Étape 5 — Parle-lui

1. Envoie un vocal (ou un texte) : « Je m'appelle… j'adore le cinéma d'animation, mon ami Karim fête son
   anniversaire le 12 mars… ».
2. Relance **Run workflow** (ou attends le prochain passage, voir « Délais »). Le bot répond :
   `✅ Noté (45 s de vocal)` et liste ce qu'il a retenu (anniversaires 🎂, rappels 🔔, centres d'intérêt ⭐).
3. Appuie sur **💡 Inspi** : au prochain passage, il te pose une question (écrite **et** dite à voix haute).
   Réponds à la voix ou par écrit : ta réponse est rangée dans la mémoire comme le reste.

### Étape 6 — Essaie le podcast

**Actions → Podcast du matin → Run workflow.** Le mp3 arrive dans Telegram (et sur la branche `podcast`).
Ensuite il se génère tout seul chaque matin.

### Étape 7 — Le téléphone (à faire ensemble)

Il reste à brancher la sonnerie de ton téléphone : alarme d'environ 10 secondes, puis lecture automatique du podcast.
Pour l'instant, le podcast t'attend dans Telegram à ton réveil.

---

## Au quotidien

| Tu fais | Il se passe |
|---|---|
| Vocal de 30 s à ~1 h 30 (ou texte) | Transcrit, rangé dans `memoire/`, accusé « ✅ Noté » avec ce qui a été retenu |
| **💡 Inspi** (ou `/inspi`) | Une question adaptée à ce qu'il sait de toi, écrite + vocale. Ta réponse enrichit la mémoire |
| **🧠 Mémoire** (ou `/memoire`) | Ce qu'il a appris, tes centres d'intérêt, et si tes actus viennent encore de la liste provisoire |
| `/aide` | Rappel des fonctions |
| Rien pendant la journée | Entre 19 h et 23 h : une question du soir (une fois par jour, jamais deux à moins de 4 h d'écart). Réglable : `heure_question` dans `config.yaml` |
| « Je ne veux plus entendre parler de… » | Le sujet est noté comme à éviter dans les actus |

Pour répondre à une question précise, utilise « Répondre » sur le message-question : sinon la réponse est rattachée
à la dernière question restée sans réponse (moins de 36 h).

## Comment il apprend tes goûts

- Un sujet devient **centre d'intérêt retenu** s'il est cité **deux fois**, ou **une fois avec enthousiasme**.
  À 3 centres d'intérêt, le podcast se met à utiliser ta mémoire à la place de la liste provisoire.
- Les sujets **sensibles** (santé, argent, politique, religion, sexualité, conflits personnels…) restent dans ta
  mémoire mais **ne servent jamais** à choisir les actus ni ne partent chez Gemini.
- Tu peux toujours forcer les choses dans `moi.yaml` : `epingles` (toujours présents), `toujours_important`,
  `a_ignorer`. Ce que tu écris à la main gagne sur ce que le robot apprend, comme `memoire/anniversaires.yaml` et
  `memoire/rappels.yaml` (le robot écrit dans `*_auto.yaml`, jamais dans tes fichiers).
- Une erreur dans la mémoire ? Le plus simple : dis-le au bot (« en fait, l'anniversaire de Karim est le 14 mars » :
  la dernière version gagne). Pour supprimer ou changer à la main : modifie le fichier dans `memoire/` sur GitHub
  (bouton ✏️) — `interets.yaml`, `anniversaires_auto.yaml`, `rappels_auto.yaml`, `faits.jsonl`. Le robot relit ces
  fichiers à chaque passage (seuls les commentaires du YAML sont réécrits). Évite d'éditer pile au moment d'un passage
  (voir « Si ça coince »).

## Confidentialité : qui voit quoi

| Service | Ce qu'il voit | Pourquoi / garde-fous |
|---|---|---|
| **Groq** | Tes vocaux (le temps de la transcription) et le texte de ton journal | Seul fournisseur IA qui voit ta vie privée. Contrat : pas d'entraînement sur tes données, pas de conservation par défaut ; option « Zero Data Retention » |
| **Gemini** (Google, offre gratuite) | Des titres d'actus **publics** et les libellés de tes centres d'intérêt **non sensibles** (ex. « Houdini ») | L'offre gratuite peut servir à améliorer les modèles de Google : jamais ton journal, jamais un nom, une date perso ou un sujet sensible |
| **Microsoft Edge TTS** (la voix) | Le texte lu : le podcast, **y compris tes lignes perso** (« demain, Karim fête ses 21 ans »), et les questions Inspi | Service non officiel et gratuit de Microsoft. Si ça te gêne : `question_vocale: false` (section `journal`) et `max_lignes: 0` (section `perso`) dans `config.yaml` — ou attends la voix locale (feuille de route) |
| **Telegram** | Tes messages, tes vocaux, le podcast | Les conversations avec un bot ne sont pas chiffrées de bout en bout |
| **GitHub** (dépôt **privé**) | `memoire/` contient le **texte complet** de ce que tu dis | **Ne passe jamais le dépôt en public.** L'audio n'est jamais conservé, seulement le texte |

Le jeton Telegram et les clés n'apparaissent jamais dans les logs. Le contenu de tes messages non plus.

## Délais, limites et quotas (tout est gratuit, avec ces plafonds)

- **Délai** : le journal est lu toutes les 30 minutes environ (GitHub peut retarder ou sauter un passage quand il est
  chargé : compte « dans l'heure »). Pour une réponse immédiate : *Actions → Journal vocal → Run workflow*
  (case « Poser une question tout de suite » pour déclencher un Inspi).
- **Taille** : Telegram interdit aux bots de télécharger plus de **20 Mo** par fichier (souvent ~1 h 30 de vocal).
  Plus long : envoie en deux fois ; le bot te prévient si un fichier est trop gros.
- **Transcription** : Groq autorise 7 200 s d'audio par heure et 28 800 s par jour (offre gratuite). Un vocal de 2 h
  est donc transcrit sur plusieurs passages ; la progression est gardée entre deux passages.
- **Minutes GitHub** : un dépôt privé a 2 000 min/mois gratuites. Compte ~1 000 à 1 400 min/mois pour le journal +
  le podcast. Vérifie dans *Settings → Billing and plans → Usage*. Trop près du plafond ? Dans `journal.yml`, passe
  `"7,37 5-21 * * *"` à `"7 5-21 * * *"` (une fois par heure).
- Les offres gratuites changent : Groq (<https://console.groq.com/docs/rate-limits>) et Google peuvent modifier leurs
  quotas. Les listes de modèles sont dans `config.yaml` (section `llm`) et modifiables.

## Si ça coince

| Symptôme | Que faire |
|---|---|
| Le bot ne répond jamais | *Actions → Journal vocal →* dernier passage → lis le message en rouge. « Il manque … » = un secret absent (étape 3) |
| Passage en échec avec « Telegram refuse le token » | Le secret `TELEGRAM_BOT_TOKEN` est faux : recopie le jeton de BotFather |
| Passage en échec avec « 409 » / « webhook » | Un webhook est actif sur ton bot. Ouvre `https://api.telegram.org/bot<TON_JETON>/deleteWebhook`, puis relance |
| Rien ne se lance tout seul | GitHub n'exécute les tâches planifiées que sur la branche principale ; en attendant, utilise *Run workflow* |
| « Impossible d'enregistrer la mémoire (conflit ?) » | Tu as modifié sur GitHub un fichier de `memoire/` au même instant. Rien n'est perdu : tes messages Telegram sont relus au passage suivant |
| Le podcast est très court, avec « je n'ai pas pu préparer les actus » | C'est le **podcast de secours** (bonjour + anniversaires/rappels). Lis `choix.txt` sur la branche `podcast` : la raison y est écrite (flux en panne, IA indisponible) |
| Un flux d'actus est « EN ÉCHEC » dans `choix.txt` | Supprime sa ligne dans `config.yaml` (section `actu`) s'il l'est depuis plusieurs jours |
| Le vocal est « abandonné » | Format ou fichier illisible : renvoie-le. Après 4 essais, le bot te le dit |
| `git push` refusé dans Termux | Le robot a écrit entre-temps : `git pull --rebase`, puis `git push` |
| `git push` refusé avec « without `workflow` scope » | Le jeton GitHub de Termux n'a pas le droit de modifier les workflows : crée-en un avec la permission « workflow » (jeton classique) ou « Workflows : lecture et écriture » (jeton fin) |

## Essais en local (facultatif, tout tourne déjà sur GitHub)

```
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                      # puis colle tes clés dedans
python -m src.main --no-audio             # texte du podcast + choix des actus, sans audio ni envoi
python -m src.journal --no-send           # une passe du journal, sans rien écrire dans Telegram
python -m unittest discover -s tests -t . # tests hors ligne (faux Telegram / Groq / Gemini / flux RSS)
```

Pour les longs vocaux, `ffmpeg` est nécessaire : GitHub l'installe tout seul quand il en a besoin
(en local : `pkg install ffmpeg` sous Termux, `apt install ffmpeg` sous Linux).

## Arborescence

```
config.yaml                    réglages : IA, tri des actus, flux RSS, voix, journal, heure de la question du soir
moi.yaml                       toi pour le podcast : liste provisoire, épingles, à ignorer (gagne sur la mémoire apprise)
memoire/                       anniversaires.yaml, rappels.yaml (à la main) + tout ce que le journal apprend :
  journal/…                    ce que tu as dit, par jour (lisible sur GitHub)
  faits.jsonl, interets.yaml, anniversaires_auto.yaml, rappels_auto.yaml, questions.jsonl, _etat/
src/main.py                    podcast : orchestre actus → tri → texte → voix → Telegram (+ podcast de secours)
src/news.py, triage.py         récupère les actus, les note, puis le programme choisit (seuils, plafonds, ton)
src/script_writer.py, tts.py   écrit le texte parlé, le transforme en mp3
src/perso.py                   anniversaires et rappels dits par le programme (jamais par une IA)
src/journal.py                 une passe du journal : Telegram → transcription → mémoire → questions
src/telegram.py, audio.py, stt.py, extract.py, inspi.py, memoire.py, pacer.py   les briques du journal
src/llm.py                     Gemini / Groq avec repli d'un modèle à l'autre
.github/workflows/podcast.yml  podcast quotidien       .github/workflows/journal.yml  journal toutes les 30 min
tests/                         tests hors ligne
```

## Feuille de route

1. **Fait** : actus → tri selon toi → texte → voix → Telegram ; journal vocal (30 s à plusieurs heures) ; mémoire ;
   Inspi ; intérêts appris ; podcast de secours.
2. **Téléphone** : sonnerie ~10 s puis lecture automatique du podcast (étape 7).
3. **Réponses instantanées** (optionnel) : une « sonnette » gratuite (Cloudflare) qui réveille le journal dès que tu
   écris, au lieu d'attendre le prochain passage.
4. **Plus de perso** : agenda, mails importants, notifications du téléphone — toujours via Groq uniquement.
5. **Ta voix** : voix clonée (XTTS-v2) quand une solution locale gratuite tiendra dans les limites de GitHub.
