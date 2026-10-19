# Réveil IA — réveil + podcast du matin + journal vocal

Trois choses qui s'alimentent les unes les autres, toutes gratuites :

1. **Le journal vocal** : tu parles (ou tu écris, ou tu envoies une photo) à ton bot Telegram, de 30 secondes à plus
   d'une heure. Il transcrit, retient ce qui compte (anniversaires, échéances, projets, goûts, **cours**…) et te pose des
   questions quand tu n'as plus d'inspiration (bouton **💡 Inspi**). Tu lui dis aussi **comment te parler**
   (« plus décontracté », « phrases courtes »…) et **à quelle heure te réveiller**.
2. **Le podcast du matin** (~2 min, ton positif, jamais dramatique) : les actus qui comptent et qui t'intéressent, puis
   tes infos perso (anniversaires, rappels, **tes cours du jour**). Il arrive chaque matin dans la conversation Telegram.
3. **Le réveil** : une petite sonnerie d'environ 10 secondes, puis le podcast se lit tout seul, **à l'heure exacte** que
   tu as choisie dans Telegram. C'est le rôle de la « sonnette » (étape 8), qui tourne gratuitement chez Cloudflare.

```
TOI (Telegram) ─ vocal / texte / photo / « 💡 Inspi » / « mets mon réveil à 6h30 » ─►  JOURNAL  (GitHub Actions)
                                                      Groq Whisper : transcription      toutes les 30 min, ou dans la
                                                      Groq (IA)    : extraction, vision  minute avec la sonnette
                                                      └─► memoire/  (fichiers lisibles, dans ton dépôt privé)
                 ◄─ « ✅ Noté… » / « ⏰ Réveil en semaine : 6 h 30 » / « 📅 J'ai lu ton emploi du temps… »

                                                      PODCAST DU MATIN  (GitHub Actions)
 flux RSS ─► Gemini note les actus ─► choix par le programme ─► Gemini écrit le texte (tes consignes de style en plus)
                       ▲ tes centres d'intérêt APPRIS dans memoire/        │
 infos perso + cours du jour : écrits par le programme (Groq seul les reformule, si tu as une consigne dessus)
                                                      ─► Gemini TTS (voix réaliste ; Edge en secours) ─► latest.mp3 ─► + sonnerie = reveil.mp3
                 ◄─ le mp3 arrive dans Telegram  +  branche « podcast » (latest.mp3, reveil.mp3, sonnerie.mp3, latest.txt…)

SONNETTE  (Cloudflare Workers, une fois par minute)
   ├─ elle voit un message qui t'attend ───────────► lance le journal tout de suite
   ├─ 15 min avant l'heure de ton réveil ──────────► lance le podcast
   └─ à l'heure exacte ────────────────────────────► Automate ou MacroDroid (ton téléphone) joue reveil.mp3
                                                     (plan B : un message Telegram avec notification)
```

**Qui choisit tes actus ?** Au début, une liste *provisoire* choisie par Claude (fichier `moi.yaml`). Dès que ton
journal a appris au moins **3** de tes centres d'intérêt, cette liste est abandonnée toute seule et c'est TA mémoire
qui décide. Le fichier `choix.txt` (branche `podcast`) dit toujours laquelle est utilisée et pourquoi chaque actu
est passée ou non.

---

## Mise en route (dans l'ordre)

Tu as déjà fait les étapes de la v1 (dépôt privé `Terreur-L3/reveil-ia`, clé Gemini, secret `GEMINI_API_KEY`).
Les étapes 1 à 6 mettent en route le journal et le podcast ; l'étape 7 règle ton réveil ; l'étape 8 le rend automatique.
Tu peux t'arrêter après la 7 : le podcast t'attend alors dans Telegram, et la sonnette viendra quand tu veux.

### Étape 0 — Mettre le dépôt à jour (Termux)

Le zip de mise à jour (`reveil-ia-v6-maj.zip`) ne contient **que ce qui change** : il ne touche ni à ta mémoire, ni à ton
`moi.yaml`, ni à ton `config.yaml` (deux petits outils en mettent à jour seulement les sections voix et perso), ni à
`extras/sonnette/` (ton `worker.js`, ton `wrangler.toml` avec tes identifiants Cloudflare et ton déploiement automatique
restent tels quels).

```
cd ~/storage/shared/Scripts/reveil-ia && git pull --rebase origin main        # 1. récupère d'abord ce que le robot a pu écrire
cd .. && unzip -o reveil-ia-v6-maj.zip                                         # 2. dézippe par-dessus (le zip est dans « Scripts »)
cd reveil-ia && python3 outils/maj_config_perso.py                             # 3. corrige la section perso de TON config.yaml
git add -A && git commit -m "v6 : réglages par jour, consignes sur tes cours" && git push   # 4. envoie
```

Fais toujours le `git pull --rebase` **avant** de dézipper : le robot enregistre ta mémoire dans le dépôt, donc le
dépôt change même quand tu n'y touches pas. (`unzip` introuvable ? `pkg install unzip`. `python3` introuvable ?
`pkg install python`. Le `pull` se plaint de « unstaged changes » ? Fais d'abord `git add -A && git commit -m "avant v6"`,
puis recommence. Le `push` répond « without `workflow` scope » ? Voir « Si ça coince » : cette mise à jour modifie un
fichier de `.github/workflows/`.)

L'étape 3 sauvegarde ton ancien fichier dans `config.yaml.avant-perso`, remplace le commentaire « jamais envoyées à une IA »
(devenu inexact, voir « Confidentialité ») et ajoute la ligne `style_ia: true` ; rien d'autre ne change, et elle est sans
danger si tu la relances. Elle est facultative : sans elle, tout marche pareil. (Tu n'as jamais lancé la v5 ? Lance aussi
`python3 outils/maj_config_voix.py`, qui active la voix réaliste.)

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

Ces valeurs ne s'écrivent **qu'ici** (et, plus tard, dans Cloudflare) : jamais dans un message, jamais dans un fichier du dépôt.

### Étape 4 — Premier lancement du journal

