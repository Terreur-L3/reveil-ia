"""Petits utilitaires de français : date parlée, comptage de mots."""
from __future__ import annotations

from datetime import date

JOURS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
MOIS = [
    "janvier", "février", "mars", "avril", "mai", "juin",
    "juillet", "août", "septembre", "octobre", "novembre", "décembre",
]


def date_fr(dt: date) -> str:
    quantieme = "1er" if dt.day == 1 else str(dt.day)
    return f"{JOURS[dt.weekday()]} {quantieme} {MOIS[dt.month - 1]} {dt.year}"


def count_words(text: str) -> int:
    return len(text.split())


def duree_fr(seconds: float) -> str:
    """45 s · 4 min 10 · 1 h 02"""
    s = int(round(seconds or 0))
    if s < 60:
        return f"{s} s"
    if s < 3600:
        m, r = divmod(s, 60)
        return f"{m} min" + (f" {r:02d}" if r else "")
    h, rest = divmod(s, 3600)
    return f"{h} h {rest // 60:02d}"


def jour_court_fr(d: date) -> str:
    """vendredi 9 octobre"""
    return f"{JOURS[d.weekday()]} {'1er' if d.day == 1 else d.day} {MOIS[d.month - 1]}"
