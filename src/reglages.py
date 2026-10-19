"""Tes réglages : l'heure du réveil (changeable par Telegram) et la façon dont le podcast est dit.

Tout est rangé dans memoire/reglages.json. Ce fichier est lu par la « sonnette » (le petit programme
Cloudflare qui déclenche le podcast à l'heure exacte), donc son format est volontairement simple :

    {
      "version": 1,
      "reveil": {
        "actif": true,
        "jours": {"lun": "06:30", "mar": "06:30", ..., "dim": null},   # null = pas de réveil ce jour-là
        "ponctuel": {"2026-10-08": "05:45", "2026-10-09": "off"},        # exceptions pour une date précise
        "maj": "2026-10-06T19:40:00+02:00"                                # quand tu as changé l'heure
      },
      "podcast": {"minutes": null, "voix": null, "vitesse": null,      # réglages « tous les jours »…
                  "jours": {"sam": {"minutes": 5}, "dim": {"minutes": 5}}},   # …et ceux d'un jour précis (clé absente si aucun)
      "sonnette_vue": "",
      "journal_offset": 0                                               # jusqu'où le journal a lu Telegram (sert à la sonnette)
    }

Les ordres sont compris par des règles fixes (pas par une IA) : une erreur d'interprétation ferait sonner
ton réveil à la mauvaise heure. Chaque changement est répété dans la réponse du bot, pour que tu le voies.
"""
from __future__ import annotations

import copy
import json
import os
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from .fr import JOURS, MOIS

