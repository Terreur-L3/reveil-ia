"""Emploi du temps : les cours que tu m'as montrés (photo, capture d'écran) ou écrits, rangés jour par jour.

Fichier : memoire/emploi_du_temps.yaml

    mis_a_jour: '2026-10-06'
    semaine_type:                      # ce qui se répète chaque semaine
      lun: [{debut: '09:00', fin: '12:00', cours: Étalonnage, salle: '204', prof: M. Martin}]
      mar: []                          # jour vu sur l'image, sans cours
    dates:                             # exceptions pour une semaine précise (« cette semaine seulement »)
      '2026-10-12': [...]

Un jour absent veut dire « je ne sais pas » (le podcast n'en dit rien) ; un jour vide veut dire « pas de cours ».
Le texte dit dans le podcast est fabriqué ICI, avec des modèles de phrases, sans IA. (Si tu as une consigne sur la façon de
dire tes cours, perso_style.py le fait reformuler par Groq seul — jamais Gemini — puis contrôler : voir ce module.)
"""
from __future__ import annotations

import os
import re
import unicodedata
from datetime import date, timedelta
from pathlib import Path

import yaml

from .fr import JOURS, MOIS

DAYS = ["lun", "mar", "mer", "jeu", "ven", "sam", "dim"]
_DAY_NAMES = {
    "lundi": 0, "lun": 0, "monday": 0, "mon": 0,
    "mardi": 1, "mar": 1, "tuesday": 1, "tue": 1, "tues": 1,
    "mercredi": 2, "mer": 2, "wednesday": 2, "wed": 2,
    "jeudi": 3, "jeu": 3, "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
    "vendredi": 4, "ven": 4, "friday": 4, "fri": 4,
    "samedi": 5, "sam": 5, "saturday": 5, "sat": 5,
    "dimanche": 6, "dim": 6, "sunday": 6, "sun": 6,
}
KEEP_DATED_DAYS = 14
MAX_LISTED = 4          # au-delà, le podcast résume au lieu de tout lister
MAX_COURSES = 80


def _fold(text) -> str:
    flat = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode().lower()
    return re.sub(r"\s+", " ", flat).strip()


def norm_day(value) -> str | None:
    """« Lundi 05/10 », « lun. », « Monday » → « lun »."""
    for word in re.findall(r"[a-z]+", _fold(value)):
        if word in _DAY_NAMES:
            return DAYS[_DAY_NAMES[word]]
    return None


def norm_time(value) -> str | None:
    """« 9h », « 9h30 », « 09:00 », « 9 h 30 » → « HH:MM »."""
    m = re.search(r"(?<!\d)([01]?\d|2[0-3])\s*(?:h|:|\.)\s*([0-5]\d)?(?!\d)", _fold(value))
    if not m:
        return None
    return f"{int(m.group(1)):02d}:{m.group(2) or '00'}"


def _text(value, limit: int) -> str:
    clean = re.sub(r"https?://\S+|\[\[|\]\]", "", str(value or ""))
    return re.sub(r"\s+", " ", clean).strip()[:limit]


def clean_course(raw) -> tuple[str, dict] | None:
    """(jour, cours) si la ligne est exploitable, sinon None."""
    if not isinstance(raw, dict):
        return None
    day = norm_day(raw.get("jour"))
    start = norm_time(raw.get("debut"))
    title = _text(raw.get("intitule") or raw.get("cours") or raw.get("matiere"), 80)
    if not day or not start or len(title) < 2:
        return None
    end = norm_time(raw.get("fin"))
    if end is not None and end <= start:
        end = None
    course = {"debut": start, "cours": title}
    if end:
        course["fin"] = end
    room = _text(raw.get("salle"), 24)
    if room:
        course["salle"] = room
    teacher = _text(raw.get("enseignant") or raw.get("prof"), 40)
    if teacher:
        course["prof"] = teacher
    return day, course


# --------------------------------------------------------------------------- fichier
def empty() -> dict:
    return {"mis_a_jour": "", "semaine_type": {}, "dates": {}}


def _clean_list(items) -> list[dict]:
    out = []
    for c in items if isinstance(items, list) else []:
        if isinstance(c, dict) and norm_time(c.get("debut")) and _text(c.get("cours"), 80):
            keep = {"debut": norm_time(c["debut"]), "cours": _text(c["cours"], 80)}
            if norm_time(c.get("fin")):
                keep["fin"] = norm_time(c["fin"])
            for k, limit in (("salle", 24), ("prof", 40)):
                if _text(c.get(k), limit):
                    keep[k] = _text(c.get(k), limit)
            out.append(keep)
    return sorted(out, key=lambda c: (c["debut"], c["cours"]))


