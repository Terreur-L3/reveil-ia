"""Le « garde » du workflow podcast : faut-il vraiment générer un podcast maintenant ?

Les exécutions PLANIFIÉES de GitHub peuvent arriver des heures en retard. Ce garde évite qu'elles refassent (et renvoient
dans Telegram) un podcast déjà fait, ou qu'elles prennent la place de la sonnette quand elle est installée.

    python -m src.garde --event schedule --cron "17 3 * * *" --meta /chemin/meta.json

Écrit « skip=true » ou « skip=false » (et la raison) pour le workflow. Un lancement à la main, par la sonnette ou
par /podcast n'est JAMAIS sauté.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# Les deux horaires planifiés de podcast.yml : le premier est le « tôt le matin » (comme avant), le second est le filet
# de sécurité de fin de matinée, qui ne sert que si rien n'a été fait.
NET_CRON_MARK = "47 6 * * *"


def decide(event: str, cron: str, meta: dict | None, today: date) -> tuple[bool, str]:
    """(sauter ?, raison). `meta` = le meta.json du dernier podcast publié (ou None)."""
    if event != "schedule":
        return False, "lancement volontaire (main, sonnette ou Telegram)"
    meta = meta or {}
    last_day = str(meta.get("date") or "")
    if last_day == today.isoformat() and meta.get("mode") != "secours" and meta.get("audio"):
        return True, "le podcast d'aujourd'hui existe déjà"
    if cron.strip() == NET_CRON_MARK:
        return False, "filet de sécurité : aucun podcast complet aujourd'hui"
    # Exécution « tôt le matin » : si la sonnette a fait celui d'hier, c'est elle qui s'occupe d'aujourd'hui
    try:
        gap = (today - date.fromisoformat(last_day)).days
    except ValueError:
        gap = 99
    if meta.get("origine") == "sonnette" and 0 <= gap <= 1:
        return True, "la sonnette s'occupe du podcast (le filet de fin de matinée passera si elle échoue)"
    return False, "pas de podcast aujourd'hui"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Faut-il générer le podcast maintenant ?")
    parser.add_argument("--event", default=os.environ.get("GITHUB_EVENT_NAME", ""))
    parser.add_argument("--cron", default="")
    parser.add_argument("--meta", default="")
    parser.add_argument("--tz", default="Europe/Paris")
    args = parser.parse_args(argv)

    meta = None
    if args.meta:
        try:
            meta = json.loads(Path(args.meta).read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError, OSError):
            meta = None
    today = datetime.now(ZoneInfo(args.tz)).date()
    skip, reason = decide(args.event, args.cron, meta if isinstance(meta, dict) else None, today)
    print(f"skip={'true' if skip else 'false'}")
    print(f"raison : {reason}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
