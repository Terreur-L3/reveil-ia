"""Point d'entrée : actus → texte du podcast → mp3.

Usage :
    python -m src.main              # texte + audio
    python -m src.main --no-audio   # texte seulement (test rapide, affiche le texte)

Codes de sortie : 0 = tout est bon, 1 = échec avant le texte, 3 = texte OK mais audio raté.
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

from . import news, script_writer, tts

ROOT = Path(__file__).resolve().parent.parent
log = logging.getLogger("reveil")

EXIT_OK, EXIT_FAIL, EXIT_NO_AUDIO = 0, 1, 3


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Génère le podcast du matin.")
    parser.add_argument("--config", default=str(ROOT / "config.yaml"))
    parser.add_argument("--out", default=str(ROOT / "output"))
    parser.add_argument("--no-audio", action="store_true", help="n'écrit que le texte")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    load_env(ROOT / ".env")
    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8")) or {}
    now = datetime.now(ZoneInfo(cfg.get("fuseau", "Europe/Paris")))

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for old in [*out.glob("latest.*"), out / "meta.json"]:  # jamais de fichiers périmés
        old.unlink(missing_ok=True)

    try:
        items = news.fetch_news(cfg.get("actu", {}))
        if not items:
            raise RuntimeError("Aucune actu récupérée (flux en panne ou articles trop anciens).")
        log.info("%d actus retenues", len(items))
        script, model = script_writer.write_script(
            items, cfg, os.environ.get("GEMINI_API_KEY", ""), now
        )
    except Exception as exc:
        log.error("Échec avant la création du texte : %s", exc)
        return EXIT_FAIL

    (out / "latest.txt").write_text(script + "\n", encoding="utf-8")
    meta = {
        "date": now.date().isoformat(),
        "genere_a": now.isoformat(timespec="seconds"),
        "mots": script_writer.count_words(script),
        "modele": model,
        "audio": False,
    }
    code = EXIT_OK

    if not args.no_audio:
        mp3 = out / "latest.mp3"
        try:
            voice = tts.synthesize(script, cfg.get("voix", {}), mp3)
            meta.update(audio=True, voix=voice, duree_s=audio_duration(mp3))
        except Exception as exc:
            log.error("Échec de la synthèse vocale : %s", exc)
            mp3.unlink(missing_ok=True)
            code = EXIT_NO_AUDIO

    (out / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    if args.no_audio:  # en test local on affiche le texte ; jamais dans les logs automatiques
        print("\n----- TEXTE DU PODCAST -----\n" + script + "\n----------------------------")
    log.info("Terminé : %s", json.dumps(meta, ensure_ascii=False))
    return code


if __name__ == "__main__":
    sys.exit(main())