Onglet **Actions → Journal vocal → Run workflow**
(<https://github.com/Terreur-L3/reveil-ia/actions>). Au bout d'une minute, le bot t'envoie un message de bienvenue
avec quatre boutons en bas : **💡 Inspi**, **🧠 Mémoire**, **⏰ Réveil** et **📅 Cours**. Rien ? Envoie-lui `/start`
dans Telegram, relance, et au besoin lis la section « Si ça coince ».

### Étape 5 — Parle-lui

1. Envoie un vocal (ou un texte) : « Je m'appelle… j'adore le cinéma d'animation, mon ami Karim fête son
   anniversaire le 12 mars… ».
2. Relance **Run workflow** (ou attends le prochain passage, voir « Délais »). Le bot répond :
   `✅ Noté (45 s de vocal)` et liste ce qu'il a retenu (anniversaires 🎂, rappels 🔔, centres d'intérêt ⭐).
3. Appuie sur **💡 Inspi** : au prochain passage, il te pose une question (écrite **et** dite à voix haute).
   Réponds à la voix ou par écrit : ta réponse est rangée dans la mémoire comme le reste.

### Étape 6 — Essaie le podcast

Dans Telegram, écris **/podcast** (ou : **Actions → Podcast du matin → Run workflow**). Au bout de 3 à 5 minutes, le mp3
arrive dans la conversation (et sur la branche `podcast`). Ensuite il se prépare tout seul chaque matin.

### Étape 7 — Ton réveil et ton emploi du temps, par Telegram

1. **L'heure du réveil.** Écris `/reveil 6h30 semaine` (ou dis-le : « mets mon réveil à 6h30 en semaine »). Le bot
   répond toujours ce qu'il a compris : `⏰ Réveil en semaine : 6 h 30.` puis ton horaire et le prochain réveil.
   Tu peux le changer quand tu veux, de la même façon (voir « Ton réveil par Telegram »).
2. **Ton emploi du temps.** Envoie la photo (ou la capture d'écran) de ton planning d'école. Le bot lit les cours
   avec une IA de vision et te répond `📅 J'ai lu ton emploi du temps : 4 cours (lun, mar, mer, jeu).` Chaque matin, le
   podcast dit alors : « Côté cours, aujourd'hui : Étalonnage de 9 h à 12 h 30, en salle 204… ». Plus de détails dans
   « Tes images ».
3. **Comment te parler.** Dis-lui : « parle-moi de façon plus décontractée » ou tape `/consigne phrases courtes` :
   il le retient (voir « Tes consignes »). Tu peux aussi lui dire comment citer tes cours (« cite mes cours sans la
   salle ») et avoir un podcast plus long le week-end (« 3 minutes en semaine, 5 le week-end » : voir « Un réglage
   différent selon les jours »).

### Étape 8 — La sonnette : le podcast se lit tout seul, à l'heure exacte

Sans cette étape, tout marche, mais le podcast attend dans Telegram que tu l'ouvres. Avec elle, ton téléphone joue la
sonnerie puis le podcast à l'heure choisie, et le bot te répond en une minute au lieu d'une demi-heure.

Elle a trois morceaux, tous gratuits : un **jeton GitHub** (8.1), un petit programme **Cloudflare** (8.2 à 8.7) et une
macro **MacroDroid** ou un flux **Automate** sur le téléphone (8.8). Prévois ~30 minutes, avec le PC pour Cloudflare et le téléphone pour la fin.

**8.1 — Un jeton GitHub réservé à la sonnette.** GitHub → ta photo → **Settings → Developer settings → Personal access
tokens → Fine-grained tokens → Generate new token**.
- *Token name* : `sonnette` · *Expiration* : la durée maximale proposée (note la date dans ton agenda).
- *Repository access* : **Only select repositories** → `reveil-ia`.
- *Repository permissions* : **Actions : Read and write** et **Contents : Read-only** (le reste reste sur « No access »).
- **Generate token**, puis copie la valeur (`github_pat_…`) : GitHub ne la remontre jamais. Garde-la pour 8.4.

**8.2 — Un compte Cloudflare.** <https://dash.cloudflare.com/sign-up> (e-mail + mot de passe, pas de carte bancaire).

**8.3 — Le stockage de la sonnette.** Ouvre la page **Workers KV** du tableau de bord
(<https://dash.cloudflare.com/?to=/:account/workers/kv/namespaces>, ou menu **Storage & databases → Workers KV**),
puis **Create instance** : nom `sonnette` → **Create**.

**8.4 — Le programme.** **Workers & Pages → Create** → sous « Start with Hello World! », **Get started** → nom
`reveil-sonnette` → **Deploy** → **Continue to project** → l'icône **Edit code** (en haut de la page). Efface tout le code
d'exemple et colle à la place le contenu de `extras/sonnette/worker.js` (sur GitHub : ouvre ce fichier et clique sur
**Copy raw file**) → **Deploy**. Cloudflare te demande, la première fois, de choisir un sous-domaine ; le tableau de bord
affiche ensuite l'adresse de ton Worker : `https://reveil-sonnette.<ton-sous-domaine>.workers.dev`. (Les noms des
boutons Cloudflare changent de temps en temps : si l'un n'existe pas, envoie-moi une capture.)

**8.5 — Relier le stockage.** Dans le Worker : **Settings → Bindings → Add → KV namespace** : nom de variable **`KV`**
(en majuscules, exactement), namespace `sonnette` → **Deploy**.

