"""Le bouton « 💡 Inspi » : quand tu n'as plus d'inspiration, l'IA te pose une question.

Au début, les questions viennent d'une banque (elles servent à remplir la mémoire sur les bases).
Dès que la mémoire contient assez d'infos, c'est l'IA (Groq, en privé) qui invente une question à partir
de ce qu'elle sait de toi : un thème peu exploré, ou une suite à ce que tu as dit récemment.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime

from . import llm
from .fr import date_fr
from .jsonutil import load_json
from .memoire import Memoire, norm
from .pacer import estimate_tokens

log = logging.getLogger(__name__)

MIN_FACTS_FOR_AI = 10
GENERIC = "Raconte-moi ta journée : qu'est-ce qui t'a marqué aujourd'hui ?"

# (identifiant, catégorie, question) — la catégorie est celle des « faits » de la mémoire.
BANQUE: list[tuple[str, str, str]] = [
    ("b01", "personne", "Pour commencer, raconte-moi qui tu es : ton prénom, ton âge, d'où tu viens et ce que tu fais en ce moment dans la vie."),
    ("b02", "etudes", "Parle-moi de ton école : ce que tu y étudies, ce que tu préfères, et ce qui te pèse un peu en ce moment."),
    ("b03", "projet", "Quel est le projet qui te prend le plus de temps ou de tête en ce moment ?"),
    ("b04", "gout", "Quels sont les trois films ou réalisateurs qui t'ont le plus marqué, et pourquoi ?"),
    ("b05", "personne", "Parle-moi des personnes qui comptent le plus pour toi : qui sont-elles, et qu'est-ce qu'elles représentent ?"),
    ("b06", "evenement", "Y a-t-il des anniversaires ou des dates importantes à ne surtout pas oublier dans les prochaines semaines ?"),
    ("b07", "habitude", "À quoi ressemble une journée normale pour toi, du réveil jusqu'au coucher ?"),
    ("b08", "habitude", "Comment aimerais-tu que tes matins se passent idéalement ? Ce que tu voudrais entendre, et ce que tu voudrais éviter."),
    ("b09", "gout", "Quels sujets d'actualité t'intéressent vraiment, et lesquels tu ne supportes plus d'entendre ?"),
    ("b10", "objectif", "Où te vois-tu dans cinq ans ? Quel métier, quelle vie ?"),
    ("b11", "etudes", "Quelles échéances approchent : rendus, tournages, examens ? Dis-moi les dates si tu les connais."),
    ("b12", "gout", "À quoi joues-tu en ce moment, et qu'est-ce qui te plaît dedans ?"),
    ("b13", "projet", "Qu'est-ce que tu apprends en ce moment en 3D, en image ou en création, et où veux-tu aller avec ça ?"),
    ("b14", "materiel", "Quel matériel utilises-tu pour tes projets, et qu'est-ce que tu aimerais t'offrir un jour ?"),
    ("b15", "lieu", "Où habites-tu, et qu'est-ce que tu aimes ou pas dans ton quartier et tes trajets ?"),
    ("b16", "personne", "Qui sont tes amis les plus proches, et qu'est-ce que vous faites ensemble ?"),
    ("b17", "gout", "Qu'est-ce que tu écoutes comme musique en ce moment ?"),
    ("b18", "evenement", "Raconte-moi un souvenir qui te rend heureux quand tu y repenses."),
    ("b19", "objectif", "Quel objectif aimerais-tu atteindre d'ici la fin de l'année ?"),
    ("b20", "lieu", "Un endroit que tu rêves de visiter, et pourquoi celui-là ?"),
    ("b21", "habitude", "Qu'est-ce que tu fais pour te détendre quand tu n'as rien de prévu ?"),
    ("b22", "projet", "Quel est le dernier truc technique que tu as appris et dont tu es fier ?"),
    ("b23", "gout", "Quelque chose que tu as découvert récemment — un film, un outil, une idée — et qui t'a marqué ?"),
    ("b24", "evenement", "Qu'est-ce qui t'a fait sourire cette semaine ?"),
    ("b25", "objectif", "Quelle est la prochaine chose que tu as hâte de faire ?"),
]

SYSTEM = """Tu es l'interlocuteur bienveillant d'un journal vocal. Ta seule tâche : poser UNE seule question à <<prenom>> pour l'aider à raconter sa vie et à enrichir sa mémoire personnelle.

