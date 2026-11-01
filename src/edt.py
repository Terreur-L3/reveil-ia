"""Emploi du temps : les cours que tu m'as montrés (photo, capture d'écran) ou écrits, rangés jour par jour.

Fichier : memoire/emploi_du_temps.yaml

    mis_a_jour: '2026-10-06'
    semaine_type:                      # ce qui se répète chaque semaine
      lun: [{debut: '09:00', fin: '12:00', cours: Étalonnage, salle: '204', prof: M. Martin}]
      mar: []                          # jour vu sur l'image, sans cours
    dates:                             # exceptions pour une semaine précise (« cette semaine seulement »)
      '2026-10-12': [...]

Un jour absent veut dire « je ne sais pas » (le podcast n'en dit rien) ; un jour vide veut dire « pas de cours ».
Le texte dit dans le podcast est fabriqué ICI, avec des modèles de phrases, sans IA. Il évite les redites : un même cours
en deux morceaux (matin et après-midi) n'est dit qu'une fois, et la salle n'est dite que si elle t'apprend quelque chose
(pas « ma classe », pas la salle où se tiennent presque tous tes cours). (Si tu as une consigne sur la façon de dire tes
cours, perso_style.py le fait reformuler par Groq seul — jamais Gemini — puis contrôler : voir ce module.)
"""
from __future__ import annotations

import os
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
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


# --------------------------------------------------------------------------- « mardi : pas de cours »
_FREE_RE = re.compile(r"\b(?:pas de cours|aucun cours|plus de cours|sans cours|jour libre|libre|repos|rien)\b")
_THIS_WEEK_RE = re.compile(r"cette semaine\s+(?:seulement|uniquement)|seulement cette semaine|juste cette semaine|exceptionnel(?:lement)?")
_FR_DAYS = {"lundi": "lun", "lun": "lun", "mardi": "mar", "mar": "mar", "mercredi": "mer", "mer": "mer", "jeudi": "jeu", "jeu": "jeu",
            "vendredi": "ven", "ven": "ven", "samedi": "sam", "sam": "sam", "dimanche": "dim", "dim": "dim"}
_FREE_GLUE = {"le", "les", "la", "l", "du", "de", "des", "d", "tous", "toutes", "chaque", "et", "puis", "aussi", "ainsi", "que", "qu",
              "est", "sont", "c", "ce", "cette", "il", "n", "y", "a", "jour", "jours", "en", "au", "aux", "mon", "ma", "mes", "end"}


def parse_free_days(text) -> list[str] | None:
    """« mardi pas de cours », « le mercredi et le vendredi : libre », « pas de cours le week-end » → ['mar'], ['mer', 'ven'],
    ['sam', 'dim']. None si la phrase ne dit pas seulement ça (une heure, un cours, un mot inconnu) : c'est alors un emploi du
    temps à lire."""
    folded = _THIS_WEEK_RE.sub(" ", _fold(text))
    if not _FREE_RE.search(folded):
        return None
    rest = _FREE_RE.sub(" ", folded)
    if re.search(r"\d", rest):
        return None
    days: set[str] = set()
    for word in re.findall(r"[a-z]+", rest):
        key = word[:-1] if word.endswith("s") and word[:-1] in _FR_DAYS else word
        if key in _FR_DAYS:
            days.add(_FR_DAYS[key])
        elif key in ("weekend", "week"):
            days.update(("sam", "dim"))
        elif key not in _FREE_GLUE:
            return None
    return [d for d in DAYS if d in days] or None


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


# Comment dire la salle : « auto » (seulement si elle t'apprend quelque chose), « toujours » ou « jamais ».
ROOM_MODES = ("auto", "toujours", "jamais")
LUNCH_GAP_MIN = 150        # deux morceaux du même cours séparés par moins que ça (la pause du midi) ne font qu'un cours
USUAL_ROOM_SHARE = 0.7     # « ta salle habituelle » : celle où se tiennent au moins 70 % de tes cours…
USUAL_ROOM_MIN = 3         # …quand au moins trois cours ont une salle

_GENERIC_ROOM_WORDS = {"ma", "ta", "la", "notre", "en", "dans", "salle", "de", "classe", "habituelle", "habituel"}
_PERIOD = r"(?:matin[ée]e|matin|apr[èe]s[\s-]?midi|aprem)"
_PERIOD_PAREN_RE = re.compile(r"\s*[(\[]\s*(?:le\s+|l['’])?" + _PERIOD + r"\s*[)\]]", re.IGNORECASE)
_PERIOD_PREFIX_RE = re.compile(r"^\s*" + _PERIOD + r"\s*[:/\-–—]\s*", re.IGNORECASE)
_PERIOD_SUFFIX_RE = re.compile(r"\s*[:/\-–—]\s*" + _PERIOD + r"\s*$", re.IGNORECASE)


