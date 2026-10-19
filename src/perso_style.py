"""Tes consignes sur la façon de dire tes infos perso (cours, anniversaires, rappels).

Les phrases perso sont fabriquées par le programme (perso.py, edt.py) : elles sont sûres, mais toujours dites de la même
façon. Quand tu as donné une consigne qui les concerne (« cite mes cours sans la salle », « souhaite les anniversaires avec
plus de chaleur », « dis les cours en commençant par l'heure »), ce module les fait reformuler par Groq (JAMAIS par Gemini),
puis VÉRIFIE le résultat : aucun chiffre nouveau, aucun nom ajouté, aucun jour changé, aucun cours ou prénom perdu (sauf si
ta consigne demande de l'omettre ou de le résumer). Si la vérification échoue deux fois, ou si Groq ne répond pas, le texte
du programme est dit tel quel et le podcast le signale : une consigne mal appliquée ne doit jamais fausser ton matin.

Sans consigne qui concerne tes infos perso, rien n'est envoyé nulle part : le texte reste celui du programme.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from . import llm
from .jsonutil import load_json
from .memoire import norm
from .pacer import estimate_tokens

log = logging.getLogger(__name__)

SYSTEM = """Tu réécris un court passage lu à voix haute dans le podcast du matin de <<prenom>> : ses informations personnelles du jour (cours, anniversaires, rappels). Tu appliques ses consignes sur la façon de les dire.

Règles absolues
- Tu ne changes JAMAIS un fait : chiffres, heures, jours (aujourd'hui, demain, dans N jours), noms de personnes, titres de cours, lieux. Tu n'inventes rien et tu n'ajoutes aucune information.
- Chaque information du texte d'origine reste dite, sauf si une consigne demande de l'omettre ou de la résumer.
- Tutoiement, français parlé et naturel, phrases faciles à lire à voix haute. Garde les heures et les nombres en chiffres (9 h 30). Pas de markdown, de liste, d'émoji ni de titre.
- Ne dis ni bonjour ni au revoir : ce passage s'insère au milieu du podcast. N'ajoute pas de conclusion.
- Ne dépasse pas la longueur du texte d'origine de plus de moitié.
- N'applique que les consignes qui concernent la FAÇON DE DIRE ces informations (ton, registre, longueur, détails à citer ou à omettre, ordre) ; ignore les autres.

Réponds par un objet JSON : {"texte": "le passage réécrit"}."""

RETRY = ("\n\nTa réponse précédente a été refusée par le contrôle : {problemes}. Recommence en corrigeant cela. "
         "Si tu n'y arrives pas, renvoie le texte d'origine tel quel.")


@dataclass
class Result:
    text: str                  # le texte à dire : reformulé, ou celui du programme
    status: str = ""           # "" : rien à faire · "applique" · "repli : <raison>"
    rules: int = 0             # nombre de consignes qui concernaient les infos perso
    attempts: int = 0

    @property
    def fallback(self) -> bool:
        return self.status.startswith("repli")


# --------------------------------------------------------------------------- les nombres, en chiffres ou en lettres
_UNITS = {"zero": 0, "un": 1, "une": 1, "deux": 2, "trois": 3, "quatre": 4, "cinq": 5, "six": 6, "sept": 7, "huit": 8, "neuf": 9}
_TEENS = {"dix": 10, "onze": 11, "douze": 12, "treize": 13, "quatorze": 14, "quinze": 15, "seize": 16}
_TENS = {"vingt": 20, "vingts": 20, "trente": 30, "quarante": 40, "cinquante": 50, "soixante": 60}
_NUMBER_WORDS = set(_UNITS) | set(_TEENS) | set(_TENS) | {"cent", "cents"}


def _word_value(span: list[str]) -> int:
    """« dix sept » → 17, « quatre vingt dix » → 90, « vingt deux » → 22, « deux cent quatre » → 204."""
    total, i = 0, 0
    while i < len(span):
        word = span[i]
        if word == "quatre" and i + 1 < len(span) and span[i + 1] in ("vingt", "vingts"):
            total, i = total + 80, i + 2
            continue
        if word in ("cent", "cents"):
            total = (total or 1) * 100
        else:
            total += _UNITS.get(word) or _TEENS.get(word) or _TENS.get(word) or 0
        i += 1
    return total


def numbers_in(text: str) -> set[int]:
    """Tous les nombres d'un texte, écrits en chiffres (9, 30) ou en lettres (neuf, trente, vingt et un)."""
    values = {int(d) for d in re.findall(r"\d+", text)}
    for phrase in re.split(r"[,.;:!?()«»\"“”…\n]+", text):          # une virgule sépare deux nombres : « neuf heures trente, vingt »
        tokens = re.findall(r"[a-z]+", norm(phrase))
        i = 0
        while i < len(tokens):
            if tokens[i] not in _NUMBER_WORDS:
                i += 1
                continue
            j = i
            while j + 1 < len(tokens) and (
                tokens[j + 1] in _NUMBER_WORDS
                or (tokens[j + 1] == "et" and j + 2 < len(tokens) and tokens[j + 2] in ("un", "une", "onze"))
            ):
                j += 1
            span = [t for t in tokens[i:j + 1] if t != "et"]
            if not (len(span) == 1 and span[0] in ("un", "une")):      # « un », « une » seuls : des articles
                values.add(_word_value(span))
            i = j + 1
    return values


# --------------------------------------------------------------------------- le contrôle
# Une consigne qui demande de ne pas tout dire change ce qu'on doit retrouver dans la version reformulée.
#  - « seulement le premier cours », « résume », « ne cite plus mes cours » : on peut laisser des cours ou des prénoms de côté ;
#  - « sans la salle », « pas de prof », « enlève l'heure de fin » : on retire un DÉTAIL, mais chaque cours, prénom et rappel
#    doit rester dit (un cours oublié, c'est un matin faussé).
_OMIT_ITEMS_RE = re.compile(
    r"\b(seulement|uniquement|juste|resum\w*|raccourci\w*|bref|brefs|premier|premiere|prochain|prochaine|nombre|combien)\b"
)
_OMIT_VERB_RE = re.compile(
    r"\b(sans|pas de|plus besoin|pas besoin|omet\w*|oublie\w*|saute\w*|ignore\w*|evite\w*|retire\w*|enleve\w*|supprime\w*|"
    r"(?:ne|n) (?:me )?(?:cite|citer|dis|dire|parle|parler|mentionne\w*|rappelle\w*|lis|indique\w*|precise\w*|donne\w*)\w* "
    r"(?:pas|plus|jamais))\b"
)
_DETAIL_WORDS = frozenset(
    "salle salles prof profs professeur professeurs enseignant enseignants intervenant intervenants lieu lieux local locaux "
    "amphi amphis studio studios heure heures horaire horaires detail details precision precisions adresse adresses etage "
    "etages batiment batiments fin duree".split()
)
_RELATIVE_DAYS = ("aujourd hui", "demain", "hier")
_STOP = frozenset("pour dans avec cette votre notre leur leurs mais donc alors plus tout tous toute toutes sont etre avoir faire".split())
_EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF☀-➿️‍]")


