# Réveil IA — podcast du matin

Chaque nuit, GitHub génère un mini-podcast (~2 min) avec les actus du jour, lu par une voix IA.
Le matin, ton téléphone télécharge le mp3 et le joue juste après ton réveil.

```
GitHub Actions (cron, ~5h30)
  └─ flux RSS ──► Gemini écrit le texte (~300 mots) ──► Edge TTS (mp3)
        └─ publié sur la branche privée « podcast » : latest.mp3, latest.txt, meta.json
Téléphone (MacroDroid, à l'heure du réveil)
  └─ télécharge latest.mp3 ──► le joue
```

## Arborescence

```
config.yaml                    réglages : sujets (flux RSS), voix, modèles, longueur
requirements.txt               dépendances Python
src/news.py                    récupère les actus (RSS)
src/llm.py                     client Gemini, avec repli entre plusieurs modèles
src/script_writer.py           prompt + écriture du texte parlé
src/tts.py                     texte → mp3
src/main.py                    orchestre le tout
.github/workflows/podcast.yml  automatisation quotidienne
```

## Mise en route

1. Dépôt GitHub **privé** + clé API Gemini (Google AI Studio).
2. Envoyer ce dossier dans le dépôt.
3. Dépôt → *Settings → Secrets and variables → Actions* → secret `GEMINI_API_KEY`.
4. Onglet *Actions* → « Podcast du matin » → *Run workflow*. Le résultat arrive sur la branche `podcast`.

## Test en local (facultatif)

```
python -m venv .venv
.venv\Scripts\activate          (Windows)   |   source .venv/bin/activate   (Mac/Linux)
pip install -r requirements.txt
copy .env.example .env          puis colle ta clé dans .env
python -m src.main --no-audio   affiche le texte, sans audio
python -m src.main              texte + mp3 dans output/
```

## Bon à savoir

- Offre gratuite de Gemini : Google peut utiliser le contenu envoyé pour améliorer ses modèles.
  Donc **pas de mails ni de messages perso en entier** avec cette offre (phase 2 à traiter avec soin).
- Edge TTS n'est pas une API officielle : il peut casser de temps en temps. Le code réessaie,
  essaie des voix de secours, et publie au moins le texte (`latest.txt`) si l'audio échoue.
- Les modèles Gemini 2.5 doivent être arrêtés par Google le 16 octobre 2026 : le projet utilise Gemini 3
  en priorité (liste modifiable dans `config.yaml`).

## Feuille de route

1. **Base** (ce dossier) : actus → texte → mp3 → téléphone.
2. **Perso** : anniversaires, notes, mails importants (en limitant ce qui part chez Google).
3. **Saisie vocale** : un bot Telegram pour alimenter ta base en parlant.
4. **Ta voix** : moteur XTTS-v2 (voix clonée).
5. **Messageries** : WhatsApp / Discord (via notifications ou un outil comme OpenClaw).