def normalize(data) -> dict:
    out = empty()
    if not isinstance(data, dict):
        return out
    out["mis_a_jour"] = str(data.get("mis_a_jour") or "")
    for key, courses in (data.get("semaine_type") or {}).items() if isinstance(data.get("semaine_type"), dict) else []:
        if key in DAYS:
            out["semaine_type"][key] = _clean_list(courses)
    for key, courses in (data.get("dates") or {}).items() if isinstance(data.get("dates"), dict) else []:
        try:
            date.fromisoformat(str(key))
        except ValueError:
            continue
        out["dates"][str(key)] = _clean_list(courses)
    return out


def load(path: Path | str) -> dict:
    try:
        return normalize(yaml.safe_load(Path(path).read_text(encoding="utf-8")))
    except (FileNotFoundError, OSError, yaml.YAMLError):
        return empty()


_HEADER = (
    "# Géré par ton journal vocal : envoie une photo de ton emploi du temps au bot, ou écris /cours.\n"
    "# Tu peux aussi corriger ce fichier à la main. Un jour absent = inconnu ; un jour vide ([]) = pas de cours.\n"
)


def save(path: Path | str, edt: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(path.name + ".part")
    body = yaml.safe_dump(normalize(edt), allow_unicode=True, sort_keys=False, default_flow_style=False, width=110)
    part.write_text(_HEADER + body, encoding="utf-8")
    os.replace(part, path)


# --------------------------------------------------------------------------- mise à jour
def monday_of(day: date) -> date:
    return day - timedelta(days=day.weekday())


def school_week(today: date) -> date:
    """Le lundi de la semaine de cours « en cours » : le dimanche, c'est déjà la semaine qui arrive qui t'intéresse."""
    monday = monday_of(today)
    return monday + timedelta(days=7) if today.weekday() == 6 else monday


def resolve_week(value, ref: date) -> date | None:
    """« 2026-10-05 » ou « 05/10 » → le lundi de cette semaine, à condition que ce soit proche de `ref`."""
    text = str(value or "").strip()
    found = None
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", text)
    if m:
        try:
            found = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    else:
        m = re.search(r"(\d{1,2})[/.-](\d{1,2})(?:[/.-](\d{2,4}))?", text)
        if m:
            day, month = int(m.group(1)), int(m.group(2))
            year = int(m.group(3)) if m.group(3) else ref.year
            year += 2000 if year < 100 else 0
            candidates = []
            for y in ([year] if m.group(3) else [year - 1, year, year + 1]):
                try:
                    candidates.append(date(y, month, day))
                except ValueError:
                    pass
            if candidates:
                found = min(candidates, key=lambda d: abs((d - ref).days))
    if found is None or not -45 <= (found - ref).days <= 200:
        return None
    return monday_of(found)


def merge(edt: dict, courses: list[dict], seen_days: list, *, week: date | None, recurring: bool, today: date) -> dict:
    """Range les cours lus. Les jours vus dans la nouvelle image REMPLACENT les anciens ; les autres jours restent."""
    by_day: dict[str, list[dict]] = {}
    for raw in courses[:MAX_COURSES]:
        cleaned = clean_course(raw)
        if cleaned:
            by_day.setdefault(cleaned[0], []).append(cleaned[1])
    days = {d for d in (norm_day(x) for x in seen_days or []) if d} | set(by_day)
    for day in days:
        todays = _clean_list(by_day.get(day, []))
        if recurring:
            edt["semaine_type"][day] = todays
        if week:
            edt["dates"][(week + timedelta(days=DAYS.index(day))).isoformat()] = todays
    edt["mis_a_jour"] = today.isoformat()
    prune(edt, today)
    ordered = [d for d in DAYS if d in days]
    return {"cours": sum(len(v) for v in by_day.values()), "jours": ordered, "semaine": week, "recurrent": recurring}


def prune(edt: dict, today: date) -> None:
    limit = (today - timedelta(days=KEEP_DATED_DAYS)).isoformat()
    for key in [k for k in edt["dates"] if k < limit]:
        del edt["dates"][key]


def count(edt: dict) -> int:
    return sum(len(v) for v in edt["semaine_type"].values())


def known(edt: dict) -> bool:
    return bool(edt["semaine_type"] or edt["dates"])


def courses_for(edt: dict, day: date) -> list[dict] | None:
    """Les cours de ce jour (liste vide = pas de cours), ou None si on ne sait pas."""
    dated = edt["dates"].get(day.isoformat())
    if dated is not None:
        return dated
    return edt["semaine_type"].get(DAYS[day.weekday()])


# --------------------------------------------------------------------------- texte
def _fr_time(hhmm: str) -> str:
    h, m = hhmm.split(":")
    return f"{int(h)} h" + (f" {m}" if m != "00" else "")


def _short_time(hhmm: str) -> str:
    h, m = hhmm.split(":")
    return f"{int(h)}h" + (m if m != "00" else "")


def _room_spoken(room: str) -> str:
    """« 204 » → « en salle 204 » ; « Salle 12 » → « en salle 12 » ; « Studio B » → « en Studio B »."""
    room = room.strip()
    if not room:
        return ""
    m = re.match(r"^(?:salle|s\.)\s*(.+)$", room, re.IGNORECASE)
    if m and (m.group(1)[0].isdigit() or len(m.group(1)) <= 4):      # un simple numéro ou code : « salle » est dit à part
        room = m.group(1)
    return f"en salle {room}" if (room[0].isdigit() or len(room) <= 4) else f"en {room}"


def _spoken_course(c: dict) -> str:
    when = f"de {_fr_time(c['debut'])} à {_fr_time(c['fin'])}" if c.get("fin") else f"à {_fr_time(c['debut'])}"
    room = _room_spoken(c.get("salle", ""))
    return f"{c['cours']} {when}" + (f", {room}" if room else "")


def spoken(edt: dict, today: date) -> str:
    """Les phrases du podcast sur les cours du jour (vide s'il n'y a rien d'utile à dire)."""
    courses = courses_for(edt, today)
    if courses is None:
        courses_tomorrow = courses_for(edt, today + timedelta(days=1)) if today.weekday() == 6 else None
        return _sunday_hint(courses_tomorrow)
    if not courses:
        return "Pas de cours aujourd'hui." if today.weekday() < 5 else ""
    if len(courses) == 1:
        return f"Côté cours, aujourd'hui : {_spoken_course(courses[0])}."
    if len(courses) <= MAX_LISTED:
        items = [_spoken_course(c) for c in courses]
        return "Côté cours, aujourd'hui : " + ", puis ".join(items) + "."
    first, last_end = courses[0], max(c.get("fin") or c["debut"] for c in courses)
    room = _room_spoken(first.get("salle", ""))
    return (f"Aujourd'hui, tu as {len(courses)} cours, de {_fr_time(first['debut'])} à {_fr_time(last_end)}. "
            f"Le premier : {first['cours']} à {_fr_time(first['debut'])}" + (f", {room}" if room else "") + ".")


def _sunday_hint(courses_tomorrow: list[dict] | None) -> str:
    if not courses_tomorrow:
        return ""
    first = courses_tomorrow[0]
    return f"Demain, ça reprend : premier cours à {_fr_time(first['debut'])}, {first['cours']}."


def _line(c: dict) -> str:
    when = f"{_short_time(c['debut'])}–{_short_time(c['fin'])}" if c.get("fin") else _short_time(c["debut"])
    extra = ", ".join(x for x in (c.get("salle", ""), c.get("prof", "")) if x)
    return f"{when} {c['cours']}" + (f" ({extra})" if extra else "")


def day_text(edt: dict, day: date, today: date) -> str | None:
    courses = courses_for(edt, day)
    if courses is None:
        return None
    mark = "👉 " if day == today else ""
    head = f"{mark}{JOURS[day.weekday()]} {day.day}"
    return f"{head} : " + (" · ".join(_line(c) for c in courses) if courses else "pas de cours")


def week_text(edt: dict, today: date) -> str:
    """Pour la commande /cours : la semaine en cours, jour par jour."""
    if not known(edt):
        return ("📅 Je ne connais pas encore ton emploi du temps.\n"
                "Envoie-moi une photo ou une capture d'écran (idéalement « en tant que fichier » pour qu'elle reste nette), "
                "ou écris-le : /cours lundi 9h-12h étalonnage salle 204 ; mardi …")
    monday = school_week(today)
    lines = [f"📅 Ton emploi du temps, semaine du {monday.day} {MOIS[monday.month - 1]}"
             + (f" (mis à jour le {edt['mis_a_jour'][8:10]}/{edt['mis_a_jour'][5:7]})" if edt["mis_a_jour"] else "")]
    shown = 0
    for i in range(7):
        text = day_text(edt, monday + timedelta(days=i), today)
        if text and (i < 5 or "pas de cours" not in text):
            lines.append(text)
            shown += 1
    if not shown:
        lines.append("(rien de connu pour cette semaine : renvoie-moi l'image)")
    lines.append("")
    lines.append("Pour corriger : renvoie une image plus nette (en fichier), ou écris /cours suivi des cours du jour à refaire. "
                 "/cours effacer repart de zéro.")
    return "\n".join(lines)


def clear(edt: dict) -> None:
    edt["semaine_type"].clear()
    edt["dates"].clear()
    edt["mis_a_jour"] = ""
