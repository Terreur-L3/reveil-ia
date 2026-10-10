"""Extraction : transforme ce que tu as dit en informations rangées.

À partir d'un message (transcrit ou écrit), l'IA en tire : un résumé, des faits, des anniversaires,
des rappels datés et des centres d'intérêt. Tout passe par Groq (chaîne « prive ») : rien de ce que
tu racontes ne va chez Gemini. Les sujets sensibles (santé, argent, religion, politique…) restent dans
la mémoire mais ne servent jamais à choisir les actus.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta

from . import llm, reglages
from .jsonutil import load_json
from .memoire import norm
from .pacer import estimate_tokens

log = logging.getLogger(__name__)

CATEGORIES = [
    "personne", "projet", "etudes", "gout", "habitude", "objectif", "evenement", "lieu", "materiel", "sante", "autre",
]

# Sujets qui ne servent jamais à choisir tes actus (comparés sans accents ; \b évite « argentique » ≠ « argent »).
_SENSIBLE_RE = re.compile(
    r"\b(sante|maladie|diagnostic|depression|anxiete|angoisse|therapie|psy|psychiatre|psychologue|medicament|"
    r"cancer|diabete|handicap|autisme|hopital|chirurgie|religion|dieu|eglise|mosquee|islam|musulman|chretien|"
    r"juif|catholique|sexualite|sexuel|homosexualite|politique|election|elections|vote|macron|dette|salaire|"
    r"impot|argent|deuil|deces|suicide|addiction|drogue|alcool|harcelement|violence|divorce)\b"
)

SYSTEM = """Tu es la mémoire personnelle d'un journal vocal. On te donne ce que <<prenom>> a dit ou écrit (transcription automatique : il peut y avoir des erreurs). Tu en extrais des informations, SANS RIEN INVENTER : uniquement ce qui est dit.

Réponds par un objet JSON avec exactement ces clés :
- "resume" : une ou deux phrases qui résument le message ;
- "faits" : liste d'objets {"categorie", "texte"}. "texte" = une phrase courte, autonome, à la troisième personne, compréhensible sans le reste du message (ex. « Prépare un court-métrage pour son école », « Karim est son meilleur ami »). "categorie" parmi : personne, projet, etudes, gout, habitude, objectif, evenement, lieu, materiel, sante, autre ;
- "anniversaires" : liste d'objets {"nom", "jour", "mois", "annee"}, uniquement si un anniversaire ou une date de naissance est clairement dit ("annee" = null si inconnue) ;
- "rappels" : liste d'objets {"texte", "date"} pour une échéance, un rendez-vous ou un événement à venir. "date" au format AAAA-MM-JJ, calculée à partir de la date du message ; ignore ce qui n'a pas de date précise ou déductible ;
- "aime" : liste d'objets {"sujet", "intensite", "sensible"} : centres d'intérêt et passions durables, ou sujets dont <<prenom>> dit vouloir entendre parler. "sujet" = un libellé court (4 mots maximum) ; si le sujet figure déjà dans « Sujets connus », reprends EXACTEMENT son libellé. "intensite" de 1 (cité en passant) à 3 (passion, enthousiasme net). "sensible" = true si le sujet touche à la santé, la sexualité, la religion, la politique, l'argent ou des conflits personnels ;
- "evite" : liste d'objets {"sujet", "sensible"} : sujets dont <<prenom>> ne veut plus entendre parler ou qui l'agacent ;
- "consignes" : liste d'objets {"texte", "portee", "sensible"} : uniquement ce que <<prenom>> DEMANDE explicitement à l'assistant sur sa façon de parler ou de se comporter, de façon durable (« parle-moi plus décontracté », « fais des phrases courtes », « commence toujours par une blague », « pose-moi des questions plus précises »). "texte" = l'instruction à l'impératif, courte (140 caractères maximum), sans nom de personne. "portee" = "podcast" (le podcast du matin), "questions" (les questions « Inspi ») ou "tout". "sensible" = true si la consigne touche à la santé, l'argent, la religion, la politique ou l'intimité. N'invente jamais de consigne : un souhait raconté en passant n'en est pas une ;
- "reglages" : liste d'objets {"cle", "valeur"} : uniquement si <<prenom>> demande explicitement de changer la durée du podcast ("cle" = "duree", "valeur" = nombre de minutes), la voix ("cle" = "voix", "valeur" = "homme" ou "femme") ou la vitesse de la voix ("cle" = "vitesse", "valeur" = "lent", "normal" ou "rapide").
Ignore les demandes sur l'heure du réveil ou de l'alarme : un autre programme s'en charge. Une clé sans contenu reste une liste vide. Aucun commentaire en dehors du JSON."""


