"""Écriture du texte du podcast à partir des infos du jour."""
from __future__ import annotations

import logging
import re
from datetime import datetime

from . import llm
from .news import Item

log = logging.getLogger(__name__)

JOURS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
MOIS = [
    "janvier", "février", "mars", "avril", "mai", "juin",
    "juillet", "août", "septembre", "octobre", "novembre", "décembre",
]

SYSTEM = """Tu es la voix d'un mini-podcast du matin, chaleureux et dynamique, qui réveille son auditeur en douceur. Tu écris UNIQUEMENT le texte qui sera lu à voix haute, rien d'autre.

Règles :
- Longueur : environ {mots} mots (≈ {minutes} minutes à l'oral). Ne dépasse jamais {max_mots} mots.
- Français parlé et naturel, phrases courtes, ton amical. Pas de markdown, pas de liste, pas de titre, pas d'émoji, pas d'URL, pas de didascalie entre parenthèses.
- Commence par un bonjour bref avec le jour et la date. Termine par une courte phrase positive pour bien démarrer la journée.
- Parmi les infos fournies, retiens les 4 à 6 plus importantes ou intéressantes et regroupe celles qui parlent du même sujet. Pas de remplissage.
- N'invente RIEN : utilise uniquement les informations fournies. Reste prudent si une info est incertaine.
- Écris les symboles et sigles de façon facile à lire à voix haute (« pour cent », « euros », etc.)."""


def date_fr(dt: datetime) -> str:
    quantieme = "1er" if dt.day == 1 else str(dt.day)
    return f"{JOURS[dt.weekday()]} {quantieme} {MOIS[dt.month - 1]} {dt.year}"


def count_words(text: str) -> int:
    return len(text.split())


def build_prompt(items: list[Item], perso: list[str], prenom: str, now: datetime) -> str:
    lines = [f"Nous sommes le {date_fr(now)}."]
    if prenom:
        lines.append(f"L'auditeur s'appelle {prenom} : utilise son prénom dans le bonjour.")
    if perso:
        lines += ["", "Infos personnelles à glisser en priorité, très brièvement :"]
        lines += [f"- {p}" for p in perso]
    lines += ["", "Actualités récentes :"]
    lines += [f"- {i.as_line()}" for i in items]
    lines += ["", "Écris maintenant le podcast."]
    return "\n".join(lines)


def clean_script(text: str) -> str:
    """Retire tout ce qui se lirait mal à voix haute (markdown, liens…)."""
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"[*#`>]+", "", text)
    text = re.sub(r"^\s*[-•]\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def write_script(
    items: list[Item],
    cfg: dict,
    api_key: str,
    now: datetime,
    perso: list[str] | None = None,
) -> tuple[str, str]:
    """Retourne (texte_du_podcast, modèle_utilisé)."""
    target = int(cfg.get("mots_cibles", 300))
    max_words = int(target * 1.25)
    system = SYSTEM.format(mots=target, minutes=f"{target / 150:g}", max_mots=max_words)

    llm_cfg = cfg.get("llm", {})
    models = llm_cfg.get("modeles") or ["gemini-3-flash-preview"]
    temperature = float(llm_cfg.get("temperature", 0.7))

    prompt = build_prompt(items, perso or [], cfg.get("prenom", "").strip(), now)
    text, model = llm.generate(
        prompt, system=system, api_key=api_key, models=models, temperature=temperature
    )
    text = clean_script(text)
    words = count_words(text)

    if words > max_words:  # un seul essai de raccourcissement
        log.info("Texte trop long (%d mots), je demande de raccourcir", words)
        shorter, _ = llm.generate(
            f"Raccourcis ce texte à environ {target} mots, même style parlé, "
            f"sans rien inventer. Renvoie uniquement le texte :\n\n{text}",
            system=system, api_key=api_key, models=models, temperature=temperature,
        )
        shorter = clean_script(shorter)
        if 0 < count_words(shorter) < words:
            text = shorter

    return text, model
