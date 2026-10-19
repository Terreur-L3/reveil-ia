"""Infos perso simples (anniversaires, rappels, cours du jour) : phrases fabriquées par le programme, sans IA.

Les phrases sont fabriquées ici, avec des modèles de texte, puis insérées dans le podcast.
Aucun nom, aucune date et aucun cours de ces fichiers ne part vers un modèle de rédaction ou de tri de Gemini (seule la voix
Gemini TTS entend le podcast terminé : voir le README). Vers Groq, seulement si tu as donné une consigne sur la façon de les
dire (« cite mes cours sans la salle ») : voir perso_style.py, qui reformule puis CONTRÔLE le résultat et garde le texte d'ici
en cas de doute.
"""
from __future__ import annotations

import hashlib
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import yaml

from . import edt

log = logging.getLogger(__name__)


@dataclass
class Event:
    delta: int   # nombre de jours avant l'événement (0 = aujourd'hui)
    kind: str    # "anniversaire" ou "rappel"
    text: str
    anchor: str = ""   # ce qu'une reformulation ne doit jamais perdre : le nom de la personne, le texte du rappel


@dataclass
class Segment:
    """Le texte perso du jour, et les mots-clés qui le prouvent (voir perso_style : une reformulation doit les garder)."""
    text: str = ""
    n: int = 0
    anchors: list[tuple[str, str]] = field(default_factory=list)   # (« nom » | « rappel » | « cours », texte)


def _load_list(path: Path) -> list[dict]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except Exception as exc:
        log.warning("%s ignoré (illisible) : %s", path.name, exc)
        return []
    if not isinstance(data, list):
        return []
    return [d for d in data if isinstance(d, dict)]


def _name(value) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:40]


def _sentence(value) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().rstrip(" .!?;:")[:160]


def _pick(options: list[str], seed: str) -> str:
    """Choix stable (même jour, même personne → même phrase) parmi plusieurs formulations."""
    digest = int(hashlib.sha256(seed.encode("utf-8")).hexdigest(), 16)
    return options[digest % len(options)]


def _int_set(value, default: set[int]) -> set[int]:
    if isinstance(value, list):
        try:
            return {int(v) for v in value}
        except (TypeError, ValueError):
            pass
    return default


def _safe_date(year: int, month: int, day: int) -> date:
    try:
        return date(year, month, day)
    except ValueError:
        if month == 2 and day == 29:  # anniversaire du 29 février une année non bissextile
            return date(year, 2, 28)
        raise


def next_occurrence(day: int, month: int, today: date) -> date:
    this_year = _safe_date(today.year, month, day)
    return this_year if this_year >= today else _safe_date(today.year + 1, month, day)


def _as_date(value) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError:
        return None


# --------------------------------------------------------------------------- anniversaires
def _birthday_text(nom: str, delta: int, age: int | None, seed: str) -> str:
    if delta == 0:
        if age:
            options = [
                f"Aujourd'hui, {nom} fête ses {age} ans : pense à lui écrire un petit mot.",
                f"C'est l'anniversaire de {nom} aujourd'hui, pour ses {age} ans : un petit message lui fera plaisir.",
            ]
        else:
            options = [
                f"Aujourd'hui, c'est l'anniversaire de {nom} : pense à lui écrire un petit mot.",
                f"N'oublie pas : c'est l'anniversaire de {nom} aujourd'hui.",
            ]
    elif delta == 1:
        if age:
            options = [f"Demain, {nom} fête ses {age} ans.", f"Pense-y : demain, {nom} fête ses {age} ans."]
        else:
            options = [f"Demain, c'est l'anniversaire de {nom}.", f"Pense-y : demain, c'est l'anniversaire de {nom}."]
    else:
        options = [
            f"Dans {delta} jours, c'est l'anniversaire de {nom} : il est encore temps de penser à une petite attention."
        ]
    return _pick(options, f"{seed}|{nom}|{delta}")