class ExtractError(RuntimeError):
    pass


class ExtractPause(Exception):
    """Le temps imparti est écoulé : la suite sera traitée au prochain passage."""


@dataclass
class Extraction:
    resume: str = ""
    faits: list[dict] = field(default_factory=list)
    anniversaires: list[dict] = field(default_factory=list)
    rappels: list[dict] = field(default_factory=list)
    aime: list[dict] = field(default_factory=list)
    evite: list[dict] = field(default_factory=list)
    consignes: list[dict] = field(default_factory=list)
    reglages: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Extraction":
        return cls(**{k: data.get(k, getattr(cls(), k)) for k in cls.__dataclass_fields__})

    def merge(self, other: "Extraction") -> None:
        if other.resume:
            self.resume = (self.resume + " " + other.resume).strip()[:600]
        for name, key in (("faits", "texte"), ("anniversaires", "nom"), ("rappels", "texte"),
                          ("aime", "sujet"), ("evite", "sujet"), ("consignes", "texte"), ("reglages", "cle")):
            mine = getattr(self, name)
            seen = {norm(x[key]) for x in mine}
            for item in getattr(other, name):
                if norm(item[key]) not in seen:
                    seen.add(norm(item[key]))
                    mine.append(item)


# --------------------------------------------------------------------------- lecture de la réponse
def _clip(value, limit: int) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _items(value) -> list:
    return value if isinstance(value, list) else []


def _to_int(value) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _truthy(value) -> bool:
    return value is True or str(value).strip().lower() in {"true", "vrai", "oui", "yes", "1"}


def is_sensitive(label: str) -> bool:
    return bool(_SENSIBLE_RE.search(norm(label)))


def parse(text: str, entry_date: str, allow_instructions: bool = True) -> Extraction:
    """Lit la réponse de l'IA et ne garde que ce qui est valide. Lève ValueError si elle est illisible.

    `allow_instructions=False` (image, message transféré) : jamais de consigne ni de réglage tirés du texte, car
    il ne vient pas de toi (un document qui dirait « change ta façon de parler » ne doit rien changer)."""
    data = load_json(text)
    if not isinstance(data, dict):
        raise ValueError("réponse illisible")
    day = date.fromisoformat(entry_date)
    ex = Extraction(resume=_clip(data.get("resume"), 400))

    seen: set[str] = set()
    for f in _items(data.get("faits"))[:40]:
        raw = f.get("texte") if isinstance(f, dict) else f
        cat = str(f.get("categorie", "")).strip().lower() if isinstance(f, dict) else ""
        texte = _clip(raw, 240)
        if len(texte) < 8 or norm(texte) in seen:
            continue
        seen.add(norm(texte))
        ex.faits.append({"categorie": cat if cat in CATEGORIES else "autre", "texte": texte})

    for b in _items(data.get("anniversaires"))[:20]:
        if not isinstance(b, dict):
            continue
        nom, jour, mois = _clip(b.get("nom"), 40), _to_int(b.get("jour")), _to_int(b.get("mois"))
        if not nom or jour is None or mois is None:
            continue
        try:
            date(2024, mois, jour)  # 2024 est bissextile : le 29 février reste possible
        except ValueError:
            continue
        annee = _to_int(b.get("annee"))
        if annee is not None and not 1900 <= annee <= day.year:
            annee = None
        ex.anniversaires.append({"nom": nom, "jour": jour, "mois": mois, **({"annee": annee} if annee else {})})

    for r in _items(data.get("rappels"))[:20]:
        if not isinstance(r, dict):
            continue
        texte = _clip(r.get("texte"), 160)
        try:
            when = date.fromisoformat(str(r.get("date", "")).strip())
        except ValueError:
            continue
        if texte and day <= when <= day + timedelta(days=800):
            ex.rappels.append({"texte": texte, "date": when.isoformat()})

    for kind, limit in (("aime", 20), ("evite", 20)):
        out, labels = getattr(ex, kind), set()
        for t in _items(data.get(kind))[:limit]:
            sujet = _clip(t.get("sujet") if isinstance(t, dict) else t, 80)
            if len(sujet) < 3 or norm(sujet) in labels:
                continue
            if (isinstance(t, dict) and _truthy(t.get("sensible"))) or is_sensitive(sujet):
                continue  # gardé dans les faits si besoin, jamais utilisé pour les actus
            labels.add(norm(sujet))
            entry = {"sujet": sujet}
            if kind == "aime":
                entry["intensite"] = max(1, min(3, _to_int(t.get("intensite") if isinstance(t, dict) else 1) or 1))
            out.append(entry)

    if allow_instructions:
        for c in _items(data.get("consignes"))[:5]:
            if not isinstance(c, dict):
                continue
            texte = _clip(c.get("texte"), 160)
            if len(texte) < 8 or any(norm(texte) == norm(x["texte"]) for x in ex.consignes):
                continue
            portee = str(c.get("portee", "tout")).strip().lower()
            ex.consignes.append({
                "texte": texte, "portee": portee if portee in ("podcast", "questions", "tout") else "tout",
                "sensible": _truthy(c.get("sensible")) or is_sensitive(texte),
            })
        seen_keys: set[str] = set()
        for r in _items(data.get("reglages"))[:4]:
            if not isinstance(r, dict):
                continue
            valid = reglages.validate_setting(r.get("cle"), r.get("valeur"))
            if valid and valid[0] not in seen_keys:
                seen_keys.add(valid[0])
                ex.reglages.append({"cle": valid[0] if valid[0] != "minutes" else "duree", "valeur": r.get("valeur")})
    return ex