def allows_omission(rules: list[str]) -> bool:
    """Tes consignes autorisent-elles à ne pas redire chaque cours, prénom ou rappel ? (Pas pour un simple détail retiré.)"""
    text = norm(" ; ".join(rules))
    if _OMIT_ITEMS_RE.search(text):
        return True
    for found in _OMIT_VERB_RE.finditer(text):
        words = re.findall(r"[a-z0-9]+", text[found.end():].split(";")[0])[:4]
        if not any(word in _DETAIL_WORDS for word in words):           # « sans la salle » : un détail ; « sans les cours du matin » : non
            return True
    return False


def _anchor_kept(kind: str, value: str, out_norm: str, out_words: list[str]) -> bool:
    key = norm(value)
    if not key:
        return True
    if kind in ("nom", "cours"):
        return key in out_norm
    words = [w for w in key.split() if len(w) >= 4 and w not in _STOP]           # un rappel : l'essentiel de ses mots
    if not words:
        return key in out_norm
    hits = sum(1 for w in words if any(o.startswith(w[:5]) for o in out_words))
    return hits / len(words) >= 0.6


def check(original: str, output: str, anchors: list[tuple[str, str]], rules: list[str]) -> list[str]:
    """Ce qui cloche dans `output` par rapport à `original` (liste vide = tout est bon)."""
    problems: list[str] = []
    out_norm, in_norm = norm(output), norm(original)
    out_words, in_words = out_norm.split(), set(in_norm.split())
    omit = allows_omission(rules)

    if re.search(r"\[\[|\]\]|https?://|[*#`_]{1}", output):
        problems.append("du balisage ou un lien s'est glissé dans le texte")
    words_in, words_out = len(original.split()), len(output.split())
    if words_out > max(words_in * 1.6, words_in + 25):
        problems.append(f"trop long ({words_out} mots pour {words_in} à l'origine)")
    if not omit:
        if words_out == 0:
            problems.append("texte vide")
        elif words_out < words_in * 0.25:
            problems.append("beaucoup trop court")

    allowed = numbers_in(original) | set(range(0, max(len(anchors), 1) + 1))        # « tu as 3 cours » est un calcul permis
    extra = sorted(numbers_in(output) - allowed)
    if extra:
        problems.append("nombre ajouté ou changé : " + ", ".join(str(n) for n in extra[:4]))

    for day in _RELATIVE_DAYS:
        if day in out_norm and day not in in_norm:
            problems.append(f"« {day.replace('aujourd hui', 'aujourd’hui')} » n'était pas dans le texte d'origine")

    foreign: list[str] = []
    for sentence in re.split(r"(?<=[.!?…:;])\s+|\n+|[«»\"“”]", output):
        letters = re.findall(r"[^\W\d_]+", sentence)
        for index, word in enumerate(letters):
            if index and word[0].isupper() and not (word.isupper() and len(word) <= 3):
                key = norm(word)
                if key and key not in in_words and word not in foreign:
                    foreign.append(word)
    if foreign:
        problems.append("mot propre ajouté : " + ", ".join(foreign[:4]))

    if not omit:
        missing = [value for kind, value in anchors if not _anchor_kept(kind, value, out_norm, out_words)]
        if missing:
            problems.append("il manque : " + ", ".join(missing[:4]))
    return problems


