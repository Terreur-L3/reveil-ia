"""La mémoire : tout ce que ton journal vocal a appris, en fichiers simples et lisibles.

memoire/
  journal/entrees.jsonl        tout ce que tu as dit ou écrit (texte complet), une entrée par ligne
  journal/AAAA/AAAA-MM-JJ.md   la même chose, à lire facilement sur GitHub
  faits.jsonl                  les infos retenues, une par ligne
  interets.yaml                tes centres d'intérêt appris (ils servent à choisir les actus)
  anniversaires_auto.yaml      anniversaires entendus dans tes messages
  rappels_auto.yaml            échéances entendues dans tes messages
  questions.jsonl              les questions « Inspi » posées, et à quelle entrée tu as répondu
  consignes.yaml               comment tu veux que je te parle (ton, longueur…), dit dans le chat
  reglages.json                l'heure du réveil, la durée et la voix du podcast (lu aussi par la sonnette)
  emploi_du_temps.yaml         tes cours, lus sur une photo ou écrits dans le chat
  _etat/telegram.json          où en est le bot (ne pas toucher)

Tes fichiers écrits à la main (anniversaires.yaml, rappels.yaml, moi.yaml) restent à toi et gagnent toujours.
L'audio et les images ne sont jamais conservés : seulement le texte qu'on en tire.
"""
from __future__ import annotations

import json
import logging
import os
import re
import unicodedata
from collections import Counter
from datetime import date, datetime, timedelta
from hashlib import sha1
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from . import reglages as reglages_mod
from .fr import duree_fr

if TYPE_CHECKING:  # pragma: no cover
    from .extract import Extraction

log = logging.getLogger(__name__)

STATE_DEFAULT = {
    "version": 1,
    "offset": 0,                     # prochain numéro de message Telegram à lire
    "file": [],                      # vocaux reçus mais pas encore entièrement traités
    "question_en_attente": None,     # dernière question « Inspi » sans réponse
    "derniere_question_du_soir": "",  # date (AAAA-MM-JJ) de la dernière question du soir
    "stt_historique": [],            # [[horodatage, secondes d'audio]] sur la dernière heure
}
REMINDER_KEEP_DAYS = 30
MAX_AIME, MAX_CANDIDATS = 40, 60
MAX_CONSIGNES = 25
CONSIGNE_SCOPES = ("podcast", "questions", "perso", "tout")
# Une consigne qui parle de tes cours, anniversaires ou rappels concerne la façon de les dire, même si elle a été rangée
# sous « podcast » ou « tout » (anciennes consignes, ou /consigne écrite sans le mot « perso »).
_PERSO_WORDS_RE = re.compile(
    r"\b(cours|emploi du temps|edt|planning|agenda|horaires?|anniversaires?|anniv|rappels?|rendez vous|rdv|"
    r"infos? perso\w*|informations? perso\w*|salles?|profs?|professeurs?|enseignants?)\b"
)


def norm(text) -> str:
    """Clé de comparaison : sans accents, majuscules ni ponctuation."""
    flat = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode()
    return re.sub(r"\W+", " ", flat.lower()).strip()


# « au cours de la semaine », « le cours du bitcoin », « un projet en cours » : le mot « cours » n'est pas celui de l'emploi du temps.
_NOT_A_COURSE_RE = re.compile(
    r"\b(?:au cours (?:de|du|des|d)|en cours|cours d eau|cours (?:de la |de l |de |du |des )?"
    r"(?:bourse|change|bitcoin|btc|dollar|euro|petrole|baril|or|cac|gaz|ble|marche|actions|monnaies|matieres))\b"
)


def mentions_perso(text) -> bool:
    """Le texte parle-t-il de tes cours, anniversaires ou rappels (donc de la façon de les citer) ?"""
    return bool(_PERSO_WORDS_RE.search(_NOT_A_COURSE_RE.sub(" ", norm(text))))


# --------------------------------------------------------------------------- fichiers
def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(path.name + ".part")
    part.write_text(text, encoding="utf-8")
    os.replace(part, path)


def read_jsonl(path: Path) -> list[dict]:
    out: list[dict] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return out
    for n, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            log.warning("%s ligne %d illisible : ignorée", path.name, n)
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def write_jsonl(path: Path, records: list[dict]) -> None:
    _write(path, "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records))


def append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def _read_yaml(path: Path, default):
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except Exception as exc:
        log.warning("%s illisible (%s) : ignoré", path.name, exc)
        return default
    return data if isinstance(data, type(default)) else default


def _write_yaml(path: Path, data, header: str) -> None:
    body = yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False, width=100)
    _write(path, "".join(f"# {line}\n" for line in header.splitlines()) + body)


