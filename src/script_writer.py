"""Écriture du texte du podcast à partir des sujets choisis.

Règle d'or : aucune info perso n'est envoyée à l'IA. Le texte perso (anniversaires, rappels) est
fabriqué ailleurs (perso.py) et inséré ici à la place d'un repère [[PERSO]] laissé par le rédacteur.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime

from . import llm
from .fr import count_words, date_fr
from .triage import Choice

log = logging.getLogger(__name__)

MARKER = "[[PERSO]]"
_ANY_MARKER_RE = re.compile(r"\[\[[^\]]*\]\]")

# Mots qui rendent un réveil anxiogène. « drame » est volontairement absent (genre de film).
DRAMATIC_RE = re.compile(
    r"\b(trag\w+|horrib\w+|horrifi\w+|atroce\w*|effroy\w+|bouleversan\w+|catastroph\w+|massacre\w*"
    r"|carnage\w*|cauchemar\w*|bain de sang|panique\w*|terrifi\w+|choquant\w*|sous le choc)\b",
    re.IGNORECASE,
)

SYSTEM = """Tu es la voix d'un mini-podcast du matin : chaleureuse, positive et posée, elle réveille son auditeur en douceur. Tu écris UNIQUEMENT le texte lu à voix haute, rien d'autre.

Forme
- Environ {mots} mots (≈ {minutes} minutes à l'oral), jamais plus de {max_mots}. C'est un plafond : s'il y a peu de sujets, fais plus court plutôt que de remplir.
- Français parlé et naturel, tutoiement, phrases courtes. Pas de markdown, de liste, de titre, d'émoji, d'URL ni de didascalie.
- Commence par un bonjour bref avec le jour et la date.{bloc_perso} Termine par une courte phrase positive pour bien démarrer la journée.
- Écris les chiffres, symboles et sigles de façon facile à lire à voix haute (« pour cent », « euros »…).

Contenu
- {regle_sujets}
- Pour chaque sujet : l'essentiel d'abord, puis ce que cela change ou pourquoi c'est intéressant, en une à trois phrases. Reformule avec tes mots, n'invente rien.
- Ne cite la source que si cela aide à juger de la fiabilité.

Ton
- Réveil positif : mets en avant ce qui est encourageant, avec un enthousiasme naturel (pas d'exclamation à chaque phrase).
- Pour un sujet marqué « difficile » : une ou deux phrases factuelles et calmes, sans adjectif dramatique, sans détail choquant ni chiffre macabre ; dis ce qui est fait ou ce que cela change concrètement, puis enchaîne naturellement. Ne termine jamais sur lui.
- Évite les mots dramatiques : tragique, horrible, atroce, catastrophe, massacre, cauchemar, panique…"""

PERSO_RULE = (
    " Juste après, écris sur sa propre ligne exactement [[PERSO]] : le programme y insérera des infos "
    "personnelles. N'écris rien à leur sujet et enchaîne ensuite naturellement avec l'actualité."
)
RULE_ORDRE = "Raconte uniquement les sujets fournis, dans l'ordre donné, sans en ajouter. Regroupe ceux qui parlent du même thème."
RULE_LIBRE = (
    "Parmi les actualités fournies, retiens les 4 à 6 plus importantes ou intéressantes (priorité aux bonnes "
    "nouvelles et aux sujets utiles), regroupe celles qui parlent du même thème et ignore le reste."
)


def _tone_tag(c: Choice) -> str:
    if c.ton == "positif":
        return "[positif]"
    if c.ton == "difficile":
        return "[difficile : 1 à 2 phrases sobres]"
    return "[neutre]"


def build_prompt(choices: list[Choice], *, libre: bool, prenom: str, now: datetime, calme: bool) -> str:
    lines = [f"Nous sommes le {date_fr(now)}."]
    if prenom:
        lines.append(f"L'auditeur s'appelle {prenom} : utilise son prénom dans le bonjour.")
    if calme:
        lines.append("Journée plutôt calme côté actualité : ne force rien, reste bref.")
    lines.append("")
    if libre:
        lines.append("Actualités récentes :")
        lines += [f"- {c.item.brief(280)}" for c in choices]
    else:
        lines.append("Sujets du jour, dans l'ordre à respecter :")
        lines += [f"{n}. {_tone_tag(c)} {c.item.brief(280)}" for n, c in enumerate(choices, 1)]
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


def dramatic_hits(text: str) -> list[str]:
    return sorted({m.group(0).lower() for m in DRAMATIC_RE.finditer(text)})


def insert_perso(text: str, segment: str) -> str:
    """Place le texte perso à l'endroit du repère ; sinon après le bonjour. Ne laisse jamais de repère."""
    if MARKER in text:
        before, _, after = text.partition(MARKER)
        after = after.replace(MARKER, "")
        parts = [before.strip(), segment.strip(), after.strip()]
        text = "\n\n".join(p for p in parts if p)
    elif segment.strip():
        first, sep, rest = text.partition("\n\n")
        if sep and count_words(first) <= 40:  # premier paragraphe = simple bonjour
            text = f"{first}\n\n{segment.strip()}\n\n{rest.strip()}"
        else:
            m = re.search(r"(?<=[.!?])\s+", text)
            if m:
                text = f"{text[:m.start()]}\n\n{segment.strip()}\n\n{text[m.end():].strip()}"
            else:
                text = f"{text}\n\n{segment.strip()}"
    return _ANY_MARKER_RE.sub("", text).strip()


def write_script(
    choices: list[Choice],
    cfg: dict,
    keys: dict[str, str],
    now: datetime,
    *,
    prenom: str = "",
    perso_text: str = "",
    libre: bool = False,
    calme: bool = False,
) -> tuple[str, str]:
    """Retourne (texte_du_podcast, modèle_utilisé)."""
    target = int(cfg.get("mots_cibles", 300))
    budget = max(120, target - count_words(perso_text))
    max_words = int(budget * 1.25)
    llm_cfg = cfg.get("llm") or {}
    chain = llm_cfg.get("ecriture") or []
    temperature = float(llm_cfg.get("temperature", 0.7))

    system = SYSTEM.format(
        mots=budget,
        minutes=f"{budget / 150:g}",
        max_mots=max_words,
        bloc_perso=PERSO_RULE if perso_text else "",
        regle_sujets=RULE_LIBRE if libre else RULE_ORDRE,
    )
    prompt = build_prompt(choices, libre=libre, prenom=prenom, now=now, calme=calme)
    text, model = llm.generate(prompt, system=system, chain=chain, keys=keys, temperature=temperature)
    text = clean_script(text)

    keep = f" Conserve tel quel le repère {MARKER} s'il est présent."

    hits = dramatic_hits(text)
    if hits:  # garde-fou « pas dramatique » : une seule réécriture
        log.info("Mots trop dramatiques (%s) : réécriture", ", ".join(hits))
        try:
            softer, _ = llm.generate(
                "Réécris ce texte en gardant exactement les mêmes informations, la même longueur et le même "
                f"style parlé, mais remplace les mots ou expressions trop dramatiques ({', '.join(hits)}) par "
                f"des formulations sobres et factuelles.{keep} Renvoie uniquement le texte :\n\n{text}",
                system=system, chain=chain, keys=keys, temperature=temperature,
            )
            softer = clean_script(softer)
            if softer and len(dramatic_hits(softer)) < len(hits):
                text = softer
        except llm.LLMError as exc:
            log.warning("Réécriture impossible : %s", exc)

    words = count_words(text)
    if words > max_words:  # un seul essai de raccourcissement
        log.info("Texte trop long (%d mots), je demande de raccourcir", words)
        try:
            shorter, _ = llm.generate(
                f"Raccourcis ce texte à environ {budget} mots, même style parlé, sans rien inventer.{keep} "
                f"Renvoie uniquement le texte :\n\n{text}",
                system=system, chain=chain, keys=keys, temperature=temperature,
            )
            shorter = clean_script(shorter)
            if 0 < count_words(shorter) < words:
                text = shorter
        except llm.LLMError as exc:
            log.warning("Raccourcissement impossible : %s", exc)

    return insert_perso(text, perso_text), model