**8.6 — Les réglages.** **Settings → Variables and Secrets → Add** (puis **Deploy** : sans lui, rien n'est pris en compte) :

| Type | Nom | Valeur |
|---|---|---|
| **Secret** | `GITHUB_TOKEN` | le jeton de 8.1 |
| **Secret** | `TELEGRAM_BOT_TOKEN` | le même jeton BotFather que dans GitHub |
| **Secret** | `SECRET` | un mot de passe inventé : **au moins 20** lettres minuscules et chiffres, sans espace ni symbole |
| Texte | `TELEGRAM_CHAT_ID` | ton identifiant Telegram (le même que dans GitHub) |
| Texte (facultatif) | `LEAD_MIN` | minutes d'avance pour préparer le podcast (15 par défaut, de 5 à 60) |
| Texte (facultatif) | `DOORBELL` | `fallback` (défaut : message Telegram seulement si le téléphone ne répond pas), `always` ou `never` |
| Texte (facultatif) | `REPO`, `BRANCH`, `TZ` | `Terreur-L3/reveil-ia`, `main`, `Europe/Paris` : déjà les valeurs par défaut |

Pour inventer `SECRET` dans Termux : `tr -dc 'a-z0-9' </dev/urandom | head -c 24` (puis recopie le résultat dans Cloudflare).
`SECRET` fait partie des adresses de la sonnette : **c'est un mot de passe**, ne le partage pas.

**8.7 — Le déclencheur.** **Settings → Triggers → Cron Triggers → Add** : `* * * * *` (toutes les minutes). Puis ouvre
`https://reveil-sonnette.<ton-sous-domaine>.workers.dev/status/<SECRET>` : tu dois lire « Prochain réveil : … » et, dans
`configuration`, `jeton_github`, `bot_telegram`, `identifiant_telegram` et `kv` à **true** (au bout d'une minute, « Dernier
passage de la sonnette » apparaît aussi). Si l'adresse ne répond pas : *Settings → Domains & Routes* → **workers.dev** activé.
Essais, avec les adresses suivantes (une toutes les 20 secondes au maximum) :

| Adresse | Ce qu'elle fait |
|---|---|
| `…/status/<SECRET>` | affiche l'état en clair |
| `…/test/<SECRET>/podcast` | lance le podcast (essai du jeton GitHub) : il arrive dans Telegram en 3 à 5 min, sans notification |
| `…/test/<SECRET>/doorbell` | envoie le message Telegram « ⏰ … bonjour ! » (le plan B), avec notification |
| `…/test/<SECRET>/ring` | fait sonner le téléphone (marche après 8.8) |
| `…/p/<SECRET>/reveil.mp3` | le fichier que joue le téléphone : sonnerie + podcast du jour, ou la sonnerie seule si le podcast n'est pas prêt |

**8.8 — La macro MacroDroid (téléphone).** ⚠️ C'est la seule partie du projet que je ne peux pas vérifier moi-même :
les écrans de MacroDroid peuvent différer de ma description. Si l'un d'eux ne ressemble pas à ce qui suit, envoie-moi
une capture d'écran et on ajuste ensemble.

1. Installe **MacroDroid** (Play Store, la version gratuite suffit : une seule macro). Dans les réglages d'Android,
   mets l'appli sur **Batterie : Non restreinte** (Applis → MacroDroid → Utilisation de la batterie), et accepte les
   autorisations qu'elle demande.
2. **Nouvelle macro** « Réveil IA ». **Déclencheur** : *Connectivité → Webhook (URL)*. MacroDroid affiche une adresse
   du type `trigger.macrodroid.com/<identifiant de ton téléphone>/<nom>` (utilise l'adresse normale, pas la version
   raccourcie « TinyURL ») : **copie-la**, puis ajoute-la dans Cloudflare sous le nom **`RING_URL`** (type *Secret*).
   Elle doit commencer par `https://` : si MacroDroid l'affiche avec `http://`, change ce début. Elle aussi est un mot
   de passe : quiconque la connaît peut faire sonner ton téléphone. (`/status` indique `adresse_macrodroid: true` quand
   elle est bien reconnue.)
3. **Actions**, dans cet ordre :
   1. *Volume → Régler le volume* : flux **Alarme**, au niveau que tu veux (80 %…) ;
   2. *Web → Requête HTTP* : méthode **GET** vers `https://reveil-sonnette.<ton-sous-domaine>.workers.dev/p/<SECRET>/reveil.mp3`,
      avec l'option **enregistrer la réponse dans un fichier** (par ex. `Téléchargements/reveil.mp3`) et « attendre la fin » ;
   3. *Média → Jouer un son* : ce fichier, flux **Alarme**, « attendre la fin de la lecture ».
4. Essai : ouvre `…/test/<SECRET>/ring`. Le téléphone doit sonner puis lire le podcast en quelques secondes.
   Rien ne se passe ? Regarde le journal de la macro (MacroDroid → *Journal système*) et envoie-moi une capture.

**8.8 bis — Automate (LlamaLab) à la place de MacroDroid.** La sonnette sait réveiller le téléphone par un « message
cloud » d'Automate (gratuit). Dans Cloudflare, ajoute le secret **`AUTOMATE_SECRET`** (la clé « secret » générée sur
llamalab.com/automate/cloud) et **`AUTOMATE_TO`** (l'e-mail du compte Google d'Automate) : si les deux sont là, Automate
est utilisé à la place de `RING_URL`. Le flux sur le téléphone : **Cloud message receive** → **HTTP request** (GET vers
`…/p/<SECRET>/reveil.mp3`, réponse enregistrée dans un fichier) → **Sound play** (ce fichier, flux alarme). `/status`
indique `automate: true`, et `…/test/<SECRET>/ring` fait sonner le téléphone pour l'essai.
⚠️ Automate enregistre **ce qu'il reçoit** comme s'il s'agissait du son : si la sonnette n'a rien à lui donner, il garde un
petit fichier de texte (« aucun fichier audio disponible ») que « Sound play » ne peut pas lire. Depuis la v4.1 de
`worker.js`, tu reçois alors tout de suite un message Telegram avec notification, et `…/status/<SECRET>` teste en direct
GitHub et Telegram et dit en français ce qui ne va pas (jeton expiré, mauvais droits, branche `podcast` vide…).
Pour aller plus loin, ajoute au flux, sur le chemin « échec » du bloc « Sound play », une requête HTTP GET vers
`…/p/<SECRET>/echec` : la sonnette prévient alors aussitôt dans Telegram.

**8.9 — Le plan B et la ceinture.** Si le téléphone n'a pas été chercher le fichier 2 minutes après l'heure, la sonnette
t'écrit dans Telegram (avec notification) : `⏰ 6 h 30 — bonjour ! Ton podcast du matin est dans la conversation…`.
Garde malgré tout, **pendant les premières semaines**, une alarme normale de l'appli Horloge ~15 minutes après ton heure
de réveil : tant que tu n'as pas vu la macro marcher plusieurs matins de suite, c'est elle qui te sauve la mise.