Règles
- Une seule question, en français parlé, au tutoiement, chaleureuse, de 25 mots maximum, sans préambule ni explication ;
- elle ouvre un sujet que la mémoire connaît peu, ou approfondit ce qui a été dit récemment, de façon à obtenir une vraie réponse développée (évite les questions fermées oui/non) ;
- ne répète pas une question déjà posée ; n'invente aucun fait : appuie-toi uniquement sur la mémoire fournie ;
- évite les sujets intimes (santé, argent, relations amoureuses) sauf si la personne vient d'en parler elle-même ;
- tiens compte du moment de la journée.
Réponds en JSON : {"question": "...", "categorie": "..."} avec une catégorie parmi : personne, projet, etudes, gout, habitude, objectif, evenement, lieu, materiel, autre."""


def _words(text: str) -> set[str]:
    return {w for w in norm(text).split() if len(w) > 2}


def too_close(question: str, previous: list[str], threshold: float = 0.7) -> bool:
    a = _words(question)
    for old in previous:
        b = _words(old)
        if a and b and len(a & b) / len(a | b) >= threshold:
            return True
    return False


def pick_from_bank(mem: Memoire) -> dict | None:
    """La question de la banque jamais posée qui comble le thème le moins rempli de la mémoire."""
    asked = {q.get("banque_id") for q in mem.questions()}
    counts = mem.stats()["categories"]
    best: tuple[int, int, tuple[str, str, str]] | None = None
    for order, item in enumerate(BANQUE):
        if item[0] in asked:
            continue
        key = (counts.get(item[1], 0), order)
        if best is None or key < best[:2]:
            best = (key[0], key[1], item)
    if best is None:
        return None
    qid, cat, text = best[2]
    return {"texte": text, "categorie": cat, "origine": "banque", "banque_id": qid}


def ask_ai(mem: Memoire, cfg: dict, keys: dict[str, str], now: datetime, pacer, prenom: str, pending: str,
           consignes: list[str] | None = None) -> dict | None:
    previous = [q.get("texte", "") for q in mem.questions()]
    system = SYSTEM.replace("<<prenom>>", prenom or "la personne")
    if consignes:
        system += "\n\nConsignes de " + (prenom or "la personne") + " sur la façon de lui poser des questions (à respecter) :\n" \
            + "\n".join(f"- {c}" for c in consignes[:8])
    prompt = f"Moment : {date_fr(now)}, {now.hour} h.\n\n{mem.digest()}"
    if pending:
        prompt += f"\n\nLa dernière question est restée sans réponse : « {pending} » — pose-en une autre."
    chain = (cfg.get("llm") or {}).get("prive") or []
    for _ in range(2):
        if pacer is not None:
            pacer.wait(estimate_tokens(system, prompt, output=400))
        try:
            reply, _model = llm.generate(prompt, system=system, chain=chain, keys=keys, temperature=0.8, json_mode=True)
        except llm.LLMError as exc:
            log.warning("Question par l'IA impossible : %s", str(exc).splitlines()[0])
            return None
        data = load_json(reply)
        question = re.sub(r"\s+", " ", str((data or {}).get("question", "")) if isinstance(data, dict) else "").strip()
        if 15 <= len(question) <= 280 and "?" in question and not too_close(question, previous):
            cat = str(data.get("categorie", "autre")).strip().lower()
            return {"texte": question, "categorie": cat or "autre", "origine": "ia"}
        log.info("Question de l'IA refusée (trop proche d'une ancienne, ou mal formée)")
    return None


def make_question(mem: Memoire, cfg: dict, keys: dict[str, str], now: datetime, *, pacer=None,
                  prenom: str = "", pending: str = "", consignes: list[str] | None = None) -> dict:
    """Une question prête à poser : {texte, categorie, origine[, banque_id]}. Ne lève jamais d'exception."""
    use_ai_first = mem.stats()["faits"] >= MIN_FACTS_FOR_AI
    if use_ai_first:
        q = ask_ai(mem, cfg, keys, now, pacer, prenom, pending, consignes)
        if q:
            return q
    q = pick_from_bank(mem)
    if q:
        return q
    if not use_ai_first:  # banque épuisée avant d'avoir assez d'infos : on tente l'IA quand même
        q = ask_ai(mem, cfg, keys, now, pacer, prenom, pending, consignes)
        if q:
            return q
    return {"texte": GENERIC, "categorie": "autre", "origine": "secours"}
