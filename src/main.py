"""Point d'entrée du podcast : actus → tri → texte → voix.

Usage :
    python -m src.main              # texte + audio (+ envoi Telegram si configuré)
    python -m src.main --no-audio   # texte seulement (essai rapide : affiche le texte et le choix des actus)

Fichiers produits (dossier output/) : latest.txt (le texte), latest.mp3 (le podcast, envoyé dans Telegram),
reveil.mp3 (sonnerie de ~10 s + podcast, joué par le téléphone à l'heure du réveil), sonnerie.mp3 (sonnerie seule de
~45 s, jouée à la place si le podcast du jour n'est pas prêt), meta.json, choix.txt.

Codes de sortie : 0 = tout est bon, 1 = rien n'a pu être produit, 3 = texte OK mais audio raté,
4 = podcast de SECOURS publié (sans actus : bonjour + infos perso), pour te prévenir que quelque chose cloche.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from . import jingle, llm, news, perso, reglages, script_writer, triage, tts
from .fr import count_words, date_fr
from .memoire import Memoire

ROOT = Path(__file__).resolve().parent.parent
log = logging.getLogger("reveil")

EXIT_OK, EXIT_FAIL, EXIT_NO_AUDIO, EXIT_SECOURS = 0, 1, 3, 4


def load_env(path: Path) -> None:
    """Charge un fichier .env local (CLE=valeur) sans écraser les variables déjà définies."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def audio_duration(path: Path) -> int | None:
    try:
        from mutagen.mp3 import MP3

        return round(MP3(str(path)).info.length)
    except Exception:
        return None


def fallback_script(prenom: str, perso_text: str, now: datetime) -> str:
    """Podcast de secours, écrit par le programme (aucune IA) : le réveil ne finit jamais en silence."""
    parts = [f"{'Bonjour ' + prenom if prenom else 'Bonjour'} ! Nous sommes le {date_fr(now)}."]
    if perso_text.strip():
        parts.append(perso_text.strip())
    parts.append("Ce matin, je n'ai pas pu préparer les actus. Ce n'est pas grave : je réessaie demain. Belle journée !")
    return "\n\n".join(parts)


def _send_to_telegram(mp3: Path, cfg: dict, now: datetime, duration: int | None, secours: bool = False,
                      silent: bool = False, voix_note: str = "") -> bool | None:
    """Dépose le podcast dans la conversation Telegram. None = pas configuré, False = échec."""
    if not (cfg.get("telegram") or {}).get("envoyer_podcast", True):
        return None
    try:
        from . import telegram

        bot = telegram.Bot.from_env(os.environ)
        if bot is None:
            return None
        caption = f"Podcast du {date_fr(now)}"
        if secours:
            caption = f"Podcast de secours du {date_fr(now)} (les actus n'ont pas pu être préparées)"
        if voix_note:
            caption += f"\n(🎙️ {voix_note})"
        bot.send_voice(mp3, caption=caption[:1000], duration=duration, silent=silent)
        return True
    except Exception as exc:  # le podcast est déjà publié : l'envoi n'est qu'un confort
        log.warning("Envoi Telegram impossible : %s", exc)
        return False