def clean_title(title: str) -> str:
    """« Lumière (matin) », « Après-midi : Lumière » → « Lumière » : l'heure dit déjà si c'est le matin ou l'après-midi."""
    text = str(title or "")
    cleaned = _PERIOD_PAREN_RE.sub("", text)
    cleaned = _PERIOD_PREFIX_RE.sub("", cleaned)
    cleaned = _PERIOD_SUFFIX_RE.sub("", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -–—:/")
    return cleaned if len(cleaned) >= 2 else text.strip()


def is_generic_room(room: str) -> bool:
    """« Ma classe », « En classe », « Salle de classe » : ce n'est pas un lieu qu'on a besoin d'entendre."""
    words = re.findall(r"[a-z0-9]+", _fold(room))
    return bool(words) and "classe" in words and set(words) <= _GENERIC_ROOM_WORDS


def _room_key(room: str) -> str:
    """Clé de comparaison : « Salle 204 », « salle 204 » et « 204 » sont la même salle."""
    return re.sub(r"^(?:salle\s+|s\.\s*)", "", _fold(room)).strip()


def usual_room(data: dict) -> str:
    """La salle (clé de comparaison) où se tiennent presque tous tes cours, ou « » s'il n'y en a pas."""
    counts: Counter = Counter()
    for courses in data["semaine_type"].values():
        for course in courses:
            room = course.get("salle", "")
            if room and not is_generic_room(room):
                counts[_room_key(room)] += 1
    total = sum(counts.values())
    if total < USUAL_ROOM_MIN:
        return ""
    key, n = counts.most_common(1)[0]
    return key if n >= total * USUAL_ROOM_SHARE else ""


def _room_to_say(room: str, mode: str, usual: str) -> str:
    if not room or mode == "jamais" or is_generic_room(room):
        return ""
    if mode == "auto" and usual and _room_key(room) == usual:
        return ""
    return _room_spoken(room)


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


@dataclass
class _Item:
    title: str
    room: str
    spans: list[list]          # [[début, fin ou None], …] : un même cours peut avoir plusieurs morceaux dans la journée


def _compatible_rooms(a: str, b: str) -> bool:
    if not a or not b or is_generic_room(a) or is_generic_room(b):
        return True
    return _room_key(a) == _room_key(b)


def _group(courses: list[dict]) -> list[_Item]:
    """Les cours du jour, un par cours : le même cours en deux morceaux (matin, après-midi) n'est dit qu'une fois."""
    items: list[_Item] = []
    for course in courses:
        title = clean_title(course["cours"])
        room = course.get("salle", "")
        last = items[-1] if items else None
        if last is not None and _fold(last.title) == _fold(title) and _compatible_rooms(last.room, room):
            if room and (not last.room or is_generic_room(last.room)):
                last.room = room
            span = last.spans[-1]
            end = course.get("fin")
            if span[1] and end and _minutes(course["debut"]) - _minutes(span[1]) <= LUNCH_GAP_MIN:
                span[0], span[1] = min(span[0], course["debut"]), max(span[1], end)      # la pause du midi ne coupe pas le cours
            else:
                last.spans.append([course["debut"], end])
            continue
        items.append(_Item(title, room, [[course["debut"], course.get("fin")]]))
    return items


def _spans_text(spans: list[list]) -> str:
    return ", puis ".join(f"de {_fr_time(a)} à {_fr_time(b)}" if b else f"à {_fr_time(a)}" for a, b in spans)


def _spoken_item(item: _Item, mode: str, usual: str) -> str:
    room = _room_to_say(item.room, mode, usual)
    return f"{item.title} {_spans_text(item.spans)}" + (f", {room}" if room else "")


def spoken_detail(data: dict, today: date, salle: str = "auto") -> tuple[str, list[str]]:
    """(les phrases du podcast sur les cours du jour, les titres des cours cités). Vide s'il n'y a rien d'utile à dire.
    `salle` : « auto » (dite seulement quand elle change), « toujours » ou « jamais »."""
    courses = courses_for(data, today)
    if courses is None:
        tomorrow = courses_for(data, today + timedelta(days=1)) if today.weekday() == 6 else None
        return _sunday_hint(tomorrow), ([clean_title(tomorrow[0]["cours"])] if tomorrow else [])
    if not courses:
        return ("Pas de cours aujourd'hui." if today.weekday() < 5 else ""), []
    mode = salle if salle in ROOM_MODES else "auto"
    usual = usual_room(data) if mode == "auto" else ""
    items = _group(courses)
    titles = [item.title for item in items]
    if len(items) == 1:
        return f"Côté cours, aujourd'hui : {_spoken_item(items[0], mode, usual)}.", titles
    if len(items) <= MAX_LISTED:
        return "Côté cours, aujourd'hui : " + ", puis ".join(_spoken_item(i, mode, usual) for i in items) + ".", titles
    first, last_end = items[0], max(c.get("fin") or c["debut"] for c in courses)
    room = _room_to_say(first.room, mode, usual)
    start = _fr_time(first.spans[0][0])
    return (f"Aujourd'hui, tu as {len(items)} cours, de {start} à {_fr_time(last_end)}. "
            f"Le premier : {first.title} à {start}" + (f", {room}" if room else "") + "."), titles


def spoken(data: dict, today: date, salle: str = "auto") -> str:
    """Les phrases du podcast sur les cours du jour (vide s'il n'y a rien d'utile à dire)."""
    return spoken_detail(data, today, salle)[0]


def _sunday_hint(courses_tomorrow: list[dict] | None) -> str:
    if not courses_tomorrow:
        return ""
    first = courses_tomorrow[0]
    return f"Demain, ça reprend : premier cours à {_fr_time(first['debut'])}, {clean_title(first['cours'])}."


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
    lines.append("Pour corriger : renvoie une image plus nette (en fichier), ou écris /cours suivi de la journée à refaire "
                 "(ex. /cours lundi 9h-17h lumière : ça remplace tout le lundi). /cours mardi pas de cours · "
                 "/cours salle jamais · /cours effacer repart de zéro.")
    return "\n".join(lines)


def clear(edt: dict) -> None:
    edt["semaine_type"].clear()
    edt["dates"].clear()
    edt["mis_a_jour"] = ""