DAYS = ["lun", "mar", "mer", "jeu", "ven", "sam", "dim"]
DEFAULTS: dict = {
    "version": 1,
    "reveil": {"actif": False, "jours": {d: None for d in DAYS}, "ponctuel": {}, "maj": ""},
    "podcast": {"minutes": None, "voix": None, "vitesse": None},
    "sonnette_vue": "",
    "journal_offset": 0,
}
VOICES = {
    "homme": "fr-FR-HenriNeural", "henri": "fr-FR-HenriNeural",
    "femme": "fr-FR-DeniseNeural", "denise": "fr-FR-DeniseNeural",
    "remy": "fr-FR-RemyMultilingualNeural", "vivienne": "fr-FR-VivienneMultilingualNeural",
    "eloise": "fr-FR-EloiseNeural",
}
# Les 30 voix de Gemini TTS : nom → (impression générale, genre). Elles parlent toutes français.
GEMINI_VOICES = {
    "Zephyr": ("lumineuse", "f"), "Puck": ("enjouée", "h"), "Charon": ("posée, informative", "h"),
    "Kore": ("ferme, assurée", "f"), "Fenrir": ("excitée, dynamique", "h"), "Leda": ("jeune", "f"),
    "Orus": ("ferme, grave", "h"), "Aoede": ("légère, aérienne", "f"), "Callirrhoe": ("détendue", "f"),
    "Autonoe": ("lumineuse", "f"), "Enceladus": ("soufflée, douce", "h"), "Iapetus": ("claire", "h"),
    "Umbriel": ("décontractée", "h"), "Algieba": ("lisse, rassurante", "h"), "Despina": ("lisse", "f"),
    "Erinome": ("claire", "f"), "Algenib": ("rauque", "h"), "Rasalgethi": ("informative", "h"),
    "Laomedeia": ("enjouée", "f"), "Achernar": ("douce", "f"), "Alnilam": ("ferme", "h"),
    "Schedar": ("régulière, neutre", "h"), "Gacrux": ("mûre", "f"), "Pulcherrima": ("directe", "f"),
    "Achird": ("amicale", "h"), "Zubenelgenubi": ("décontractée", "h"), "Vindemiatrix": ("douce", "f"),
    "Sadachbia": ("vive", "h"), "Sadaltager": ("savante", "h"), "Sulafat": ("chaleureuse", "f"),
}
GEMINI_BY_LOWER = {name.lower(): name for name in GEMINI_VOICES}
_CUSTOM_VOICE_RE = re.compile(r"voice_[A-Za-z0-9_-]{3,80}")
SPEEDS = {"tres lent": "-20%", "lent": "-10%", "normal": "+0%", "rapide": "+10%", "tres rapide": "+20%"}
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def _valid_minutes(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and 1 <= value <= 8 else None


def _valid_voice(value):
    if isinstance(value, str) and (re.fullmatch(r"[a-z]{2}-[A-Z]{2}-\w+Neural", value) or _valid_gemini_choice(value)):
        return value
    return None


def _valid_speed(value):
    return value if isinstance(value, str) and re.fullmatch(r"[+-]\d{1,2}%", value) else None


_PODCAST_CHECKS = {"minutes": _valid_minutes, "voix": _valid_voice, "vitesse": _valid_speed}


def _valid_gemini_choice(value: str) -> bool:
    """« gemini » (la voix de config.yaml), « gemini:Sulafat » ou « gemini:voice_… » (voix créée sur mesure)."""
    if value == "gemini":
        return True
    if not value.startswith("gemini:"):
        return False
    name = value[len("gemini:"):]
    return name in GEMINI_VOICES or bool(_CUSTOM_VOICE_RE.fullmatch(name))


# --------------------------------------------------------------------------- lecture / écriture
def normalize(data) -> dict:
    """Réglages complets et valides, même si le fichier est absent, abîmé ou édité à la main."""
    out = copy.deepcopy(DEFAULTS)
    if not isinstance(data, dict):
        return out
    rev = data.get("reveil")
    if isinstance(rev, dict):
        out["reveil"]["actif"] = bool(rev.get("actif"))
        jours = rev.get("jours")
        if isinstance(jours, dict):
            for d in DAYS:
                v = jours.get(d)
                out["reveil"]["jours"][d] = v if isinstance(v, str) and _TIME_RE.match(v) else None
        ponctuel = rev.get("ponctuel")
        if isinstance(ponctuel, dict):
            for k, v in ponctuel.items():
                try:
                    date.fromisoformat(str(k))
                except ValueError:
                    continue
                if v == "off" or (isinstance(v, str) and _TIME_RE.match(v)):
                    out["reveil"]["ponctuel"][str(k)] = v
        out["reveil"]["maj"] = str(rev.get("maj") or "")
    pod = data.get("podcast")
    if isinstance(pod, dict):
        for key, check in _PODCAST_CHECKS.items():
            out["podcast"][key] = check(pod.get(key))
        jours = pod.get("jours")
        if isinstance(jours, dict):
            clean: dict[str, dict] = {}
            for d in DAYS:
                entry = jours.get(d)
                if isinstance(entry, dict):
                    valid = {k: v for k, check in _PODCAST_CHECKS.items() if (v := check(entry.get(k))) is not None}
                    if valid:
                        clean[d] = valid
            if clean:   # la clé n'existe que s'il y a quelque chose : le format reste celui d'avant pour qui n'en a pas besoin
                out["podcast"]["jours"] = clean
    out["sonnette_vue"] = str(data.get("sonnette_vue") or "")
    offset = data.get("journal_offset")
    if isinstance(offset, int) and not isinstance(offset, bool) and offset >= 0:
        out["journal_offset"] = offset
    return out


def load(path: Path | str) -> dict:
    try:
        return normalize(json.loads(Path(path).read_text(encoding="utf-8")))
    except (FileNotFoundError, ValueError, OSError):
        return normalize(None)


def save(path: Path | str, settings: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(path.name + ".part")
    part.write_text(json.dumps(normalize(settings), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(part, path)


# --------------------------------------------------------------------------- l'horaire du réveil
def fr_time(hhmm: str) -> str:
    """06:30 → « 6 h 30 » ; 07:00 → « 7 h »."""
    h, m = hhmm.split(":")
    return f"{int(h)} h" + (f" {m}" if m != "00" else "")


def wake_for(settings: dict, day: date) -> str | None:
    """L'heure (« HH:MM ») du réveil pour ce jour, ou None s'il n'y en a pas."""
    rev = settings["reveil"]
    exception = rev["ponctuel"].get(day.isoformat())
    if exception is not None:
        return None if exception == "off" else exception
    if not rev["actif"]:
        return None
    return rev["jours"].get(DAYS[day.weekday()])


def next_wake(settings: dict, now: datetime) -> tuple[date, str] | None:
    """Le prochain réveil à partir de maintenant (aujourd'hui s'il n'est pas encore passé)."""
    for offset in range(0, 15):
        day = now.date() + timedelta(days=offset)
        hhmm = wake_for(settings, day)
        if hhmm is None:
            continue
        h, m = int(hhmm[:2]), int(hhmm[3:])
        if offset > 0 or (now.hour, now.minute) <= (h, m):
            return day, hhmm
    return None


def day_label(day: date, today: date) -> str:
    gap = (day - today).days
    base = f"{JOURS[day.weekday()]} {'1er' if day.day == 1 else day.day} {MOIS[day.month - 1]}"
    if gap == 0:
        return f"aujourd'hui ({base})"
    if gap == 1:
        return f"demain ({base})"
    return base


def _groups(jours: dict) -> list[tuple[int, int, str | None]]:
    out: list[tuple[int, int, str | None]] = []
    start = 0
    for i in range(1, 8):
        if i == 7 or jours[DAYS[i]] != jours[DAYS[start]]:
            out.append((start, i - 1, jours[DAYS[start]]))
            start = i
    return out


def week_summary(jours: dict) -> str:
    """« lun–ven 6 h 30 · sam 9 h · dim pas de réveil »"""
    if all(v is None for v in jours.values()):
        return "aucune heure choisie"
    parts = []
    for a, b, hhmm in _groups(jours):
        names = DAYS[a] if a == b else f"{DAYS[a]}–{DAYS[b]}"
        if (a, b) == (0, 6):
            names = "tous les jours"
        parts.append(f"{names} {fr_time(hhmm) if hhmm else 'pas de réveil'}")
    return " · ".join(parts)


def describe(settings: dict, now: datetime) -> str:
    rev = settings["reveil"]
    if all(v is None for v in rev["jours"].values()) and not rev["ponctuel"]:
        return "⏰ Aucun réveil réglé. Écris par exemple « /reveil 7h » (ou « /reveil 6h30 semaine »)."
    head = "⏰ Réveil : " + week_summary(rev["jours"])
    if not rev["actif"]:
        head = "⏰ Réveil DÉSACTIVÉ (réglage gardé : " + week_summary(rev["jours"]) + "). Pour le remettre : /reveil on"
    lines = [head]
    upcoming = sorted(k for k in rev["ponctuel"] if k >= now.date().isoformat())
    for k in upcoming[:5]:
        v = rev["ponctuel"][k]
        lines.append(f"• {day_label(date.fromisoformat(k), now.date())} : " + ("pas de réveil" if v == "off" else fr_time(v)))
    nxt = next_wake(settings, now)
    if nxt:
        lines.append(f"Prochain réveil : {day_label(nxt[0], now.date())} à {fr_time(nxt[1])}.")
    return "\n".join(lines)


def prune(settings: dict, today: date) -> None:
    """Oublie les exceptions de dates passées."""
    limit = (today - timedelta(days=1)).isoformat()
    ponctuel = settings["reveil"]["ponctuel"]
    for k in [k for k in ponctuel if k < limit]:
        del ponctuel[k]


# --------------------------------------------------------------------------- comprendre un ordre (français)
def _fold(text: str) -> str:
    flat = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode().lower()
    flat = re.sub(r"[’'`´\-_]", " ", flat)
    return re.sub(r"\s+", " ", flat).strip()


_HOUR_WORDS = {
    "zero": 0, "un": 1, "une": 1, "deux": 2, "trois": 3, "quatre": 4, "cinq": 5, "six": 6, "sept": 7, "huit": 8,
    "neuf": 9, "dix": 10, "onze": 11, "douze": 12, "treize": 13, "quatorze": 14, "quinze": 15, "seize": 16,
    "dix sept": 17, "dix huit": 18, "dix neuf": 19, "vingt": 20, "vingt et un": 21, "vingt deux": 22,
    "vingt trois": 23,
}
_MIN_WORDS = {
    "cinq": 5, "dix": 10, "quinze": 15, "vingt": 20, "vingt cinq": 25, "trente": 30, "trente cinq": 35,
    "quarante": 40, "quarante cinq": 45, "cinquante": 50, "cinquante cinq": 55,
}
_alt = lambda words: "|".join(sorted((re.escape(w) for w in words), key=len, reverse=True))  # noqa: E731
_SUFFIX = (
    r"(?:\s+et\s+(?P<et>demie?|quart)|\s+moins\s+(?:le\s+(?P<mq>quart)|(?P<mn>" + _alt(_MIN_WORDS) + r"|\d{1,2})))?"
)
_DIGIT_TIME_RE = re.compile(
    r"(?<![\d:])(?P<h>[01]?\d|2[0-3])\s*(?P<sep>heures?|h|:)\s*(?P<m>[0-5]\d)?(?!\d)" + _SUFFIX
)
_WORD_TIME_RE = re.compile(
    r"\b(?P<hw>" + _alt(_HOUR_WORDS) + r")\s+heures?\b(?:\s+(?P<mw>" + _alt(_MIN_WORDS) + r")\b|\s+(?P<md>[0-5]\d)(?!\d))?"
    + _SUFFIX
)
_NOON_RE = re.compile(r"\b(?P<w>midi|minuit)\b")
_PM_RE = re.compile(r"^\s*(?:du soir|de l apres midi|pm\b)")
_AM_RE = re.compile(r"^\s*(?:du matin|am\b)")
_RELATIVE_BEFORE_RE = re.compile(r"\b(dans|pendant|durant|depuis|il y a|plus de|moins de)\s*$")
_AT_BEFORE_RE = re.compile(r"\b(a|vers|pour|jusqu a|de)\s*$")


def _minutes_from_suffix(groups: dict) -> int | None:
    """Décale l'heure pour « et demie », « et quart », « moins le quart », « moins dix »."""
    if groups.get("et"):
        return 30 if groups["et"].startswith("demi") else 15
    if groups.get("mq"):
        return -15
    mn = groups.get("mn")
    if mn:
        return -(int(mn) if mn.isdigit() else _MIN_WORDS[mn])
    return None


def find_time(folded: str, start: int = 0, *, bare: bool = False) -> tuple[int, int, str] | None:
    """Premier horaire trouvé à partir de `start` : (début, fin, « HH:MM »). `folded` doit être passé par _fold."""
    candidates: list[tuple[int, int, int]] = []   # (début, fin, minutes depuis minuit)
    for m in _DIGIT_TIME_RE.finditer(folded, start):
        before = folded[:m.start()]
        if _RELATIVE_BEFORE_RE.search(before):
            continue
        compact = m.group("sep") in ("h", ":")
        if not compact and not _AT_BEFORE_RE.search(before):
            continue  # « 8 heures de sommeil » est une durée, pas une heure
        total = int(m.group("h")) * 60
        adjust = _minutes_from_suffix(m.groupdict())
        if m.group("m") is not None:
            total += int(m.group("m"))
        elif adjust is not None:
            total += adjust
        candidates.append((m.start(), m.end(), total))
    for m in _WORD_TIME_RE.finditer(folded, start):
        before = folded[:m.start()]
        if _RELATIVE_BEFORE_RE.search(before) or not _AT_BEFORE_RE.search(before):
            continue
        total = _HOUR_WORDS[m.group("hw")] * 60
        if m.group("mw"):
            total += _MIN_WORDS[m.group("mw")]
        elif m.group("md"):
            total += int(m.group("md"))
        else:
            adjust = _minutes_from_suffix(m.groupdict())
            if adjust is not None:
                total += adjust
        candidates.append((m.start(), m.end(), total))
    for m in _NOON_RE.finditer(folded, start):
        candidates.append((m.start(), m.end(), 12 * 60 if m.group("w") == "midi" else 0))
    if bare:
        m = re.fullmatch(r"\s*(?:a\s+)?([01]?\d|2[0-3])\s*", folded[start:])
        if m:
            candidates.append((start, len(folded), int(m.group(1)) * 60))
    if not candidates:
        return None
    begin, end, total = min(candidates)
    tail = folded[end:end + 24]
    if _PM_RE.match(tail) and total < 12 * 60:
        total += 12 * 60
    total %= 24 * 60
    return begin, end, f"{total // 60:02d}:{total % 60:02d}"


_WEEKDAY_WORDS = {name: i for i, name in enumerate(JOURS)}
_WEEKDAY_ALT = _alt(JOURS)
_MONTHS = {_fold(name): i + 1 for i, name in enumerate(MOIS)}
_MONTH_ALT = _alt(_MONTHS)
_DATE_RE = re.compile(r"\b(?:le|du)\s+(?:(?:" + _WEEKDAY_ALT + r")\s+)?(\d{1,2})(?:\s*er)?\s+(" + _MONTH_ALT + r")\b")
# « les mardis et jeudis », « le lundi et le mercredi », « tous les lundis » : tous les jours nommés comptent.
_DAYLIST = r"(?:" + _WEEKDAY_ALT + r")s?(?:\s*(?:,|et)\s*(?:le\s+|les\s+)?(?:" + _WEEKDAY_ALT + r")s?)*"
# (« le lundi 9h » = tous les lundis à 9h ; « le lundi 12 octobre » / « le lundi 12 » n'est pas un rythme mais une date)
_RECURRING_DAY_RE = re.compile(
    r"\b(?:tous\s+les|chaque|les|le)\s+(?P<days>" + _DAYLIST + r")\b(?!\s+\d{1,2}(?!\d)(?:er)?(?!\s*(?:h(?![a-z])|:|heures?\b)))(?!\s+prochain)"
)
_ONE_OFF_DAY_RE = re.compile(r"\b(?:ce\s+)?(" + _WEEKDAY_ALT + r")(?:\s+prochain)?\b")
_GLOBAL_RE = re.compile(
    r"\b(desormais|dorenavant|toujours|tout le temps|tous les jours|chaque jour|chaque matin|"
    r"tous les matins|quotidien\w*|maintenant)\b"
)
# « à partir de demain / de maintenant » = un réglage durable. « À partir de lundi » ne l'est pas ici : on ne sait pas
# changer l'heure « à partir d'une date », donc on applique le plus prudent (ce lundi seulement) et la réponse le dit.
_FROM_NOW_RE = re.compile(r"\ba partir (?:de|d) ?(?:maintenant|aujourd hui|demain)\b")
# Périodes, plages et « à partir de <date> » : le réveil ne sait régler qu'un jour précis ou un réglage durable. Plutôt
# que de transformer « 5h du 12 au 15 octobre » en réveil permanent, on le dit et on demande un jour à la fois.
_UNSUPPORTED_SCOPE_RE = re.compile(
    r"\bdu (?:\d{1,2}(?:er)?|" + _WEEKDAY_ALT + r")(?: [a-z]+)? au (?:\d{1,2}(?:er)?|" + _WEEKDAY_ALT + r")\b"
    r"|\bjusqu (?:a|au|en)\b|\bpendant\b|\bsemaine (?:prochaine|suivante)\b|\bcette semaine\b|\btoute la semaine\b"
    r"|\bsemaine du\b|\bweek ?ends? (?:prochain|suivant)\b|\bce (?:matin|soir|midi)\b|\bcette nuit\b|\bsauf\b|\ba part\b"
    r"|\ba partir (?:de|du|des|d)\s+(?!maintenant\b|aujourd hui\b|demain\b)|\bun jour sur\b|\btous les \d+ jours\b"
)
ASK_TEXT = ("🤔 Je ne sais pas régler une période ni une exception (« du 12 au 15 », « à partir de lundi », « sauf le dimanche »…). "
            "Je règle un jour à la fois : « mets le réveil à 5h le 12 octobre », « … lundi », « … en semaine » "
            "ou « … tous les jours » pour changer durablement.")
UNCLEAR_TEXT = ("🤔 Je n'ai pas compris à quels jours va chaque heure, et je ne devine pas : un réveil réglé de travers, ça se paie au "
                "petit matin. Dis-le-moi une heure à la fois : « mets le réveil à 7h en semaine », puis « mets le réveil à 9h le week-end ».")


def has_unsupported_scope(folded: str) -> bool:
    """`folded` doit être passé par _fold. « du lundi au vendredi » (= en semaine) est, lui, compris."""
    return bool(_UNSUPPORTED_SCOPE_RE.search(re.sub(r"\b(?:du )?lundi au vendredi\b", "en semaine", folded)))
_WEEKDAYS_RE = re.compile(
    r"\b(en semaine|la semaine(?! (?:prochaine|suivante))|jours? de cours|jours? ouvres?|jours? ouvrables?|(?:du )?lundi au vendredi)\b"
)
_WEEKEND_RECURRING_RE = re.compile(
    r"\b(?:les?|en|tous les)\s+(?:week ?ends?)\b|\b(?:les\s+)?samedis?\s+et\s+(?:les\s+)?dimanches?\b"
)
_THIS_WEEKEND_RE = re.compile(r"\b(?:ce|le prochain|ce prochain)\s+week ?end\b")
_PAST_RE = re.compile(
    r"\b(a sonne|ont sonne|ai mis|ai regle|avais mis|avais regle|avait sonne|etait|etais|hier|ai ecoute|ecoutais)\b"
)
# « podcast » seul est ambigu (« un podcast sur le trajet ») : on exige « le / mon / ce podcast ».
_NOUN_RE = re.compile(
    r"\b(?:reveil|alarme|lecture)\b|\b(?:le|mon|ton|ce|notre|du|au)\s+podcast\b|\breveille moi\b|"
    r"\bme reveill(?:e|es|ez|er)\b"
)
_REQUEST_RE = re.compile(
    r"\b(met|mets|mettre|mettez|regle|regles|regler|reglez|programme|programmes|programmer|change|changes|changer|"
    r"changez|modifie|modifies|modifier|decale|decales|decaler|avance|avances|avancer|recule|reculer|fixe|fixer|"
    r"cale|caler|passe|peux|peut|pouvez|pourrais|pourriez|veux|voudrais|voudrait|souhaite|aimerais|"
    r"aimerai|faut|faudrait|besoin|stp|svp|plait|lis|lire|lance|lancer|joue|jouer|diffuse|diffuser|"
    r"reveille moi|reveilles|reveillez|reveiller)\b|^(?:reveil|alarme|podcast)\s+a\b"
)
_CONJ_RE = re.compile(r",|\bet\b|\bpuis\b|\bmais\b|\bsinon\b")
_OFF_RE = re.compile(
    r"\b(?:pas de|sans|plus de)\s+(?:reveil|alarme|podcast)\b|\bne me reveill\w*\s+(?:pas|plus)\b|"
    r"\b(?:desactive|desactiver|desactivez|coupe|couper|coupez|supprime|supprimer|annule|annuler|arrete|arreter|"
    r"eteins|eteindre|stoppe|stopper)\b.{0,25}\b(?:reveil|alarme|podcast)\b|"
    r"\b(?:reveil|alarme|podcast)\b.{0,12}\b(?:off|desactive|coupe|annule)\b"
)
_ON_RE = re.compile(
    r"\b(?:reactive|reactiver|reactivez|remets|remettre|rallume|rallumer|active|activer|activez|relance|relancer)\b"
    r".{0,25}\b(?:reveil|alarme|podcast)\b"
)
_SHOW_RE = re.compile(
    r"\bquelle heure\b.{0,30}\b(?:reveil|alarme|podcast)\b|\b(?:reveil|alarme|podcast)\b.{0,30}\bquelle heure\b|"
    r"\ba quelle heure (?:est|sonne|je me reveille)\b"
)


@dataclass
class WakeRequest:
    # "set", "off", "on", "show", ou — quand on ne sait pas — "ask" (période / exception : ASK_TEXT) et "unclear"
    # (plusieurs heures sans jours clairs : UNCLEAR_TEXT). Dans les deux cas on le dit et on ne change rien.
    action: str
    time: str | None = None          # « HH:MM » pour "set"
    days: tuple[int, ...] | None = None   # jours de la semaine concernés (0 = lundi) ; None = tous
    on_date: date | None = None      # une date précise (exception) au lieu d'un réglage durable


def _next_weekday(today: date, weekday: int) -> date:
    gap = (weekday - today.weekday()) % 7 or 7
    return today + timedelta(days=gap)


def _next_date(today: date, day: int, month: int) -> date | None:
    for year in (today.year, today.year + 1):
        try:
            candidate = date(year, month, day)
        except ValueError:
            continue
        if candidate >= today:
            return candidate
    return None


def _scope(folded: str, today: date) -> tuple[tuple[int, ...] | None, date | None]:
    """(jours concernés, date précise). (None, None) = tous les jours, de façon durable."""
    folded = _FROM_NOW_RE.sub(" ", folded)  # « à partir de demain » = durable, pas « demain seulement »
    m = _DATE_RE.search(folded)
    if m:
        target = _next_date(today, int(m.group(1)), _MONTHS[m.group(2)])
        if target:
            return None, target
    if re.search(r"\bapres demain\b", folded):
        return None, today + timedelta(days=2)
    if re.search(r"\bdemain\b", folded):
        return None, today + timedelta(days=1)
    if re.search(r"\baujourd hui\b", folded):
        return None, today
    if _THIS_WEEKEND_RE.search(folded):
        saturday = today + timedelta(days=(5 - today.weekday()) % 7)
        return None, saturday  # le dimanche est traité par l'appelant (voir _expand_weekend)
    if _GLOBAL_RE.search(folded) and not _WEEKDAYS_RE.search(folded) and not _WEEKEND_RECURRING_RE.search(folded):
        return None, None
    if _WEEKDAYS_RE.search(folded):
        return (0, 1, 2, 3, 4), None
    if _WEEKEND_RECURRING_RE.search(folded):
        return (5, 6), None
    m = _RECURRING_DAY_RE.search(folded)
    if m:
        named = {_WEEKDAY_WORDS[w] for w in re.findall(_WEEKDAY_ALT, m.group("days"))}
        return tuple(sorted(named)), None
    m = _ONE_OFF_DAY_RE.search(folded)
    if m:
        return None, _next_weekday(today, _WEEKDAY_WORDS[m.group(1)])
    return None, None


def target_day(text: str, today: date) -> date | None:
    """« demain », « après-demain », « lundi », « le 12 octobre »… → la date visée (None si rien de clair)."""
    folded = _fold(text)
    return _scope(folded, today)[1] if folded else None


def _ambiguous_scope(folded: str) -> bool:
    """Un jour précis (« demain », « le 12 octobre ») ET un rythme (« en semaine », « le week-end »…) dans la même phrase :
    on ne sait pas lequel est visé (« à demain, pas de réveil le week-end »)."""
    folded = _FROM_NOW_RE.sub(" ", folded)
    precise = _DATE_RE.search(folded) or re.search(r"\b(?:apres demain|demain|aujourd hui)\b", folded)
    rhythm = (_WEEKDAYS_RE.search(folded) or _WEEKEND_RECURRING_RE.search(folded) or _RECURRING_DAY_RE.search(folded)
              or _GLOBAL_RE.search(folded))
    return bool(precise and rhythm)


def _requests_for_scope(action: str, time: str | None, folded: str, today: date) -> list[WakeRequest]:
    if _ambiguous_scope(folded):
        return [WakeRequest("ask")]
    days, on_date = _scope(folded, today)
    out = [WakeRequest(action, time, days, on_date)]
    if on_date and _THIS_WEEKEND_RE.search(folded):
        out.append(WakeRequest(action, time, None, on_date + timedelta(days=1)))
    return out


def _has_scope(folded: str) -> bool:
    """Un mot qui dit QUAND (un jour, une date, « en semaine », « le week-end », « tous les jours »…) ?"""
    folded = _FROM_NOW_RE.sub(" ", folded)
    return bool(
        _DATE_RE.search(folded) or re.search(r"\b(?:apres demain|demain|aujourd hui)\b", folded) or _THIS_WEEKEND_RE.search(folded)
        or _GLOBAL_RE.search(folded) or _WEEKDAYS_RE.search(folded) or _WEEKEND_RECURRING_RE.search(folded)
        or _RECURRING_DAY_RE.search(folded) or _ONE_OFF_DAY_RE.search(folded)
    )


def _set_requests(sentence: str, start: int, today: date) -> list[WakeRequest]:
    """Les « réveil à HH:MM » d'une phrase. Plusieurs horaires (« 7h en semaine et 9h le week-end ») : un ordre
    par morceau, dans l'ordre où ils sont dits. Seul le PREMIER morceau peut ne pas dire quels jours (« 7h, et le
    week-end 9h » = 7h partout, puis 9h le week-end) ; sinon on ne devine pas : « unclear »."""
    times: list[tuple[int, int, str]] = []
    pos = start
    while True:
        found = find_time(sentence, pos)
        if found is None:
            break
        times.append(found)
        pos = found[1]
    if not times or times[0][0] - start > 70:
        return []
    if len(times) == 1:
        return _requests_for_scope("set", times[0][2], sentence, today)
    cuts = [0]
    for prev, nxt in zip(times, times[1:]):
        seps = [m.start() for m in _CONJ_RE.finditer(sentence, prev[1], nxt[0])]
        if not seps:
            return [WakeRequest("unclear")]
        cuts.append(seps[-1])
    cuts.append(len(sentence))
    out: list[WakeRequest] = []
    for index, ((a, b), (_, _, hhmm)) in enumerate(zip(zip(cuts, cuts[1:]), times)):
        piece = sentence[a:b]
        reqs = _requests_for_scope("set", hhmm, piece, today)
        if reqs[0].action == "ask":
            return reqs
        if index > 0 and not _has_scope(piece):
            return [WakeRequest("unclear")]
        out += reqs
    return out


# Une phrase SANS « réveil » qui prolonge l'ordre de la phrase d'avant : « Et le week-end à 9h. » ou « Du 12 au 15 octobre,
# mets-le à 5h. ». Prudence : seulement après un ordre de la même note, et soit « Et… » + rien d'autre que jours/heure,
# soit un verbe d'ordre suivi de « le / la ».
_CONNECTIVE_RE = re.compile(r"^(?:et puis|et sinon|et|puis|sinon|mais|par contre|ensuite)\b[\s,]*")
_PRONOUN_ORDER_RE = re.compile(
    r"\b(?:met|mets|mettre|regle|regles|regler|change|changes|changer|decale|decales|decaler|fixe|fixer|cale|caler|"
    r"programme|programmes|programmer)\s+(?:le|la|l|ca|cela)\b"
)
_ELLIPSIS_WORDS = frozenset(
    "le la les l en a pour tous chaque jour jours semaine week end weekend demain apres aujourd hui ce du au de d prochain "
    "ou et".split()
) | {name for name in _WEEKDAY_WORDS} | {name + "s" for name in _WEEKDAY_WORDS} | set(_MONTHS)


def _is_elliptical(folded: str) -> bool:
    """« le week-end à 9h », « samedi et dimanche 10h » : rien d'autre que des jours et une heure."""
    found = find_time(folded, 0)
    if found is None:
        return False
    rest = folded[:found[0]] + " " + folded[found[1]:]
    return all(w in _ELLIPSIS_WORDS or w.isdigit() for w in re.findall(r"[a-z]+|\d+", rest))


def _follow_up_requests(sentence: str, today: date) -> list[WakeRequest]:
    stripped = _CONNECTIVE_RE.sub("", sentence, count=1)
    if not (_PRONOUN_ORDER_RE.search(sentence) or (stripped != sentence and _is_elliptical(stripped))):
        return []
    if find_time(sentence, 0) is None:
        return []
    if has_unsupported_scope(sentence):
        return [WakeRequest("ask")]
    if not _has_scope(sentence):
        return [WakeRequest("unclear")]      # « Et à 9h. » : 9h quand ? on ne devine pas
    return _set_requests(sentence, 0, today) or [WakeRequest("unclear")]


def find_wake_requests(text: str, today: date) -> list[WakeRequest]:
    """Les ordres de réveil contenus dans un message (texte ou vocal transcrit), dans l'ordre où ils sont dits.

    Volontairement strict : « demain je me lève à 6h pour le tournage » n'est PAS un ordre ; « mets mon
    réveil à 6h » en est un. Mieux vaut ignorer une phrase floue que dérégler un réveil.
    """
    requests: list[WakeRequest] = []
    for sentence in re.split(r"(?<=[.!?;])\s+|\n+", _fold(text)):
        if not sentence or _PAST_RE.search(sentence):
            continue
        if _SHOW_RE.search(sentence):
            requests.append(WakeRequest("show"))
            continue
        if _OFF_RE.search(sentence):
            requests += [WakeRequest("ask")] if has_unsupported_scope(sentence) else _requests_for_scope("off", None, sentence, today)
            continue
        if _ON_RE.search(sentence):
            requests.append(WakeRequest("on"))
            continue
        if not _NOUN_RE.search(sentence):
            if any(r.action != "show" for r in requests):        # la suite de l'ordre précédent ? (« Et le week-end à 9h. »)
                requests += _follow_up_requests(sentence, today)
            continue
        for noun in _NOUN_RE.finditer(sentence):
            if noun.group(0).startswith("me reveill") and sentence[max(0, noun.start() - 3):noun.start()] == "je ":
                continue  # « je me réveille à 7h » raconte une habitude, ce n'est pas une demande
            if not _REQUEST_RE.search(sentence):
                continue
            found = _set_requests(sentence, noun.end(), today)
            if not found:
                continue
            requests += [WakeRequest("ask")] if has_unsupported_scope(sentence) else found
            break
    return requests


def parse_wake_args(args: str, today: date) -> list[WakeRequest]:
    """Ce qui suit « /reveil » : « 6h30 », « 9h week-end », « demain 5h45 », « off », « on », « pas demain »…"""
    folded = _fold(args)
    if not folded:
        return [WakeRequest("show")]
    if has_unsupported_scope(folded) and (_OFF_RE.search(folded) or find_time(folded, 0, bare=True) or re.match(r"(?:off|non|pas)\b", folded)):
        return [WakeRequest("ask")]
    folded = re.sub(r"(?<!en )(?<!la )\bsemaine\b", "en semaine", folded)
    folded = re.sub(r"(?<!le )(?<!ce )(?<!les )(?<!en )(?<!tous les )\bweek ?ends?\b", "le week end", folded)
    first = folded.split()[0]
    if first in ("on", "oui", "active", "reactive", "reactiver", "activer"):
        return [WakeRequest("on")]
    if first in ("off", "non", "stop", "desactive", "desactiver", "pause", "arret", "annule", "coupe") or _OFF_RE.search(folded) \
            or re.match(r"pas\b", folded):
        return _requests_for_scope("off", None, folded, today)
    folded = "a " + folded                               # « /reveil 8 heures » : comme « à 8 heures » dans une phrase
    found = find_time(folded, 0, bare=True)
    if found is None:
        return []
    if find_time(folded, found[1]) is not None:         # « 7h30 semaine et 9h week-end » : mêmes règles que dans une phrase
        return _set_requests(folded, 0, today)
    return _requests_for_scope("set", found[2], folded, today)


# --------------------------------------------------------------------------- appliquer un ordre
def days_phrase(days: tuple[int, ...] | None) -> str:
    if days is None or len(days) == 7:
        return "tous les jours"
    if tuple(days) == (0, 1, 2, 3, 4):
        return "en semaine"
    if tuple(days) == (5, 6):
        return "le week-end"
    names = [f"le {JOURS[d]}" for d in days]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " et " + names[-1]


def apply_request(settings: dict, req: WakeRequest, when: datetime, stamp: datetime | None = None) -> str:
    """Modifie `settings` (sur place) et retourne la phrase courte qui dit ce qui a changé.

    `when` = maintenant ; `stamp` = l'instant où tu as écrit (c'est lui qui est noté dans « maj » : un réveil
    changé APRÈS son heure ne sonne pas le jour même)."""
    rev = settings["reveil"]
    today = when.date()
    stamp = stamp or when
    mark = stamp.isoformat(timespec="seconds")
    prune(settings, today)
    if req.action == "show":
        return describe(settings, when)
    if req.action == "ask":
        return ASK_TEXT
    if req.action == "unclear":
        return UNCLEAR_TEXT
    if req.action == "on":
        rev["actif"] = True
        rev["maj"] = mark
        if all(v is None for v in rev["jours"].values()):
            return "⏰ Réveil réactivé, mais aucune heure n'est choisie. Écris par exemple « /reveil 7h »."
        return "⏰ Réveil réactivé."
    if req.action == "off":
        rev["maj"] = mark
        if req.on_date:
            rev["ponctuel"][req.on_date.isoformat()] = "off"
            return f"😴 Pas de réveil {day_label(req.on_date, today)}."
        if req.days is not None:
            for d in req.days:
                rev["jours"][DAYS[d]] = None
            return f"😴 Plus de réveil {days_phrase(req.days)}."
        rev["actif"] = False
        return "😴 Réveil désactivé (pour le remettre : /reveil on)."
    assert req.time is not None  # "set"
    rev["maj"] = mark
    if req.on_date:
        rev["ponctuel"][req.on_date.isoformat()] = req.time
        return f"⏰ Réveil {day_label(req.on_date, today)} : {fr_time(req.time)} (les autres jours ne changent pas)."
    for d in (req.days if req.days is not None else tuple(range(7))):
        rev["jours"][DAYS[d]] = req.time
    rev["actif"] = True
    return f"⏰ Réveil {days_phrase(req.days)} : {fr_time(req.time)}."


# --------------------------------------------------------------------------- réglages du podcast
def parse_duree(text: str) -> float | None:
    """« 3 », « 2 minutes », « 90 secondes », « 1,5 » → minutes (entre 1 et 8)."""
    folded = _fold(text).replace(",", ".")
    m = re.search(r"(\d+(?:\.\d+)?)\s*(secondes?|sec|s|minutes?|min|m)?\b", folded)
    if not m:
        return None
    value = float(m.group(1))
    if m.group(2) and m.group(2).startswith("s"):
        value /= 60
    value = round(value * 2) / 2  # par demi-minute
    return value if 1 <= value <= 8 else None


def parse_voix(text: str) -> str | None:
    folded = _fold(text)
    custom = _CUSTOM_VOICE_RE.search(str(text))
    if custom:
        return f"gemini:{custom.group(0)}"
    for word in folded.split():
        if word in VOICES:
            return VOICES[word]
        if word in GEMINI_BY_LOWER:
            return f"gemini:{GEMINI_BY_LOWER[word]}"
        if word == "gemini":
            return "gemini"
    m = re.search(r"\b([a-z]{2}-[A-Z]{2}-\w+Neural)\b", str(text))
    return m.group(1) if m else None


def parse_vitesse(text: str) -> str | None:
    folded = _fold(text)
    for name in sorted(SPEEDS, key=len, reverse=True):
        if re.search(r"\b" + name + r"\b", folded):
            return SPEEDS[name]
    m = re.search(r"([+\-−–]?)\s*(\d{1,2})\s*%", str(text))   # le texte d'origine : _fold transforme « - » en espace
    if m:
        value = min(30, int(m.group(2)))
        return f"{-value if m.group(1) in ('-', '−', '–') else value:+d}%"
    return None


def validate_setting(key: str, value) -> tuple[str, object] | None:
    """Réglage proposé par l'IA (« parle plus lentement ») → (clé, valeur valide) ou None."""
    key = str(key or "").strip().lower()
    if key in ("duree", "duree_min", "minutes"):
        minutes = parse_duree(str(value))
        return ("minutes", minutes) if minutes else None
    if key == "voix":
        voice = parse_voix(str(value))
        return ("voix", voice) if voice else None
    if key == "vitesse":
        speed = parse_vitesse(str(value))
        return ("vitesse", speed) if speed else None
    return None


def voice_label(value: str) -> str:
    """Le nom que tu vois dans Telegram : « femme », « Sulafat (Gemini) », « fr-FR-RemyMultilingualNeural »…"""
    if value == "gemini":
        return "Gemini (voix par défaut)"
    if value.startswith("gemini:"):
        return f"{value[len('gemini:'):]} (Gemini)"
    return next((k for k, v in VOICES.items() if v == value and k in ("homme", "femme")), value)


def _tidy_podcast(p: dict) -> None:
    """Garde le dictionnaire des jours aussi petit que possible : rien qui répète le réglage « tous les jours »,
    et si les sept jours ont la même valeur, c'est simplement le réglage « tous les jours »."""
    jours = p.get("jours")
    if not jours:
        p.pop("jours", None)
        return
    for key in _PODCAST_CHECKS:
        values = [jours.get(d, {}).get(key) for d in DAYS]
        if all(v is not None for v in values) and len(set(values)) == 1:
            p[key] = values[0]
            for d in DAYS:
                jours[d].pop(key, None)
    for d in list(jours):
        for key in list(jours[d]):
            if jours[d][key] == p.get(key):
                del jours[d][key]
        if not jours[d]:
            del jours[d]
    if not jours:
        p.pop("jours", None)


def set_podcast(settings: dict, key: str, value, days: tuple[int, ...] | None = None, rest: bool = False) -> str:
    """Change un réglage du podcast et retourne la phrase qui le dit.

    `days=None` : tous les jours (efface au passage les réglages propres à un jour pour cette clé) ;
    `days=(5, 6)` : seulement ces jours-là ; `rest=True` : le réglage de base, SANS toucher aux jours déjà réglés
    (« 5 minutes le week-end, 3 sinon »)."""
    p = settings["podcast"]
    if days is not None and len(set(days)) >= 7:
        days = None
    where = ""
    if days is None:
        p[key] = value
        if rest:
            where = " les autres jours"
        elif p.get("jours"):
            for entry in p["jours"].values():
                entry.pop(key, None)
    else:
        for d in days:
            p.setdefault("jours", {}).setdefault(DAYS[d], {})[key] = value
        where = " " + days_phrase(days)
    _tidy_podcast(p)
    if key == "minutes":
        shown = f"{value:g}".replace(".", ",")
        return f"durée du podcast{where} : environ {shown} minute{'s' if value > 1 else ''}"
    if key == "voix":
        return f"voix{where} : {voice_label(value)}"
    return f"vitesse de la voix{where} : {value}"


def podcast_for(settings: dict, day: date | None) -> dict:
    """Durée, voix et vitesse qui valent CE jour-là : le réglage « tous les jours » complété par celui du jour de la semaine."""
    p = settings["podcast"]
    out = {key: p.get(key) for key in _PODCAST_CHECKS}
    if day is not None:
        out.update((p.get("jours") or {}).get(DAYS[day.weekday()], {}))
    return out


def _runs_label(a: int, b: int) -> str:
    if (a, b) == (0, 4):
        return "en semaine"
    if (a, b) == (5, 6):
        return "le week-end"
    if a == b:
        return f"le {JOURS[a]}"
    return f"du {JOURS[a]} au {JOURS[b]}"


def _by_days(values: list[str], render) -> str:
    """Sept valeurs (lundi → dimanche) → « X » si elles sont toutes pareilles, sinon « X en semaine, Y le week-end »."""
    if len(set(values)) == 1:
        return render(values[0], None)
    parts, start = [], 0
    for i in range(1, 8):
        if i == 7 or values[i] != values[start]:
            parts.append(render(values[start], _runs_label(start, i - 1)))
            start = i
    return ", ".join(parts)


def podcast_summary(settings: dict, default_words: int = 300) -> str:
    p = settings["podcast"]
    base_minutes = p["minutes"] if p["minutes"] else default_words / 150
    if not p.get("jours"):
        voice = voice_label(p["voix"]) if p["voix"] else "par défaut"
        return (f"🎙️ Podcast : environ {base_minutes:g} min · voix {voice} · vitesse {p['vitesse'] or 'normale'}"
                .replace(".", ","))
    week = [podcast_for(settings, date(2026, 10, 5 + i)) for i in range(7)]      # un lundi, puis la semaine
    minutes = _by_days([f"{(w['minutes'] or base_minutes):g}" for w in week],
                       lambda v, label: f"{v} min" + (f" {label}" if label else ""))
    voices = _by_days([voice_label(w["voix"]) if w["voix"] else "par défaut" for w in week],
                      lambda v, label: v + (f" {label}" if label else ""))
    speeds = _by_days([w["vitesse"] or "normale" for w in week],
                      lambda v, label: v + (f" {label}" if label else ""))
    return f"🎙️ Podcast : environ {minutes} · voix {voices} · vitesse {speeds}".replace(".", ",")


def apply_to_cfg(cfg: dict, settings: dict, day: date | None = None) -> dict:
    """Copie de la config où tes réglages remplacent ceux de config.yaml. `day` : le jour du podcast, pour que les
    réglages « en semaine » / « le week-end » / « le lundi »… s'appliquent (sans lui, seuls les réglages de tous les jours)."""
    out = copy.deepcopy(cfg)
    p = podcast_for(settings, day)
    if p["minutes"]:
        out["mots_cibles"] = int(p["minutes"] * 150)
    voix = out.setdefault("voix", {})
    if p["voix"]:
        if p["voix"].startswith("gemini"):
            voix["moteur"] = "gemini"
            name = p["voix"].partition(":")[2]
            if name:
                voix["gemini"] = {**(voix.get("gemini") or {}), "voix": name}
        else:
            voix["edge_voix"] = p["voix"]
            if voix.get("moteur") == "gemini":   # tu as choisi une voix Edge : on n'envoie rien à Gemini
                voix["moteur"] = "edge"
    if p["vitesse"]:
        voix["vitesse"] = p["vitesse"]
    return out


# --------------------------------------------------------------------------- « en semaine », « le week-end », « le lundi »…
@dataclass
class DayScope:
    """À quels jours s'applique un réglage du podcast ou une consigne."""
    days: tuple[int, ...] | None = None    # None = tous les jours (ou rien n'est dit)
    rest: bool = False                     # « sinon », « les autres jours » : le réglage de base, sans toucher aux jours déjà réglés
    problem: str | None = None             # "periode" : une date, « demain », une période… (pas géré pour le podcast)


_REST_RE = re.compile(
    r"\b(?:sinon|les autres jours|tous les autres jours|le reste du temps|autrement|par defaut|les jours restants)\b"
)
# Une date, « cette semaine », « lundi prochain »… : un jour précis, pas un rythme (jamais compris comme « chaque lundi »).
_ONE_OFF_CALENDAR_RE = re.compile(
    r"\b(?:cette semaine|semaines? (?:prochaine|suivante|derniere)|(?:week ?end|semaine|" + _WEEKDAY_ALT
    + r") (?:dernier|derniere|passe|passee|prochain|prochaine)|(?:ce|cet) (?:" + _WEEKDAY_ALT + r"))\b"
)
# « demain », « ce matin »… : un jour précis aussi, mais dans une consigne en texte libre, « les cours de demain » reste une façon de dire.
_ONE_OFF_RE = re.compile(r"\b(?:apres demain|demain|aujourd hui|ce matin|ce soir|cette nuit)\b")
_RANGE_RE = re.compile(r"\b(?:du )?(" + _WEEKDAY_ALT + r") au (" + _WEEKDAY_ALT + r")\b")
_TOPIC_RE = re.compile(r"\b(?:du|des|de|sur|d)\s+(?:le |la |les |l )?(?:week ?ends?|semaine|" + _WEEKDAY_ALT + r")s?\b")
# « sauf le dimanche », « pas le week-end » : le contraire de ce que le mot jour ferait comprendre. On ne devine pas.
_NEGATED_DAY_RE = re.compile(
    r"\b(?:pas|sans|hors|sauf|excepte|a part)\s+(?:le |la |les |en |l |du |des )?(?:week ?ends?|semaine|" + _WEEKDAY_ALT + r")s?\b"
)
_NOT_A_RHYTHM_RE = re.compile(r"\b(?:chaque|toutes les|par|une) semaines?\b")


def _expand_range(m: re.Match) -> str:
    a, b = _WEEKDAY_WORDS[m.group(1)], _WEEKDAY_WORDS[m.group(2)]
    span = range(a, b + 1) if a <= b else [*range(a, 7), *range(0, b + 1)]
    return " ".join(JOURS[d] for d in span)


def parse_day_scope(text, *, topic_guard: bool = False, strict: bool = True) -> DayScope:
    """Les jours dont parle une phrase : « en semaine », « le week-end », « le lundi et le mercredi », « du lundi au jeudi »,
    « tous les jours », « sinon »… Une date, « demain » ou une période ne sont pas gérés (problem="periode").

    `topic_guard=True` (consignes, texte libre) : « les films du week-end » parle d'un sujet, pas d'une condition.
    `strict=False` (texte libre) : seules les vraies dates et périodes refusent ; « demain », « pendant », « sauf »… peuvent
    faire partie de la consigne elle-même."""
    folded = _FROM_NOW_RE.sub(" ", _fold(text))
    folded = re.sub(r"\bpendant (?=(?:le|les|la) (?:week ?ends?|semaine)\b)", "", folded)
    folded = _RANGE_RE.sub(_expand_range, folded)
    if topic_guard:
        folded = _TOPIC_RE.sub(" ", folded)
    calendar = _DATE_RE.search(folded) or _THIS_WEEKEND_RE.search(folded) or _ONE_OFF_CALENDAR_RE.search(folded)
    if calendar or _NEGATED_DAY_RE.search(folded) or (strict and (_ONE_OFF_RE.search(folded) or has_unsupported_scope(folded))):
        return DayScope(problem="periode")
    rest = bool(_REST_RE.search(folded))
    folded = _NOT_A_RHYTHM_RE.sub(" ", _REST_RE.sub(" ", folded))
    days: set[int] = set()
    if re.search(r"\bsemaine\b|\bjours? (?:de cours|ouvres?|ouvrables?)\b", folded):
        days.update(range(5))
    if re.search(r"\bweek ?ends?\b", folded):
        days.update((5, 6))
    for m in re.finditer(r"\b(" + _WEEKDAY_ALT + r")s?\b", folded):
        days.add(_WEEKDAY_WORDS[m.group(1)])
    if len(days) == 7:
        days = set()
    return DayScope(tuple(sorted(days)) or None, rest)


def scope_from_value(value) -> DayScope:
    """Le « jours » proposé par l'IA (texte ou liste : « semaine », « weekend », ["lundi", "mercredi"], « tous »…)."""
    if value is None or value is False:
        return DayScope()
    items = value if isinstance(value, (list, tuple)) else [value]
    days: set[int] = set()
    rest = False
    for item in items:
        word = _fold(item)
        if word in DAYS:
            days.add(DAYS.index(word))
            continue
        scope = parse_day_scope(word)
        if scope.problem:
            return scope
        days.update(scope.days or ())
        rest = rest or scope.rest
    if len(days) == 7:
        days = set()
    return DayScope(tuple(sorted(days)) or None, rest)


_PIECE_SPLIT_RE = re.compile(r"\s*(?:;|,(?!\d)|\bet\b|\bpuis\b|\bmais\b)\s*", re.IGNORECASE)
SCOPE_PERIOD_TEXT = ("🤔 Pour le podcast, je comprends « en semaine », « le week-end », « tous les jours » ou un jour de la semaine "
                     "(« le lundi »). Je ne gère pas encore une date précise, une période ni une exception (« demain », « du 12 au 15 », "
                     "« sauf le dimanche »…).")
SCOPE_REFUSED_SHORT = ("réglage non appliqué : pour le podcast je comprends « en semaine », « le week-end » ou un jour de la semaine, "
                       "pas une date précise ni une période")
SCOPE_UNCLEAR_TEXT = ("🤔 Je n'ai pas compris à quels jours va chaque valeur, et je ne devine pas. Dis-le comme ça : "
                      "« 3 en semaine et 5 le week-end ».")


@dataclass
class ScopedOrder:
    value: object
    days: tuple[int, ...] | None = None
    rest: bool = False


@dataclass
class ScopedOrders:
    orders: list[ScopedOrder]
    problem: str | None = None      # "valeur" : aucune valeur comprise · "periode" · "flou" (jours pas clairs)


def minutes_spans(text: str) -> list[tuple[int, int, object]]:
    """Les durées dites dans `text` : (début, fin, minutes). « 3 minutes », « 1,5 », « 90 secondes »…"""
    out = []
    for m in re.finditer(r"(\d+(?:[.,]\d+)?)\s*(?:secondes?|sec|s|minutes?|mins?|m)?\b", text, re.IGNORECASE):
        value = parse_duree(m.group(0))
        if value is not None:
            out.append((m.start(), m.end(), value))
    return out


# --------------------------------------------------------------------------- une durée dite en phrase libre
# « Je voudrais un podcast de 3 minutes en semaine et de 5 minutes le week-end », au clavier ou en vocal. Comme pour l'heure du
# réveil, c'est lu à règles fixes : une IA d'extraction peut oublier les jours, et un ordre pris à moitié, c'est un ordre ignoré.
_SPOKEN_NUMBERS = {"un": 1, "une": 1, "deux": 2, "trois": 3, "quatre": 4, "cinq": 5, "six": 6, "sept": 7, "huit": 8}
_NUMBER_ALT = "|".join(_SPOKEN_NUMBERS)
_SPOKEN_HALF_RE = re.compile(r"\b(" + _NUMBER_ALT + r")\s+minutes?\s+et\s+demie?\b")
_BARE_HALF_RE = re.compile(r"\b(\d|deux|trois|quatre|cinq|six|sept|huit)\s+et\s+demie?\b(?!\s*(?:heures?|h\b))")
_SPOKEN_MINUTES_RE = re.compile(r"\b(" + _NUMBER_ALT + r")\s+minutes?\b")
_EXPLICIT_MINUTES_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(?:minutes?|mins?|mn)\b")
_PODCAST_SUBJECT_RE = re.compile(r"\b(?:podcasts?|durees?|durer|dure|durent|emissions?|episodes?|briefing|resume)\b")
_FREE_REQUEST_RE = re.compile(
    r"\b(?:je (?:voudrais|veux|souhaite|souhaiterais|prefere|preferais)|j (?:aimerais|aimerai|voudrais)|"
    r"(?:mets|mettez|met|fais|faites|regle|reglez|passe|passez|change|changez|garde|gardez|prends|prenez|donne|donnez|"
    r"reduis|diminue|augmente|allonge|raccourcis)(?: moi)?|il faudrait|il faut|faudrait|tu peux|peux tu|tu pourrais|pourrais tu|"
    r"(?:ce|ca|cela) serait (?:bien|mieux|top|parfait|super)|plutot|desormais|dorenavant|a partir de maintenant)\b"
)
# « 10 minutes avant le réveil », « plus tôt » : une durée, mais pas celle du podcast.
_FREE_OFFSET_RE = re.compile(r"\b(?:avant|apres|plus tot|plus tard|en avance|en retard|d avance|de retard|depuis|il y a)\b")


# Un chiffre seul compte comme une durée quand il est collé à un type de jour (« … et 5 le week-end », « le week-end 5 »),
# sauf s'il est suivi d'une autre unité (7 h, 2 fois, 20 ans…). Ailleurs (« j'ai 2 cours le lundi »), c'est autre chose.
_BARE_MINUTES_RE = re.compile(
    r"(?<![\d.,:])\b(\d(?:[.,]5)?)\b(?!\s*(?:h\b|heures?|fois|jours?|ans?|euros?|%|e\b|eme\b|ieme\b|er\b|\d|:|[.,]\d))"
)
_SCOPE_WORDS = (r"(?:week ?ends?|semaines?|lundis?|mardis?|mercredis?|jeudis?|vendredis?|samedis?|dimanches?|sinon|autres jours|"
                r"tous les jours|reste du temps|jours ouvres)")
_SCOPE_AFTER_RE = re.compile(r"^\s+(?:(?:le|la|les|en|du|au|aux|pour|de|des)\s+)?" + _SCOPE_WORDS + r"\b")
_SCOPE_BEFORE_RE = re.compile(_SCOPE_WORDS + r"\s*[,:]?\s*(?:(?:de|a|pour|mets|met)\s+)?$")
_SPOKEN_BARE_RE = re.compile(r"\b(deux|trois|quatre|cinq|six|sept|huit)\b")      # « un » et « une » sont des articles
_REASON_RE = re.compile(r"\b(?:parce que|parce qu|car|puisque|puisqu|vu que|etant donne que|sachant que|attendu que)\b")
_WAKE_WORDS_RE = re.compile(r"\b(?:reveil|reveille\w*|alarme|sonne\w*|lever|leve\w*)\b")
_SEPARATORS_RE = re.compile(r"(\s*(?:;|,(?!\d)|\bet\b|\bpuis\b|\bmais\b)\s*)")
_CONTINUATION_GLUE = frozenset(
    "et puis ensuite aussi pareil donc alors le la les en du de des au aux pour a sinon minute minutes min mins mn week end "
    "weekend weekends semaine semaines jour jours ouvres ouvrables tous toutes autres reste temps".split()
) | frozenset(_WEEKDAY_WORDS) | frozenset(day + "s" for day in _WEEKDAY_WORDS)


def explicit_minutes_spans(text: str) -> list[tuple[int, int, object]]:
    """Comme minutes_spans, mais seulement les durées AVEC leur unité (« 3 minutes », « 1,5 min ») : en phrase libre,
    un nombre seul peut être une heure, un âge, un prix…"""
    out = []
    for m in _EXPLICIT_MINUTES_RE.finditer(text):
        value = parse_duree(m.group(0))
        if value is not None:
            out.append((m.start(), m.end(), value))
    return out


def _next_to_scope(text: str, start: int, end: int) -> bool:
    """Le mot (ou chiffre) de `text[start:end]` est-il collé à un type de jour, avant ou après ?"""
    return bool(_SCOPE_AFTER_RE.match(text[end:end + 40]) or _SCOPE_BEFORE_RE.search(text[max(0, start - 40):start]))


def _spoken_to_digits(folded: str) -> str:
    """Écrit les durées d'une phrase d'ordre en chiffres AVEC leur unité : « trois minutes et demie » → « 3.5 minutes » ;
    « … et cinq le week-end » → « … et 5 minutes le week-end » (un nombre seul, collé à un type de jour, est une durée :
    « j'ai 2 cours le lundi » n'en est pas une)."""
    folded = _SPOKEN_HALF_RE.sub(lambda m: f"{_SPOKEN_NUMBERS[m.group(1)]}.5 minutes", folded)
    folded = _SPOKEN_MINUTES_RE.sub(lambda m: f"{_SPOKEN_NUMBERS[m.group(1)]} minutes", folded)
    folded = _BARE_HALF_RE.sub(lambda m: f"{_SPOKEN_NUMBERS.get(m.group(1), m.group(1))}.5", folded)     # « 4 et demi le dimanche »
    folded = _SPOKEN_BARE_RE.sub(
        lambda m: str(_SPOKEN_NUMBERS[m.group(1)]) if _next_to_scope(folded, m.start(), m.end()) else m.group(0), folded)
    for m in reversed(list(_BARE_MINUTES_RE.finditer(folded))):
        if not folded.startswith(" minute", m.end()) and _next_to_scope(folded, m.start(), m.end()):
            folded = folded[:m.end()] + " minutes" + folded[m.end():]
    return folded


def _without_wake_clauses(folded: str) -> str:
    """Retire d'une phrase les morceaux qui parlent du réveil (« mets le réveil à 7h30 en semaine, et… ») : leurs jours ne sont
    pas ceux de la durée du podcast."""
    parts = _SEPARATORS_RE.split(folded)                 # texte, séparateur, texte, séparateur…
    kept: list[str] = []
    for i in range(0, len(parts), 2):
        if _WAKE_WORDS_RE.search(parts[i]) or find_time(parts[i]):
            continue
        if kept:
            kept.append(parts[i - 1])
        kept.append(parts[i])
    return "".join(kept)


def _read_order_sentence(sentence: str, follows: bool) -> list[ScopedOrder] | None:
    """Une phrase du message : les durées par jour qu'elle demande ; [] si ce n'est pas un ordre de durée ; None si un doute
    (date, exception, jours flous, durée hors de 1 à 8 minutes) interdit de rien conclure du message."""
    folded = _REASON_RE.split(_fold(sentence))[0]            # « …3 minutes parce que le lundi j'ai cours » : le lundi est une raison
    if _FREE_OFFSET_RE.search(folded):
        return []
    is_order = bool(_PODCAST_SUBJECT_RE.search(folded) and _FREE_REQUEST_RE.search(folded))
    tokens = re.findall(r"[a-z]+|\d+(?:\.\d+)?", folded)
    is_follow_up = follows and bool(tokens) and all(
        t in _CONTINUATION_GLUE or t in _SPOKEN_NUMBERS or re.fullmatch(r"\d+(?:\.\d+)?", t) for t in tokens)
    if not (is_order or is_follow_up):
        return []
    if is_order:
        folded = _without_wake_clauses(folded)
    folded = _spoken_to_digits(folded)
    mentions = len(_EXPLICIT_MINUTES_RE.findall(folded))
    if mentions != len(explicit_minutes_spans(folded)):
        return None                                          # « 12 minutes » : hors de 1 à 8, l'IA et les garde-fous s'en occupent
    if not mentions:
        return []
    parsed = parse_scoped(folded, explicit_minutes_spans)
    if parsed.problem or not parsed.orders:
        return None
    return parsed.orders if any(o.days or o.rest for o in parsed.orders) else []     # sans aucun jour dit : « mets le podcast à 3 minutes » reste à l'IA


def scan_spoken_durations(text: str) -> tuple[list[ScopedOrder] | None, int]:
    """Les durées de podcast demandées EN PHRASE, pour certains jours : « je voudrais un podcast de trois minutes en semaine
    et de cinq minutes le week-end ». Retourne (ordres, nombre de mots du message qui ne sont PAS dans une phrase d'ordre).

    Les ordres sont None tant que ce n'est pas clairement un ordre sur la durée du podcast avec au moins un type de jour
    (l'IA décide alors, comme avant) ; en cas de doute (date, exception, jours flous, durée hors de 1 à 8 minutes), None
    aussi : on ne devine pas."""
    orders: list[ScopedOrder] = []
    leftover = 0
    follows = False                                          # la phrase d'avant était un ordre : « Et 5 minutes le week-end. » en est la suite
    for sentence in re.split(r"(?<=[.!?…])\s+|\n+", str(text or "")):
        found = _read_order_sentence(sentence, follows)
        if found is None:
            return None, 0
        follows = bool(found)
        if found:
            orders += found
        else:
            leftover += len(sentence.split())
    return (orders or None), leftover


def parse_spoken_durations(text: str) -> list[ScopedOrder] | None:
    """Voir scan_spoken_durations : seulement les ordres."""
    return scan_spoken_durations(text)[0]


def voice_spans(text: str) -> list[tuple[int, int, object]]:
    """Les voix nommées dans `text` : (début, fin, voix)."""
    out = []
    for m in re.finditer(r"[^\W\d_][\w-]*", text):
        value = parse_voix(m.group(0))
        if value:
            out.append((m.start(), m.end(), value))
    return out


def speed_spans(text: str) -> list[tuple[int, int, object]]:
    """Les vitesses dites dans `text` : « lent », « très rapide », « normal », « +15% »…"""
    out = []
    for m in re.finditer(r"(?:tr[eè]s\s+)?(?:lent|rapide|normal)\b|[+\-−–]?\s*\d{1,2}\s*%", text, re.IGNORECASE):
        value = parse_vitesse(m.group(0))
        if value:
            out.append((m.start(), m.end(), value))
    return out


def _one_value_pieces(piece: str, spans: list[tuple[int, int, object]]) -> list[tuple[str, object]]:
    """Coupe un morceau qui dit plusieurs valeurs sans les séparer (« 3 semaine 5 week-end », « en semaine 3 minutes le
    week-end 5 minutes ») : si le morceau COMMENCE par une valeur, chaque valeur ouvre un morceau ; sinon chacune le ferme."""
    if len(spans) == 1:
        return [(piece, spans[0][2])]
    out: list[tuple[str, object]] = []
    if not piece[:spans[0][0]].strip():                                  # « 3 semaine 5 week-end » : la valeur d'abord
        cuts = [0] + [span[0] for span in spans[1:]] + [len(piece)]
    else:                                                                # « en semaine 3 minutes le week-end 5 » : la valeur ferme
        cuts = [0] + [span[1] for span in spans[:-1]] + [len(piece)]
    for (a, b), span in zip(zip(cuts, cuts[1:]), spans):
        out.append((piece[a:b], span[2]))
    return out


def parse_scoped(args: str, spans_of) -> ScopedOrders:
    """« 3 », « 3 semaine 5 week-end », « 5 le week-end, 3 sinon », « femme en semaine et kore le week-end »… → une valeur par groupe de jours.

    `spans_of(texte)` repère les valeurs dites (voir minutes_spans). Plusieurs valeurs : chacune doit dire ses jours (sauf la
    première, qui vaut alors pour tous les jours : « 3 minutes, et 5 le week-end »). Rien n'est deviné."""
    pieces: list[tuple[str, object]] = []                  # (morceau, valeur ou None)
    for chunk in _PIECE_SPLIT_RE.split(str(args or "")):
        if not chunk or not chunk.strip():
            continue
        spans = spans_of(chunk)
        pieces += _one_value_pieces(chunk, spans) if spans else [(chunk, None)]
    entries: list[list] = []                        # [valeur, jours (ensemble), rest]
    pending_days: set[int] = set()
    pending_rest = False
    for piece, value in pieces:
        scope = parse_day_scope(piece)
        if scope.problem:
            return ScopedOrders([], scope.problem)
        if value is None:
            if scope.days or scope.rest:            # « le samedi et le dimanche » : la suite des jours d'avant (ou de la valeur à venir)
                if entries and not pending_days and not pending_rest:
                    entries[-1][1] |= set(scope.days or ())
                    entries[-1][2] = entries[-1][2] or scope.rest
                else:
                    pending_days |= set(scope.days or ())
                    pending_rest = pending_rest or scope.rest
            continue
        days, rest = set(scope.days or ()), scope.rest
        if not days and not rest:
            days, rest = pending_days, pending_rest
        entries.append([value, set(days), rest])
        pending_days, pending_rest = set(), False
    if not entries:
        return ScopedOrders([], "valeur")
    if any(not days and not rest for _, days, rest in entries[1:]):
        return ScopedOrders([], "flou")
    orders = [ScopedOrder(value, tuple(sorted(days)) or None, rest) for value, days, rest in entries]
    orders.sort(key=lambda o: not o.rest)          # « sinon » d'abord : il ne doit jamais effacer les jours déjà réglés
    return ScopedOrders(orders)