# --------------------------------------------------------------------------- la mémoire
class Memoire:
    def __init__(self, racine: Path | str):
        self.racine = Path(racine)
        self.dir = self.racine / "memoire"
        self.p_entrees = self.dir / "journal" / "entrees.jsonl"
        self.p_faits = self.dir / "faits.jsonl"
        self.p_questions = self.dir / "questions.jsonl"
        self.p_interets = self.dir / "interets.yaml"
        self.p_anniv = self.dir / "anniversaires_auto.yaml"
        self.p_rappels = self.dir / "rappels_auto.yaml"
        self.p_consignes = self.dir / "consignes.yaml"
        self.p_reglages = self.dir / "reglages.json"
        self.p_edt = self.dir / "emploi_du_temps.yaml"
        self.p_etat = self.dir / "_etat" / "telegram.json"

    # ----- état du bot
    def load_state(self) -> dict:
        state = json.loads(json.dumps(STATE_DEFAULT))
        try:
            saved = json.loads(self.p_etat.read_text(encoding="utf-8"))
            if isinstance(saved, dict):
                state.update(saved)
        except FileNotFoundError:
            pass
        except ValueError:
            log.warning("État du bot illisible : on repart de zéro (les messages sont dédoublonnés)")
        return state

    def save_state(self, state: dict) -> None:
        _write(self.p_etat, json.dumps(state, ensure_ascii=False, indent=1) + "\n")

    # ----- journal
    def entries(self) -> list[dict]:
        return read_jsonl(self.p_entrees)

    def has_entry(self, entry_id: str) -> bool:
        return any(e.get("id") == entry_id for e in self.entries())

    def add_entry(self, entry: dict, question_text: str | None = None) -> None:
        append_jsonl(self.p_entrees, entry)
        day = self.dir / "journal" / entry["date"][:4] / f"{entry['date']}.md"
        head = "" if day.exists() else f"# {entry['date']}\n"
        kind = entry.get("type") if entry.get("type") in ("vocal", "image") else "texte"
        title = f"{entry.get('heure', '')} · {kind}" + (f" · {duree_fr(entry['duree_s'])}" if entry.get("duree_s") else "")
        block = f"\n## {title}\n"
        if question_text:
            block += f"> Réponse à la question : « {question_text} »\n"
        block += f"\n{entry['texte'].strip()}\n"
        day.parent.mkdir(parents=True, exist_ok=True)
        with open(day, "a", encoding="utf-8") as fh:
            fh.write(head + block)

    def update_entry(self, entry_id: str, **changes) -> None:
        records = self.entries()
        for rec in records:
            if rec.get("id") == entry_id:
                rec.update(changes)
        write_jsonl(self.p_entrees, records)

    def entries_to_extract(self, min_words: int, max_attempts: int = 5) -> list[dict]:
        return [
            e for e in self.entries()
            if not e.get("extrait") and len(str(e.get("texte", "")).split()) >= min_words
            and int(e.get("essais_extraction", 0)) < max_attempts
        ]

    # ----- faits
    def facts(self) -> list[dict]:
        return read_jsonl(self.p_faits)

    def add_facts(self, faits: list[dict], source: str, day: str) -> int:
        known = {f.get("id") for f in self.facts()}
        added = 0
        for fait in faits:
            texte = str(fait.get("texte", "")).strip()
            fid = sha1(norm(texte).encode("utf-8")).hexdigest()[:10]
            if not texte or fid in known:
                continue
            known.add(fid)
            append_jsonl(self.p_faits, {
                "id": fid, "date": day, "categorie": fait.get("categorie", "autre"), "texte": texte, "source": source,
            })
            added += 1
        return added

    # ----- anniversaires et rappels appris
    def birthdays(self) -> list[dict]:
        return [d for d in _read_yaml(self.p_anniv, []) if isinstance(d, dict)]

    def upsert_birthday(self, nom: str, jour: int, mois: int, annee: int | None, source: str) -> str:
        """'ajoute', 'modifie' ou 'identique'. Le dernier message l'emporte (c'est ainsi qu'on corrige)."""
        entries = self.birthdays()
        new = {"nom": nom, "jour": int(jour), "mois": int(mois)}
        if annee:
            new["annee"] = int(annee)
        for i, old in enumerate(entries):
            if norm(old.get("nom")) == norm(nom):
                same = all(old.get(k) == new.get(k) for k in ("jour", "mois")) and (
                    not annee or old.get("annee") == new.get("annee")
                )
                if same:
                    return "identique"
                if not annee and old.get("annee"):
                    new["annee"] = old["annee"]  # on garde l'année déjà connue
                new["source"] = source
                entries[i] = new
                _write_yaml(self.p_anniv, entries, _HEADER_ANNIV)
                return "modifie"
        new["source"] = source
        entries.append(new)
        _write_yaml(self.p_anniv, entries, _HEADER_ANNIV)
        return "ajoute"

    def reminders(self) -> list[dict]:
        return [d for d in _read_yaml(self.p_rappels, []) if isinstance(d, dict)]

    def add_reminder(self, texte: str, when: str, source: str, today: date) -> bool:
        entries = self.reminders()
        keep_after = (today - timedelta(days=REMINDER_KEEP_DAYS)).isoformat()
        entries = [e for e in entries if str(e.get("date", "")) >= keep_after]
        key = (norm(texte), when)
        if any((norm(e.get("texte")), str(e.get("date"))) == key for e in entries):
            return False
        entries.append({"texte": texte, "date": when, "source": source})
        entries.sort(key=lambda e: str(e.get("date", "")))
        _write_yaml(self.p_rappels, entries, _HEADER_RAPPELS)
        return True

    # ----- centres d'intérêt appris
    def interests(self) -> dict:
        data = _read_yaml(self.p_interets, {})
        return {
            "aime": [e for e in data.get("aime") or [] if isinstance(e, dict) and e.get("sujet")],
            "candidats": [e for e in data.get("candidats") or [] if isinstance(e, dict) and e.get("sujet")],
            "evite": [str(v) for v in data.get("evite") or [] if str(v).strip()],
        }

    def merge_interests(self, aime: list[dict], evite: list[dict], day: str) -> list[str]:
        """Met à jour interets.yaml. Retourne les sujets qui viennent d'être retenus pour les actus."""
        data = self.interests()
        before = {norm(e["sujet"]) for e in data["aime"]}
        pool = {norm(e["sujet"]): dict(e) for e in data["aime"] + data["candidats"]}
        for topic in aime:
            key = norm(topic["sujet"])
            entry = pool.get(key) or {"sujet": topic["sujet"], "mentions": 0, "intensite": 1}
            entry["mentions"] = int(entry.get("mentions", 0)) + 1
            entry["intensite"] = max(int(entry.get("intensite", 1)), int(topic.get("intensite", 1)))
            entry["derniere"] = day
            pool[key] = entry

        avoided = {norm(v): v for v in data["evite"]}
        for topic in evite:
            avoided.setdefault(norm(topic["sujet"]), topic["sujet"])
        for key in avoided:  # « j'en ai marre du foot » : le sujet quitte la liste de ce qui plaît
            pool.pop(key, None)

        liked: list[dict] = []
        candidates: list[dict] = []
        for entry in pool.values():
            if entry["mentions"] >= 2 or entry["intensite"] >= 2:
                strong = entry["intensite"] >= 3 or entry["mentions"] >= 5
                entry["poids"] = 3 if strong else 2 if (entry["intensite"] >= 2 or entry["mentions"] >= 3) else 1
                liked.append(entry)
            else:
                entry.pop("poids", None)
                candidates.append(entry)
        liked.sort(key=lambda e: (-e["poids"], -e["mentions"], norm(e["sujet"])))
        candidates.sort(key=lambda e: str(e.get("derniere", "")), reverse=True)
        out = {"aime": liked[:MAX_AIME], "candidats": candidates[:MAX_CANDIDATS], "evite": sorted(avoided.values())}
        _write_yaml(self.p_interets, out, _HEADER_INTERETS)
        return [e["sujet"] for e in out["aime"] if norm(e["sujet"]) not in before]

    # ----- réglages (réveil, voix…) et emploi du temps
    def load_reglages(self) -> dict:
        return reglages_mod.load(self.p_reglages)

    def save_reglages(self, settings: dict) -> None:
        reglages_mod.save(self.p_reglages, settings)

    # ----- consignes : comment tu veux que je te parle
    def consignes(self) -> list[dict]:
        return [c for c in _read_yaml(self.p_consignes, []) if isinstance(c, dict) and c.get("texte")]

    @staticmethod
    def _consigne_days(entry_or_list) -> list[str]:
        """Les jours (« lun », « sam »…) d'une consigne, dans l'ordre de la semaine ; vide = tous les jours."""
        raw = entry_or_list.get("jours") if isinstance(entry_or_list, dict) else entry_or_list
        days = sorted({d for d in (raw if isinstance(raw, (list, tuple)) else []) if d in reglages_mod.DAYS},
                      key=reglages_mod.DAYS.index)
        return [] if len(days) == 7 else days

    def add_consigne(self, texte: str, portee: str, day: str, source: str, prive: bool = False,
                     jours: list[str] | None = None) -> bool:
        texte = re.sub(r"\s+", " ", re.sub(r"\[\[|\]\]|https?://\S+", "", str(texte or ""))).strip()[:160]
        if len(texte) < 6:
            return False
        days = self._consigne_days(jours)
        entries = self.consignes()
        if any(norm(c["texte"]) == norm(texte) and self._consigne_days(c) == days for c in entries):
            return False
        entry = {"texte": texte, "portee": portee if portee in CONSIGNE_SCOPES else "tout",
                 "date": day, "source": source}
        if days:
            entry["jours"] = days
        if prive:
            entry["prive"] = True
        entries.append(entry)
        _write_yaml(self.p_consignes, entries[-MAX_CONSIGNES:], _HEADER_CONSIGNES)
        return True

    def remove_consigne(self, number: int) -> dict | None:
        """Retire la consigne n° `number` (1 = la première de la liste affichée par /consignes)."""
        entries = self.consignes()
        if not 1 <= number <= len(entries):
            return None
        gone = entries.pop(number - 1)
        _write_yaml(self.p_consignes, entries, _HEADER_CONSIGNES)
        return gone

    def consignes_for(self, scope: str, *, allow_private: bool, day: date | None = None) -> list[str]:
        """Les consignes qui concernent « podcast », « questions » ou « perso » (la façon de citer tes cours, anniversaires
        et rappels). Celles de portée « tout » comptent pour podcast et questions.

        `day` : le jour du podcast ; une consigne limitée à certains jours (« le week-end, parle plus décontracté ») ne
        sort que ces jours-là. Sans `day`, aucun filtre par jour.

        `allow_private=False` (rédaction du podcast, qui peut passer par Gemini) : on écarte celles que l'IA ou
        la liste de sujets sensibles a jugées personnelles. Elles restent dans la liste et servent aux questions."""
        from .extract import is_sensitive  # import tardif : extract importe déjà ce module

        out = []
        for c in self.consignes():
            portee = c.get("portee", "tout")
            if scope == "perso":
                if not (portee == "perso" or (portee in ("podcast", "tout") and mentions_perso(c["texte"]))):
                    continue
            elif portee not in (scope, "tout"):
                continue
            days = self._consigne_days(c)
            if day is not None and days and reglages_mod.DAYS[day.weekday()] not in days:
                continue
            if not allow_private and (c.get("prive") or is_sensitive(c["texte"])):
                continue
            out.append(c["texte"])
        return out

    # ----- questions « Inspi »
    def questions(self) -> list[dict]:
        return read_jsonl(self.p_questions)

    def next_question_id(self, now: datetime) -> str:
        base = f"q{now:%Y%m%d-%H%M}"
        taken = {q.get("id") for q in self.questions()}
        qid, n = base, 1
        while qid in taken:
            n += 1
            qid = f"{base}-{n}"
        return qid

    def add_question(self, question: dict) -> None:
        append_jsonl(self.p_questions, question)

    def update_question(self, question_id: str, **changes) -> None:
        records = self.questions()
        for rec in records:
            if rec.get("id") == question_id:
                rec.update(changes)
        write_jsonl(self.p_questions, records)

    def question_by_message(self, message_id: int | None) -> dict | None:
        if message_id is None:
            return None
        return next((q for q in self.questions() if q.get("message_id") == message_id), None)

    def question(self, question_id: str | None) -> dict | None:
        if not question_id:
            return None
        return next((q for q in self.questions() if q.get("id") == question_id), None)

    # ----- application d'une extraction
    def apply_extraction(self, entry: dict, ex: "Extraction", today: date, settings: dict | None = None) -> dict:
        """Range ce que l'IA a extrait. `settings` = les réglages que l'appelant tient déjà en mémoire et écrira lui-même
        à la fin (sinon on les relirait ici, puis il écraserait notre changement avec sa copie plus ancienne)."""
        source = f"journal {entry['date']}"
        report = {"faits": self.add_facts(ex.faits, entry["id"], entry["date"]), "anniversaires": [], "rappels": []}
        for b in ex.anniversaires:
            status = self.upsert_birthday(b["nom"], b["jour"], b["mois"], b.get("annee"), source)
            if status != "identique":
                report["anniversaires"].append((b, status))
        for r in ex.rappels:
            if self.add_reminder(r["texte"], r["date"], source, today):
                report["rappels"].append(r)
        report["promus"] = self.merge_interests(ex.aime, ex.evite, entry["date"])
        report["consignes"] = [
            c for c in ex.consignes
            if self.add_consigne(c["texte"], c.get("portee", "tout"), entry["date"], source, bool(c.get("sensible")),
                                 c.get("jours"))
        ]
        report["reglages"] = []
        if ex.reglages:
            own = settings is None
            current = self.load_reglages() if own else settings
            changed = False
            for r in sorted(ex.reglages, key=lambda r: not r.get("autres")):     # « sinon » d'abord (voir parse_scoped)
                valid = reglages_mod.validate_setting(r.get("cle"), r.get("valeur"))
                if not valid:
                    continue
                if r.get("refus"):
                    report["reglages"].append("⚠️ " + reglages_mod.SCOPE_REFUSED_SHORT)
                    continue
                days = tuple(reglages_mod.DAYS.index(d) for d in r.get("jours") or [] if d in reglages_mod.DAYS) or None
                report["reglages"].append(reglages_mod.set_podcast(current, *valid, days, bool(r.get("autres"))))
                changed = True
            if changed and own:
                self.save_reglages(current)
        self.update_entry(entry["id"], resume=ex.resume, extrait=True)
        return report

    # ----- ce que la mémoire sait (pour les questions et pour /memoire)
    def stats(self) -> dict:
        entries = self.entries()
        facts = self.facts()
        interets = self.interests()
        questions = self.questions()
        return {
            "entrees": len(entries),
            "secondes": sum(int(e.get("duree_s") or 0) for e in entries),
            "mots": sum(len(str(e.get("texte", "")).split()) for e in entries),
            "faits": len(facts),
            "categories": Counter(f.get("categorie", "autre") for f in facts),
            "anniversaires": len(self.birthdays()),
            "rappels": len(self.reminders()),
            "aime": interets["aime"],
            "evite": interets["evite"],
            "questions": len(questions),
            "repondues": sum(1 for q in questions if q.get("repondue_par")),
            "consignes": len(self.consignes()),
        }

    def digest(self, recent_facts: int = 15, recent_entries: int = 5, recent_questions: int = 12) -> str:
        """Résumé de la mémoire pour guider une question (reste en privé : Groq uniquement)."""
        interets = self.interests()
        facts = sorted(self.facts(), key=lambda f: str(f.get("date", "")))[-recent_facts:]
        entries = [e for e in self.entries() if e.get("resume")][-recent_entries:]
        questions = self.questions()[-recent_questions:]
        counts = Counter(f.get("categorie", "autre") for f in self.facts())
        lines = []
        if interets["aime"]:
            lines.append("Centres d'intérêt connus : " + " ; ".join(f"{e['sujet']} ({e.get('poids', 1)})" for e in interets["aime"][:10]))
        if facts:
            lines.append("Infos récentes :")
            lines += [f"- {f['texte']}" for f in facts]
        if entries:
            lines.append("Messages récents (résumés) :")
            lines += [f"- {e['date']} : {e['resume']}" for e in entries]
        if counts:
            lines.append("Nombre d'infos par thème : " + ", ".join(f"{k} {v}" for k, v in counts.most_common()))
        if questions:
            lines.append("Questions déjà posées (ne pas les répéter) :")
            lines += [f"- {q.get('texte', '')}" for q in questions]
        return "\n".join(lines) or "(la mémoire est encore vide)"