def generate(
    racine: Path,
    cfg: dict,
    out: Path,
    *,
    keys: dict[str, str],
    now: datetime,
    no_audio: bool = False,
    send_telegram: bool = True,
    silent: bool = False,
    origine: str = "",
) -> int:
    out.mkdir(parents=True, exist_ok=True)
    for old in [*out.glob("latest.*"), out / "reveil.mp3", out / "meta.json", out / "choix.txt"]:  # jamais de fichiers périmés
        old.unlink(missing_ok=True)

    reveil_cfg = cfg.get("reveil") or {}
    if reveil_cfg.get("sonnerie", True):  # d'abord : même si tout le reste échoue, le réveil aura au moins sa sonnerie
        try:
            jingle.build_alarm(out / "sonnerie.mp3", seconds=float(reveil_cfg.get("sonnerie_secours_s", 45)))
        except Exception as exc:
            log.warning("Sonnerie de secours impossible : %s", exc)

    mem = Memoire(racine)
    settings = mem.load_reglages()
    cfg = reglages.apply_to_cfg(cfg, settings)  # durée, voix et vitesse choisies dans Telegram
    consignes: list[str] = []
    if (cfg.get("perso") or {}).get("consignes_dans_podcast", True):
        try:  # seules les consignes de style non personnelles accompagnent la rédaction (qui peut passer par Gemini)
            consignes = mem.consignes_for("podcast", allow_private=False)
        except Exception as exc:
            log.warning("Consignes ignorées : %s", exc)

    profile = triage.load_profile(racine)
    prenom = profile.get("prenom") or str(cfg.get("prenom") or "").strip()
    try:  # les infos perso ne doivent jamais empêcher le podcast
        perso_text, n_perso = perso.build_segment(racine, cfg.get("perso"), now.date())
    except Exception as exc:
        log.warning("Infos perso ignorées : %s", exc)
        perso_text, n_perso = "", 0

    res = None
    raison = ""
    try:
        items, feeds = news.fetch(cfg.get("actu", {}))
        if not items:
            for status in feeds:
                log.error(status.line())
            raise RuntimeError("Aucune actu récupérée (flux en panne ou articles trop anciens).")
        log.info("%d actus à examiner", len(items))

        res = triage.run(items, profile, cfg, keys, now)
        (out / "choix.txt").write_text(triage.report(res, now, feeds, profile), encoding="utf-8")
        log.info("Tri : %s, %d sujets", "ok" if res.ok else "REPLI sans tri", len(res.choices))

        script, model = script_writer.write_script(
            res.choices, cfg, keys, now,
            prenom=prenom, perso_text=perso_text, libre=res.libre, calme=res.calme, consignes=consignes,
        )
    except Exception as exc:
        raison = (" ; ".join(l.strip() for l in str(exc).splitlines() if l.strip()) or exc.__class__.__name__)[:300]
        log.error("Échec avant la création du texte : %s", exc)
        script, model = fallback_script(prenom, perso_text, now), "secours (sans IA)"
        log.warning("Je publie un podcast de secours (bonjour + infos perso).")
        header = (f"PODCAST DE SECOURS du {date_fr(now)}\n"
                  f"Les actus n'ont pas pu être préparées : {raison}\n"
                  "Ce podcast ne contient que le bonjour et tes infos perso (anniversaires, rappels).\n")
        previous = (out / "choix.txt")
        body = previous.read_text(encoding="utf-8") if previous.exists() else ""
        previous.write_text(header + ("\n" + body if body else ""), encoding="utf-8")

    (out / "latest.txt").write_text(script + "\n", encoding="utf-8")
    meta = {
        "date": now.date().isoformat(),
        "genere_a": now.isoformat(timespec="seconds"),
        "mots": count_words(script),
        "modele": model,
        "mode": "secours" if raison else "normal",
        "tri": {
            "ok": res.ok, "modele": res.modele, "actus": res.nb_actus,
            "retenues": len(res.choices), "calme": res.calme, "profil": profile.get("source", "?"),
        } if res is not None else {
            "ok": False, "modele": "", "actus": 0, "retenues": 0, "calme": False, "profil": profile.get("source", "?"),
        },
        "perso": {"lignes": n_perso},
        "consignes": len(consignes),
        "origine": origine or "local",
        "audio": False,
    }
    if raison:
        meta["raison_secours"] = raison
    code = EXIT_SECOURS if raison else EXIT_OK

    if not no_audio:
        mp3 = out / "latest.mp3"
        try:
            voice = tts.synthesize(script, cfg.get("voix", {}), mp3)
            meta.update(audio=True, voix=voice, duree_s=audio_duration(mp3))
            note = getattr(tts, "last_note", lambda: "")()
            if note:                      # Gemini n'a pas pu servir : on le voit dans meta.json et dans Telegram
                meta["voix_note"] = note
                log.warning("Voix : %s", note)
            if (cfg.get("reveil") or {}).get("sonnerie", True):  # sonnerie + podcast, pour le téléphone
                try:
                    jingle.build(mp3, out / "reveil.mp3", seconds=float((cfg.get("reveil") or {}).get("sonnerie_s", 10)))
                    meta["reveil_mp3"] = True
                except Exception as exc:  # le podcast seul reste valable
                    log.warning("Sonnerie impossible, le podcast seul sera joué : %s", exc)
                    (out / "reveil.mp3").unlink(missing_ok=True)
        except Exception as exc:
            log.error("Échec de la synthèse vocale : %s", exc)
            mp3.unlink(missing_ok=True)
            code = EXIT_NO_AUDIO
        else:
            if send_telegram:
                sent = _send_to_telegram(mp3, cfg, now, meta.get("duree_s"), secours=bool(raison), silent=silent,
                                         voix_note=meta.get("voix_note", ""))
                if sent is not None:
                    meta["telegram"] = sent

    (out / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if no_audio:  # en essai local on affiche tout ; jamais dans les logs automatiques
        print("\n----- TEXTE DU PODCAST -----\n" + script + "\n----------------------------")
        print("\n----- CHOIX DES ACTUS -----\n" + (out / "choix.txt").read_text(encoding="utf-8"))
    log.info("Terminé : %s", json.dumps(meta, ensure_ascii=False))
    return code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Génère le podcast du matin.")
    parser.add_argument("--racine", default=str(ROOT), help="dossier du projet (config.yaml, moi.yaml, memoire/)")
    parser.add_argument("--config", default=None)
    parser.add_argument("--out", default=None)
    parser.add_argument("--no-audio", action="store_true", help="n'écrit que le texte")
    parser.add_argument("--no-telegram", action="store_true", help="n'envoie rien dans Telegram")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    racine = Path(args.racine).resolve()
    load_env(racine / ".env")
    config_path = Path(args.config) if args.config else racine / "config.yaml"
    cfg = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    now = datetime.now(ZoneInfo(cfg.get("fuseau", "Europe/Paris")))
    out = Path(args.out) if args.out else racine / "output"

    return generate(
        racine, cfg, out,
        keys=llm.keys_from_env(os.environ), now=now,
        no_audio=args.no_audio, send_telegram=not args.no_telegram,
        silent=os.environ.get("PODCAST_SILENT", "").strip().lower() in ("1", "true", "oui", "yes"),
        origine=os.environ.get("PODCAST_ORIGINE", "").strip()[:20],
    )


if __name__ == "__main__":
    sys.exit(main())