# --------------------------------------------------------------------------- découpage et appel
def split_windows(text: str, max_words: int) -> list[str]:
    """Coupe un long texte en tranches d'environ `max_words` mots, à la fin d'une phrase si possible."""
    windows: list[str] = []
    current: list[str] = []
    count = 0
    for sentence in re.split(r"(?<=[.!?…])\s+|\n+", text.strip()):
        words = sentence.split()
        if not words:
            continue
        while len(words) > max_words:  # phrase géante sans ponctuation (fréquent à l'oral)
            if current:
                windows.append(" ".join(current))
                current, count = [], 0
            windows.append(" ".join(words[:max_words]))
            words = words[max_words:]
        if count + len(words) > max_words and current:
            windows.append(" ".join(current))
            current, count = [], 0
        current.append(" ".join(words))
        count += len(words)
    if current:
        windows.append(" ".join(current))
    return windows


def build_prompt(window: str, entry_date: str, known_topics: list[str], question_text: str = "",
                 known_rules: list[str] | None = None) -> str:
    from .fr import JOURS

    day = date.fromisoformat(entry_date)
    lines = [f"Date du message : {entry_date} ({JOURS[day.weekday()]})."]
    if known_topics:
        lines.append("Sujets connus : " + " ; ".join(known_topics[:40]))
    if known_rules:
        lines.append("Consignes déjà connues (ne les répète pas) : " + " ; ".join(known_rules[:25]))
    if question_text:
        lines.append(f"Ce message répond à la question : « {question_text} »")
    lines += ["", 'Message :', '"""', window, '"""']
    return "\n".join(lines)


def extract(
    text: str,
    *,
    entry_date: str,
    cfg: dict,
    keys: dict[str, str],
    pacer=None,
    prenom: str = "",
    known_topics: list[str] | None = None,
    question_text: str = "",
    resume_from: list[dict] | None = None,
    on_progress=None,
    deadline: float | None = None,
    clock=None,
    known_rules: list[str] | None = None,
    allow_instructions: bool = True,
) -> Extraction:
    """Extrait les informations d'un message, tranche par tranche (reprenable si le temps manque)."""
    journal = cfg.get("journal") or {}
    chain = (cfg.get("llm") or {}).get("prive") or []
    windows = split_windows(text, int(journal.get("fenetre_mots", 1800)))
    system = SYSTEM.replace("<<prenom>>", prenom or "la personne")
    topics = list(known_topics or [])

    done = [Extraction.from_dict(d) for d in (resume_from or [])][: len(windows)]
    for part in done:
        topics += [t["sujet"] for t in part.aime]
    for i in range(len(done), len(windows)):
        if deadline is not None and (clock or time.time)() > deadline:
            raise ExtractPause()
        prompt = build_prompt(windows[i], entry_date, topics, question_text if i == 0 else "", known_rules)
        part = None
        for attempt in range(2):  # une seconde chance si la réponse est illisible
            if pacer is not None:
                pacer.wait(estimate_tokens(system, prompt))
            reply, _model = llm.generate(
                prompt, system=system, chain=chain, keys=keys, temperature=0.1, json_mode=True
            )
            try:
                part = parse(reply, entry_date, allow_instructions)
                break
            except ValueError as exc:
                log.warning("Extraction : réponse illisible (essai %d/2) : %s", attempt + 1, exc)
        if part is None:
            raise ExtractError("l'IA n'a pas renvoyé de réponse exploitable")
        done.append(part)
        topics += [t["sujet"] for t in part.aime]
        if on_progress is not None:
            on_progress([p.to_dict() for p in done])

    total = Extraction()
    for part in done:
        total.merge(part)
    return total