def birthday_events(entries: list[dict], today: date, days_before: set[int]) -> list[Event]:
    events: list[Event] = []
    for e in entries:
        nom = _name(e.get("nom"))
        try:
            jour, mois = int(e["jour"]), int(e["mois"])
            occ = next_occurrence(jour, mois, today)
        except (KeyError, TypeError, ValueError):
            log.warning("Une entrée d'anniversaire est ignorée (jour ou mois invalide)")
            continue
        if not nom:
            continue
        delta = (occ - today).days
        if delta not in days_before:
            continue
        age = None
        try:
            if e.get("annee") not in (None, ""):
                age = occ.year - int(e["annee"])
        except (TypeError, ValueError):
            age = None
        if age is not None and not 1 <= age <= 120:
            age = None
        events.append(Event(delta, "anniversaire", _birthday_text(nom, delta, age, today.isoformat()), nom))
    return events


# --------------------------------------------------------------------------- rappels
def reminder_events(entries: list[dict], today: date, days_before: set[int]) -> list[Event]:
    events: list[Event] = []
    for e in entries:
        texte = _sentence(e.get("texte"))
        when = _as_date(e.get("date"))
        if not texte or when is None:
            continue
        delta = (when - today).days
        if delta < 0 or delta not in _int_set(e.get("prevenir"), days_before):
            continue
        if delta == 0:
            line = f"Pour aujourd'hui : {texte}."
        elif delta == 1:
            line = f"Demain : {texte}."
        else:
            line = f"Dans {delta} jours : {texte}."
        events.append(Event(delta, "rappel", line, texte))
    return events


# --------------------------------------------------------------------------- assemblage
def _key(value) -> str:
    """Clé de comparaison : sans accents ni majuscules ni ponctuation."""
    text = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode()
    return re.sub(r"\W+", " ", text.lower()).strip()


def merge_birthdays(manual: list[dict], auto: list[dict]) -> list[dict]:
    """Tes fichiers écrits à la main l'emportent ; les anniversaires appris par le journal complètent."""
    known = {_key(e.get("nom")) for e in manual}
    return manual + [e for e in auto if _key(e.get("nom")) not in known]


def merge_reminders(manual: list[dict], auto: list[dict]) -> list[dict]:
    known = {(_key(e.get("texte")), str(e.get("date"))) for e in manual}
    return manual + [e for e in auto if (_key(e.get("texte")), str(e.get("date"))) not in known]


def build_segment_detail(racine: Path, cfg: dict | None, today: date) -> Segment:
    """Le texte perso du jour (vide s'il n'y a rien à dire), avec ses mots-clés. Aucune IA : voir perso_style pour la reformulation."""
    cfg = cfg or {}
    mem = Path(racine) / "memoire"
    birthdays = merge_birthdays(_load_list(mem / "anniversaires.yaml"), _load_list(mem / "anniversaires_auto.yaml"))
    reminders = merge_reminders(_load_list(mem / "rappels.yaml"), _load_list(mem / "rappels_auto.yaml"))
    events = birthday_events(
        birthdays, today, _int_set(cfg.get("anniversaires_jours_avant"), {7, 1, 0})
    ) + reminder_events(reminders, today, _int_set(cfg.get("rappels_jours_avant"), {3, 1, 0}))
    events.sort(key=lambda ev: (ev.delta, ev.kind != "anniversaire", ev.text))
    max_lines = int(cfg.get("max_lignes", 4))
    events = events[:max_lines]
    sentences = [ev.text for ev in events]
    anchors = [("nom" if ev.kind == "anniversaire" else "rappel", ev.anchor) for ev in events if ev.anchor]
    if max_lines > 0 and cfg.get("cours", True):  # « max_lignes: 0 » coupe tout le segment perso
        try:
            data = edt.load(mem / "emploi_du_temps.yaml")
            courses = edt.spoken(data, today)
            titles = [c["cours"] for c in (edt.courses_for(data, today) or []) if c.get("cours")]
        except Exception as exc:  # un fichier abîmé ne doit jamais empêcher le podcast
            log.warning("Emploi du temps ignoré : %s", exc)
            courses, titles = "", []
        if courses:
            sentences.append(courses)
            anchors += [("cours", t) for t in titles if _key(t) in _key(courses)]
    return Segment(" ".join(sentences), len(sentences), anchors)


def build_segment(racine: Path, cfg: dict | None, today: date) -> tuple[str, int]:
    """Retourne (texte à insérer dans le podcast, nombre de phrases). Vide s'il n'y a rien à dire."""
    seg = build_segment_detail(racine, cfg, today)
    return seg.text, seg.n