# --------------------------------------------------------------------------- la reformulation
def _read(reply: str) -> str | None:
    data = load_json(reply)
    text = data.get("texte") if isinstance(data, dict) else None
    if not isinstance(text, str):
        return None
    text = _EMOJI_RE.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


def build_prompt(text: str, rules: list[str]) -> str:
    return "Consignes :\n" + "\n".join(f"- {r}" for r in rules[:8]) + '\n\nTexte d\'origine :\n"""\n' + text + '\n"""'


def restyle(text: str, rules: list[str], *, cfg: dict, keys: dict[str, str], prenom: str = "",
            anchors: list[tuple[str, str]] | None = None, pacer=None) -> Result:
    """Applique tes consignes de `rules` au texte perso `text`. Ne lève jamais : au pire, `text` revient tel quel."""
    anchors = anchors or []
    if not text.strip() or not rules:
        return Result(text, "", len(rules))
    # Ces phrases contiennent tes prénoms, tes cours, tes rendez-vous : Groq seulement, quoi que dise la configuration.
    chain = [e for e in ((cfg.get("llm") or {}).get("prive") or []) if str(e.get("fournisseur", "")).lower() == "groq"]
    if not chain:
        return Result(text, "repli : aucun modèle Groq dans la configuration", len(rules))
    system = SYSTEM.replace("<<prenom>>", prenom or "l'auditeur")
    base = build_prompt(text, rules)
    problems: list[str] = []
    for attempt in (1, 2):
        prompt = base + (RETRY.format(problemes=" ; ".join(problems[:4])) if problems else "")
        try:
            if pacer is not None:
                pacer.wait(estimate_tokens(system, prompt))
            reply, _model = llm.generate(prompt, system=system, chain=chain, keys=keys, temperature=0.3, json_mode=True)
        except llm.LLMError as exc:
            log.warning("Reformulation perso impossible : %s", str(exc).splitlines()[0])
            return Result(text, "repli : l'IA n'a pas répondu", len(rules), attempt)
        candidate = _read(reply)
        if candidate is None:
            problems = ["réponse illisible (JSON attendu avec la clé « texte »)"]
            continue
        problems = check(text, candidate, anchors, rules)
        if not problems:
            return Result(candidate, "applique", len(rules), attempt)
        log.info("Reformulation perso refusée (essai %d) : %s", attempt, " ; ".join(problems))
    return Result(text, "repli : " + " ; ".join(problems[:3]), len(rules), 2)