_HEADER_ANNIV = (
    "Géré automatiquement par ton journal vocal : tu peux corriger ce fichier à la main.\n"
    "Pour qu'un anniversaire soit prioritaire, mets-le plutôt dans anniversaires.yaml."
)
_HEADER_RAPPELS = (
    "Géré automatiquement par ton journal vocal : tu peux corriger ce fichier à la main.\n"
    "Les rappels dépassés depuis plus d'un mois sont effacés."
)
_HEADER_CONSIGNES = (
    "Géré par ton journal vocal : tes consignes sur la façon de te parler (/consignes pour les voir, /oublier N pour en retirer).\n"
    "portee : podcast, questions, perso (comment citer tes cours, anniversaires, rappels) ou tout. jours : liste (lun, mar…) si la consigne\n"
    "ne vaut que certains jours. prive : true = jamais envoyée à Gemini (seulement à Groq). Tu peux corriger ce fichier à la main."
)
_HEADER_INTERETS = (
    "Géré automatiquement par ton journal vocal.\n"
    "aime : sujets retenus pour choisir tes actus (poids 1 à 3) ; candidats : mentionnés une fois, pas encore retenus ;\n"
    "evite : sujets dont tu ne veux plus entendre parler. Tu peux corriger ce fichier à la main."
)