À savoir : le **premier matin**, le podcast peut aussi arriver vers 5 h 17 (4 h 17 après le passage à l'heure d'hiver)
avec une notification : c'est le passage planifié de GitHub, qui ne s'efface que lorsque la sonnette a déjà fait celui
de la veille. Ça ne se reproduit plus ensuite.

---

## Au quotidien

| Tu fais | Il se passe |
|---|---|
| Vocal de 30 s à ~1 h 30 (ou texte) | Transcrit, rangé dans `memoire/`, accusé « ✅ Noté » avec ce qui a été retenu |
| Photo / capture / fichier image | Lu par Groq (vision) : emploi du temps → tes cours ; autre document → texte rangé dans le journal |
| **💡 Inspi** (ou `/inspi`) | Une question adaptée à ce qu'il sait de toi, écrite + vocale. Ta réponse enrichit la mémoire |
| **🧠 Mémoire** (ou `/memoire`) | Ce qu'il a appris, tes centres d'intérêt, tes consignes, ton emploi du temps |
| **⏰ Réveil** (ou `/reveil`) | Ton horaire et le prochain réveil. `/reveil 6h30 semaine` pour le changer |
| **📅 Cours** (ou `/cours`) | Ton emploi du temps de la semaine. `/cours lundi 9h-12h étalonnage salle 204` pour l'écrire à la main ; `/cours effacer` |
| `/podcast` | Prépare le podcast tout de suite (une fois toutes les 10 minutes au plus) |
| `/duree 3` · `/voix sulafat` · `/vitesse lent` | Longueur du podcast (1 à 8 min), voix (voir « Une voix réaliste » ci-dessous), vitesse (lent, normal, rapide ou `-15%`). Différent selon les jours : `/duree 3 semaine 5 week-end` |
| `/voix essai` | Je t'envoie quelques échantillons de voix réalistes à écouter (`/voix essai puck zephyr` pour choisir lesquelles) |
| `/consigne phrases courtes` · `/consignes` · `/oublier 2` | Ajouter, lister, retirer une consigne de comportement. Pour certains jours : `/consigne le week-end, parle plus décontracté` |
| `/perso demain` | T'écrit comment tes cours, anniversaires et rappels de demain seront dits, avec tes consignes (voir « Tes consignes ») |
| `/reglages` | Tous tes réglages d'un coup (réveil, podcast, consignes, emploi du temps) |
| `/aide` | Rappel des fonctions |
| Rien pendant la journée | Entre 19 h et 23 h : une question du soir (une fois par jour, jamais deux à moins de 4 h d'écart). Réglable : `heure_question` dans `config.yaml` |
| « Je ne veux plus entendre parler de… » | Le sujet est noté comme à éviter dans les actus |

Pour répondre à une question précise, utilise « Répondre » sur le message-question : sinon la réponse est rattachée
à la dernière question restée sans réponse (moins de 36 h).

## Un réglage différent selon les jours

La durée, la voix et la vitesse du podcast peuvent changer d'un jour à l'autre. Dis-le comme ça vient, en vocal ou en
texte : « je voudrais un podcast de 3 minutes en semaine et de 5 minutes le week-end » (ou « … trois minutes en semaine et
cinq le week-end ») est lu **à règles fixes, comme l'heure du réveil, sans dépendre de l'IA** : le bot répond tout de suite
ce qu'il a compris (`🔧 Réglé : • durée du podcast en semaine : environ 3 minutes • durée du podcast le week-end : environ
5 minutes`) et cela vaut dès le prochain podcast. Pour la voix et la vitesse par jour, tape la commande (ou dis-le : l'IA
les comprend aussi, mais moins sûrement). Les commandes :

| Tu écris | Résultat |
|---|---|
| `/duree 3 semaine 5 week-end` | 3 minutes du lundi au vendredi, 5 le samedi et le dimanche |
| `/duree 5 le week-end, 3 sinon` | 5 minutes le week-end, 3 les autres jours |
| `/duree 2 lundi et mercredi` | seulement ces jours-là ; les autres jours gardent leur réglage |
| `/voix femme en semaine, kore le week-end` · `/vitesse lent semaine rapide week-end` | pareil pour la voix et la vitesse |
| `/duree 4` | 4 minutes **tous les jours** (efface les durées propres à un jour) |
| `/reglages` | ce qui s'applique à chaque jour : « environ 3 min en semaine, 5 min le week-end » |

Le bot **refuse sans deviner** ce qui n'est pas une habitude de la semaine : une date (« demain », « le 12 octobre », « ce
week-end »), une période (« du 12 au 15 ») ou une exception (« sauf le dimanche »). Il te le dit et ne change rien ; dis
plutôt les jours voulus (« du lundi au samedi »). Le réveil se règle de la même façon (`/reveil 8h30 semaine 12h30 week-end`).

## Une voix réaliste (Gemini)

Le podcast est dit par **Gemini TTS** (Google) : des voix beaucoup plus naturelles que les voix « lues » d'avant (elles
respirent, mettent le ton, sourient). C'est gratuit avec la clé `GEMINI_API_KEY` que tu as déjà (aucun secret à ajouter).

**Essayer et choisir.**
1. Dans Telegram : `/voix essai`. Une à trois minutes plus tard arrivent 6 échantillons (la même phrase, voix différentes)
   et, pour comparer, une voix Edge classique. (Ou : GitHub → Actions → **Essai de voix** → Run workflow.)
2. Choisis à l'oreille : `/voix sulafat` (chaleureuse), `/voix achird` (amicale), `/voix kore`, `/voix charon`, `/voix orus`,
   `/voix algieba`… Il y en a **30** (`/voix essai puck zephyr leda` pour écouter d'autres). C'est pris en compte au
   prochain podcast ; `/podcast` en fait un tout de suite.
3. Le ton se règle dans `config.yaml` → `voix` → `gemini` → `style` (c'est une phrase, en anglais de préférence : « warm,
   natural and relaxed, like a friendly morning radio host… »). Essaie par exemple « energetic and upbeat » pour les
   lundis difficiles. La vitesse (`/vitesse lent`) marche aussi.

**Filet de sécurité.** Si Gemini ne répond pas (quota du jour atteint, panne, clé refusée), le podcast sort quand même, avec
la voix Edge, et la raison est écrite dans `meta.json` (« voix_note ») et sous le message Telegram (« voix Gemini indisponible
(…) : voix Edge à la place »). Un réveil ne finit jamais en silence. Les questions du soir (Inspi) restent toujours sur Edge.

**Ce que je n'ai pas pu vérifier moi-même.** Je n'ai pas accès à Gemini depuis mon environnement : tout est testé hors ligne
contre un faux serveur qui suit la documentation Google d'octobre 2026. Les noms de modèles (`gemini-3.8-flash-tts`,
`…flash-lite-tts`) et les limites du palier gratuit (non publiées) peuvent changer : si `/voix essai` répond par des ❌, le
message te donne la raison exacte (écris-la-moi, sans aucune clé) et je corrige. Un podcast par jour ne demande qu'un appel.

**Pourquoi pas ta propre voix ?** Le clonage de voix de Gemini n'est pas proposé en Europe (EEE) via AI Studio. Les
voix « sur mesure » (Voice design, nom du type `voice_…`) existent : si tu en crées une, écris `/voix voice_…` ou mets-la
dans `config.yaml`. Les autres pistes gratuites (ElevenLabs, modèles locaux comme XTTS ou Chatterbox) sont soit trop limitées,
soit trop lentes sur les machines gratuites de GitHub.

**Marquage.** L'audio de Gemini contient un filigrane inaudible (SynthID) qui indique qu'il est généré par IA.

## Ton réveil par Telegram

Les ordres de réveil sont compris par des **règles fixes**, pas par une IA : une erreur d'interprétation te ferait
sonner à la mauvaise heure. Le bot **répète toujours** ce qu'il a compris, avec ton horaire et le prochain réveil.

| Tu écris (ou tu dis) | Résultat |
|---|---|
| `/reveil 6h30` · « mets mon réveil à 6h30 » | tous les jours 6 h 30, de façon durable |
| `/reveil 7h semaine` · « 7h en semaine » · « du lundi au vendredi » | lundi à vendredi seulement |
| `/reveil 9h week-end` · « 9h le week-end » · « samedi et dimanche » | samedi et dimanche seulement |
| « 8h tous les lundis » · « 7h les mardis et jeudis » | ces jours-là, durablement |
| `/reveil demain 5h45` · « mets le réveil à 5h45 demain » | une seule fois : demain (les autres jours ne changent pas) |
| « 8h lundi » · « 6h le 12 octobre » | une seule fois, ce jour-là |
| « 7h30 en semaine et 9h le week-end » · « 7h, et le week-end 9h » | deux réglages d'un coup |
| `/reveil off` · « pas de réveil demain » · « pas de réveil le week-end » | désactive tout / ce jour-là / ces jours-là |
| `/reveil on` · « réactive le réveil » | le remet en route (l'horaire est gardé) |
| `/reveil` · « à quelle heure est mon réveil ? » | te montre l'horaire |

Ce qu'il **ne devine pas** — il te le dit et ne change rien (il applique quand même le reste de ton message) :
une période (« du 12 au 15 octobre », « pendant le tournage », « la semaine prochaine »), une exception (« tous les
jours sauf le dimanche »), « à partir de lundi », ou plusieurs heures dont on ne sait pas à quels jours elles vont
(« à 7h et 9h »). Fais-le **un jour à la fois** : « mets le réveil à 5h le 12 octobre », puis « … le 13 », etc.

À savoir :
- Un message court qui ne contient qu'un ordre n'entre pas dans le journal ; un long message avec un ordre au
  milieu fait les deux. Un texte **transféré** ne peut jamais donner un ordre.
- La phrase « demain je me lève à 6h pour le tournage » est une information, pas un ordre : rien ne change.
- Le changement est pris en compte dès que le journal a lu ton message (quelques minutes avec la sonnette). Un réveil
  modifié **après** son heure ne sonne pas le jour même : change-le avant, ou pour le lendemain.
- Le fichier `memoire/reglages.json` contient ces réglages (et c'est lui que lit la sonnette) : tu peux le corriger à la
  main sur GitHub, mais le plus simple est de passer par le bot.

## Tes images : emploi du temps et documents

- Envoie une **photo** ou une **capture d'écran**. Pour un planning avec de petits caractères, envoie-la plutôt
  **« en tant que fichier »** (Telegram écrase les photos) ou recadre quelques jours à la fois.
- Une IA de vision de **Groq** lit l'image (jamais Gemini). **L'image n'est jamais gardée** : seul le texte lu est rangé.
- Un **emploi du temps** est reconnu, nettoyé (jours, horaires, salles, enseignants) et répété chaque semaine. Écris en
  légende **« cette semaine seulement »** (ou « exceptionnel ») pour une exception : sinon c'est ta semaine type. Une
  légende comme « groupe B » aide à ne garder que ton groupe. Si l'image affiche les dates de la semaine, il les lit et
  les garde. Un nouvel emploi du temps remplace les jours qu'il montre, pas les autres.
- Le podcast dit les cours **du jour** : jusqu'à 4 sont énumérés ; au-delà, il dit combien tu en as et le premier.
  Le dimanche, il annonce le premier cours de lundi. La phrase est fabriquée par le programme à partir du fichier :
  les noms de cours ne vont **jamais à Gemini** (ni au tri, ni à l'écriture) ; ils ne vont à Groq que si tu as donné une
  consigne sur la façon de les dire (voir « Tes consignes »). Pour ne plus les entendre : `cours: false` dans la section
  `perso` de `config.yaml`.
- Un **autre document** (affiche, mot de l'école, ticket…) est lu et rangé dans le journal comme un texte que tu aurais
  écrit… **sans pouvoir donner d'ordres** : un document qui dirait « change ta façon de parler » ne change rien.
- Pas de PDF pour l'instant : fais-en une capture d'écran.

## Tes consignes : comment il doit te parler

Dis-le comme ça vient : « parle-moi de façon plus décontractée », « fais des phrases courtes », « pose-moi des questions
plus précises », « ne me parle jamais de mon argent »… ou tape `/consigne …`. Il répond `📌 Consigne retenue` (tu peux
la retirer avec `/consignes` puis `/oublier N`). Chaque consigne concerne **le podcast**, **tes questions Inspi**,
**la façon de dire tes infos perso** (cours, anniversaires, rappels) ou **tout**.

- Les consignes de style accompagnent la rédaction du podcast. Celles qui touchent un sujet **sensible** (santé, argent,
  politique…) sont marquées 🔒 : elles servent aux questions (Groq seul) mais **ne partent jamais** chez Gemini.
- Elles ne changent ni la longueur maximale ni le ton positif du podcast.
- Pour que le podcast ne suive plus aucune consigne : `consignes_dans_podcast: false` (section `perso` de `config.yaml`).

**Pour certains jours seulement.** « Le week-end, parle-moi de façon plus décontractée », « le lundi et le mercredi, va
droit au but » : la consigne ne sort que ces jours-là (`/consignes` l'affiche : « · le week-end »). Sans jour, elle vaut
tous les jours. Comme pour les réglages, une date ou une exception (« demain », « sauf le dimanche ») est refusée plutôt
que devinée. Un sujet n'est pas un jour : « parle-moi des films du week-end » reste une consigne de tous les jours.

**Comment il dit tes cours, anniversaires et rappels.** Ces phrases sont écrites par le programme (elles sont exactes,
mais toujours dites pareil). Dis par exemple : « cite mes cours sans la salle », « dis mes cours en commençant par
l'heure », « souhaite les anniversaires avec plus de chaleur », « ne me dis que le premier cours » (ou `/consigne cite mes
cours sans la salle`). Il répond « concerne : mes infos perso » et le podcast suit la consigne dès le prochain podcast.
`/perso demain` t'écrit tout de suite comment demain serait dit, pour vérifier sans attendre.

- **Ce qui part où.** Seulement si une consigne de ce genre existe, ces phrases (avec tes prénoms, tes cours, tes
  rappels) sont envoyées à **Groq** pour être reformulées — **jamais à Gemini**. Sans consigne, ou avec `style_ia: false`
  (section `perso` de `config.yaml`), rien n'est envoyé nulle part. (La voix Gemini TTS, elle, entend le podcast terminé :
  voir « Confidentialité ».)
- **Le programme vérifie avant de parler.** Aucun chiffre, aucune heure, aucun jour (aujourd'hui, demain), aucun nom ne
  doit être changé ou ajouté, et chaque cours, prénom ou rappel doit rester dit — sauf si ta consigne demande d'en omettre
  (« seulement le premier cours », « ne cite plus… »). « Sans la salle » retire un détail mais ne permet pas d'oublier un cours.
- **En cas de doute, le texte sûr.** Si la reformulation échoue deux fois au contrôle, ou si Groq ne répond pas, le podcast
  dit le texte du programme tel quel, et le message Telegram du podcast te le dit (`ℹ️ consigne sur tes infos perso non
  appliquée ce matin : …`). Une consigne mal appliquée ne fausse jamais ton matin : pas d'heure inventée, pas de cours perdu.

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
  (bouton ✏️) — `interets.yaml`, `anniversaires_auto.yaml`, `rappels_auto.yaml`, `faits.jsonl`, `consignes.yaml`,
  `emploi_du_temps.yaml`. Le robot
  relit ces fichiers à chaque passage (seuls les commentaires du YAML sont réécrits). Évite d'éditer pile au moment d'un
  passage (voir « Si ça coince »).

## Confidentialité : qui voit quoi

| Service | Ce qu'il voit | Pourquoi / garde-fous |
|---|---|---|
| **Groq** | Tes vocaux (le temps de la transcription), le texte de ton journal, **tes images** (le temps de la lecture), tes consignes, et — **seulement si tu as une consigne sur la façon de les dire** — tes phrases perso du jour (cours, prénoms, rappels) | Seul fournisseur IA qui voit ta vie privée. Contrat : pas d'entraînement sur tes données, pas de conservation par défaut ; option « Zero Data Retention » |
| **Gemini** (Google, offre gratuite) | Des titres d'actus **publics**, les libellés de tes centres d'intérêt **non sensibles** (ex. « Houdini ») et tes consignes de **style** non sensibles | L'offre gratuite peut servir à améliorer les modèles de Google : jamais ton journal, jamais une image, jamais un nom, une date perso, un cours ou un sujet sensible (jamais non plus tes phrases perso reformulées : c'est Groq qui s'en charge) |
| **Gemini TTS** (la voix du podcast) | Le texte du podcast à lire, **y compris tes lignes perso et tes cours du jour** (« demain, Karim fête ses 21 ans »). Jamais ton journal, ni tes images, ni les questions Inspi | En Europe, Google applique à l'API les conditions des services payants : tes textes ne servent pas à entraîner ses modèles. Si ça te gêne : `max_lignes: 0` et `cours: false` (section `perso`) dans `config.yaml`, ou `/voix homme` (voix Edge) |
| **Microsoft Edge TTS** (voix de secours) | Les questions Inspi (toujours) et, si Gemini est indisponible ou choisi par toi, le texte du podcast | Service non officiel et gratuit de Microsoft. Si ça te gêne : `question_vocale: false` (section `journal`) dans `config.yaml` |
| **Telegram** | Tes messages, tes vocaux, tes photos, le podcast | Les conversations avec un bot ne sont pas chiffrées de bout en bout |
| **GitHub** (dépôt **privé**) | `memoire/` contient le **texte complet** de ce que tu dis, ton emploi du temps et tes réglages ; la branche `podcast` contient le podcast du jour | **Ne passe jamais le dépôt en public.** L'audio et les images ne sont jamais conservés, seulement le texte |
| **Cloudflare** (la sonnette) | Les deux jetons (GitHub, bot Telegram) que tu lui confies, ton horaire de réveil, le podcast du jour (il passe par elle jusqu'au téléphone) ; elle voit *passer* tes messages en attente dans Telegram | Elle n'en **garde rien** (ni message, ni podcast), n'écrit jamais dans ton dépôt, n'efface jamais un message. Jeton GitHub limité à ce seul dépôt. Les adresses sont secrètes (`SECRET`) |

Le jeton Telegram et les clés n'apparaissent jamais dans les logs. Le contenu de tes messages non plus.

## Délais, limites et quotas (tout est gratuit, avec ces plafonds)

- **Délai** : sans la sonnette, le journal est lu toutes les 30 minutes environ (GitHub peut retarder ou sauter un passage
  quand il est chargé : compte « dans l'heure »). Avec elle : une à deux minutes. Pour une réponse immédiate sans
  sonnette : *Actions → Journal vocal → Run workflow* (case « Poser une question tout de suite » pour un Inspi).
- **Taille** : Telegram interdit aux bots de télécharger plus de **20 Mo** par fichier (souvent ~1 h 30 de vocal).
  Plus long : envoie en deux fois ; le bot te prévient si un fichier est trop gros. Une image de plus de 9 Mo, ou dans
  un autre format que JPEG, PNG ou WebP, est réduite ou convertie avec ffmpeg (sinon le bot te le dit).
- **Transcription** : Groq autorise 7 200 s d'audio par heure et 28 800 s par jour (offre gratuite). Un vocal de 2 h
  est donc transcrit sur plusieurs passages ; la progression est gardée entre deux passages.
- **Minutes GitHub** : un dépôt privé a 2 000 min/mois gratuites. Ordre de grandeur : journal planifié toutes les
  30 min ≈ 1 000, podcast ≈ 100, lancements de la sonnette (un par message, 80 par jour au plus) ≈ 100 à 300.
  Vérifie dans *Settings → Billing and plans → Usage*. Trop près du plafond ? Dans `journal.yml`, passe
  `"7,37 5-21 * * *"` à `"7 5-21 * * *"` (une fois par heure : avec la sonnette, ce passage n'est plus qu'un filet).
- **Cloudflare (offre gratuite)** : 100 000 requêtes par jour (la sonnette en fait ~1 500), un déclencheur toutes les
  minutes, un stockage KV de 1 000 écritures par jour (elle en fait moins de 300 en pratique). Ce sont les limites
  de Cloudflare à ma connaissance, elles peuvent changer : vérifie-les sur leur site.
- Les offres gratuites changent : Groq (<https://console.groq.com/docs/rate-limits>) et Google peuvent modifier leurs
  quotas. Les listes de modèles sont dans `config.yaml` (section `llm`) et modifiables.

## Si ça coince

| Symptôme | Que faire |
|---|---|
| Le bot ne répond jamais | *Actions → Journal vocal →* dernier passage → lis le message en rouge. « Il manque … » = un secret absent (étape 3) |
| Passage en échec avec « Telegram refuse le token » | Le secret `TELEGRAM_BOT_TOKEN` est faux : recopie le jeton de BotFather |
| Passage en échec avec « 409 » / « webhook » | Un webhook est actif sur ton bot. Ouvre `https://api.telegram.org/bot<TON_JETON>/deleteWebhook`, puis relance |
| Rien ne se lance tout seul | GitHub n'exécute les tâches planifiées que sur la branche principale ; en attendant, utilise *Run workflow* ou `/podcast` |
| « Impossible d'enregistrer la mémoire (conflit ?) » | Tu as modifié sur GitHub un fichier de `memoire/` au même instant. Rien n'est perdu : tes messages Telegram sont relus au passage suivant |
| Le podcast est très court, avec « je n'ai pas pu préparer les actus » | C'est le **podcast de secours** (bonjour + anniversaires/rappels/cours). Lis `choix.txt` sur la branche `podcast` : la raison y est écrite (flux en panne, IA indisponible) |
| Ma consigne sur mes cours (ou mes anniversaires) ne change rien | Tape `/perso demain` : si le bot répond « Aucune consigne sur tes infos perso », la consigne n'a pas été rangée dans « mes infos perso » (reformule : « cite mes cours sans la salle »). S'il répond « ⚠️ Je n'ai pas pu appliquer… », la raison est écrite (une heure changée par l'IA, un cours oublié, Groq indisponible) : le texte sûr est dit à la place ; reformule plus simplement. Le détail est aussi dans `meta.json` → `perso` → `style` |
| Mon réglage « semaine / week-end » n'a pas été pris | `/reglages` montre ce qui est retenu. Une date, une période ou une exception (« demain », « du 12 au 15 », « sauf le dimanche ») est refusée avec un message : redis-le avec des jours (« 3 en semaine, 5 le week-end ») |
| Le podcast est dit avec la voix de secours (Edge) | Regarde « voix_note » dans `meta.json` (branche `podcast`) ou sous le message Telegram : quota Gemini atteint (réessaie plus tard), clé refusée (secret `GEMINI_API_KEY`), panne. `/voix essai` te dit lesquelles marchent |
| Le téléphone joue un son bizarre ou rien, et le fichier téléchargé ne fait que ~76 octets | C'est un message d'erreur de la sonnette, pas un son : ouvre `…/status/<SECRET>`, il dit ce qui ne va pas (le plus souvent le jeton `GITHUB_TOKEN` de Cloudflare : expiré, ou sans « Actions : Read and write » + « Contents : Read-only » sur le dépôt) |
| Un flux d'actus est « EN ÉCHEC » dans `choix.txt` | Supprime sa ligne dans `config.yaml` (section `actu`) s'il l'est depuis plusieurs jours |
| Le vocal est « abandonné » | Format ou fichier illisible : renvoie-le. Après 4 essais, le bot te le dit |
| « Je n'arrive pas à lire cette image » / emploi du temps illisible | Renvoie-le **« en tant que fichier »**, plus net, ou recadre quelques jours. Sinon écris-le : `/cours lundi 9h-12h étalonnage salle 204` |
| `/podcast` répond « GitHub n'a pas lancé le podcast » | Le workflow `journal.yml` doit avoir `actions: write` (c'est déjà le cas dans la v4 : refais l'étape 0) |
| `git push` refusé dans Termux | Le robot a écrit entre-temps : `git pull --rebase`, puis `git push` |
| `git push` refusé avec « without `workflow` scope » | Le jeton GitHub de Termux n'a pas le droit de modifier les workflows : crée-en un avec la permission « workflow » (jeton classique) ou « Workflows : lecture et écriture » (jeton fin) |
| `…/status/<SECRET>` répond « Not found » | `SECRET` absent ou trop court (20 caractères minimum) dans Cloudflare, ou faute de frappe dans l'adresse |
| `/status` : `jeton_github` ou `kv` à **false** | Étape 8.6 (secret `GITHUB_TOKEN`) ou 8.5 (la liaison s'appelle exactement `KV`) |
| Message « ⚠️ Sonnette : je n'ai pas pu lancer… » | Jeton GitHub expiré ou sans les bons droits (Actions : lecture et écriture) : refais 8.1 et remplace le secret `GITHUB_TOKEN`. Ces alertes arrivent sans notification, au plus une toutes les 6 heures |
| Le téléphone ne sonne pas, mais le message Telegram « ⏰ … bonjour ! » arrive | Le plan B a servi : la macro MacroDroid ne s'est pas déclenchée. Teste `…/test/<SECRET>/ring` ; vérifie `RING_URL`, la batterie « Non restreinte » et envoie-moi une capture de la macro |
| La sonnette sonne, mais sans le podcast du jour | Elle joue alors la sonnerie seule (le podcast n'était pas prêt) : regarde l'onglet Actions → « Podcast du matin » |
| Il faut changer `SECRET` (fuite possible) | Cloudflare → Settings → remplace la valeur, puis mets la nouvelle adresse dans l'action « Requête HTTP » de la macro |

## Essais en local (facultatif, tout tourne déjà sur GitHub)

```
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                      # puis colle tes clés dedans
python -m src.main --no-audio             # texte du podcast + choix des actus, sans audio ni envoi
python -m src.journal --no-send           # une passe du journal, sans rien écrire dans Telegram
python -m unittest discover -s tests -t . # ~580 tests hors ligne (faux Telegram / Groq / Gemini / GitHub / flux RSS)
cd extras/sonnette && node --test         # 166 tests de la sonnette (Node 20 ou plus)
```

Pour les longs vocaux, la conversion des images et la sonnerie, `ffmpeg` est nécessaire : GitHub l'installe tout seul quand il en
a besoin (en local : `pkg install ffmpeg` sous Termux, `apt install ffmpeg` sous Linux). Sans lui, les tests qui en ont
besoin sont simplement sautés.

## Arborescence

```
config.yaml                    réglages : IA, tri des actus, flux RSS, voix, sonnerie, journal, heure de la question du soir
moi.yaml                       toi pour le podcast : liste provisoire, épingles, à ignorer (gagne sur la mémoire apprise)
memoire/                       anniversaires.yaml, rappels.yaml (à la main) + tout ce que le journal apprend :
  journal/…                    ce que tu as dit, par jour (lisible sur GitHub)
  faits.jsonl, interets.yaml, anniversaires_auto.yaml, rappels_auto.yaml, questions.jsonl, _etat/
  consignes.yaml               tes consignes de comportement          emploi_du_temps.yaml   tes cours
  reglages.json                ton réveil, la durée/voix/vitesse du podcast, par jour si tu veux (lu aussi par la sonnette)
src/main.py                    podcast : actus → tri → texte → voix → sonnerie → Telegram (+ podcast de secours)
src/news.py, triage.py         récupère les actus, les note, puis le programme choisit (seuils, plafonds, ton)
src/script_writer.py, tts.py   écrit le texte parlé (avec tes consignes de style) ; tts.py : Gemini TTS + secours Edge → mp3 ; `python -m src.tts --essai`
src/jingle.py, garde.py        sonnerie (reveil.mp3, sonnerie.mp3) ; garde-fou des lancements planifiés
src/perso.py, edt.py           anniversaires, rappels et cours du jour, écrits par le programme (sans IA)
src/perso_style.py             tes consignes sur la façon de les dire : Groq seul reformule, le programme contrôle, repli sur le texte sûr
src/journal.py                 une passe du journal : Telegram → transcription/vision → mémoire → ordres → questions
src/reglages.py, vision.py     comprendre « mets mon réveil à… » et « 3 min en semaine, 5 le week-end » (règles fixes) ; lire les images (Groq)
src/telegram.py, github.py, audio.py, stt.py, extract.py, inspi.py, memoire.py, pacer.py   les briques du journal
src/llm.py                     Gemini / Groq avec repli d'un modèle à l'autre
extras/sonnette/               la sonnette Cloudflare (worker.js, 166 tests ; Automate ou MacroDroid)
outils/maj_config_voix.py      met à jour la section voix de ton config.yaml (v5)
outils/maj_config_perso.py     met à jour la section perso de ton config.yaml (v6, étape 0)
.github/workflows/podcast.yml  podcast quotidien       .github/workflows/journal.yml  journal toutes les 30 min
.github/workflows/voix.yml     essai des voix (sur demande : /voix essai)
tests/                         tests hors ligne
```

## Feuille de route

1. **Fait** : actus → tri selon toi → texte → voix → Telegram ; journal vocal ; mémoire ; Inspi ; intérêts appris ;
   podcast de secours ; consignes de comportement ; lecture d'images (emploi du temps) ; réveil réglable par Telegram ;
   sonnette Cloudflare + sonnerie + lecture automatique à l'heure exacte (Automate ou MacroDroid) ; voix réaliste Gemini avec secours Edge ;
   durée/voix/vitesse et consignes différentes selon les jours ; consignes sur la façon de dire tes cours, anniversaires et rappels.
2. **Idées** (à ta demande) : « il y a un an, tu me disais… » dans le podcast ; perturbations de transports en Île-de-France
   le matin ; bilan de la semaine le dimanche ; emploi du temps en PDF ; agenda et mails importants (toujours via Groq seul).
3. **Ta propre voix** : le clonage n'est pas proposé en Europe par Gemini ; une voix « Voice design » sur mesure est possible (voir « Une voix réaliste »).
