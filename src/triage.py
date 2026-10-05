"""Tri des actus : importance, intérêt et ton, puis choix des sujets du jour.

Le modèle (Gemini) ne fait que NOTER chaque article, en JSON. Le choix, l'ordre et les plafonds
(sujets difficiles, catégories) sont décidés ici, par du code simple et testable.
Aucune donnée perso n'intervient dans cette étape : seulement des titres de presse et des centres d'intérêt.

D'où viennent les centres d'intérêt ?
  1. ceux que tu épingles toi-même dans moi.yaml ;
  2. ceux que la mémoire a appris de ton journal vocal (memoire/interets.yaml) ;
  3. tant que la mémoire est trop maigre : une liste PROVISOIRE de départ (moi.yaml → provisoires).
"""
from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import yaml

from . import llm
from .fr import date_fr
from .jsonutil import load_json
from .news import Item

log = logging.getLogger(__name__)

MIN_APPRIS = 3  # nombre de sujets appris à partir duquel la liste provisoire est abandonnée

DEFAULT_PROVISOIRES: list[dict] = [
    {"sujet": "cinéma et image : caméras, optiques, lumière, étalonnage, tournages, festivals", "poids": 3},
    {"sujet": "3D et effets visuels : Houdini, Blender, moteurs temps réel, rendu", "poids": 3},
    {"sujet": "réalité virtuelle : casques VR, jeux VR, nouveautés matérielles", "poids": 2},
    {"sujet": "jeux vidéo, Minecraft et modding", "poids": 2},
    {"sujet": "intelligence artificielle et nouvelles technologies utiles", "poids": 2},
    {"sujet": "sciences, espace, santé et découvertes", "poids": 1},
]
DEFAULT_IMPORTANT = [
    "événements majeurs qui changent le quotidien en France : transports en Île-de-France, énergie, "
    "santé publique, économie du quotidien, vie étudiante",
    "grandes avancées scientifiques ou technologiques",
    "événements mondiaux majeurs",
]
DEFAULT_IGNORER = [
    "polémiques et petites phrases politiques sans effet concret",
    "faits divers locaux, people, télé-réalité",
    "bons plans, promos, codes promo, concours, contenus sponsorisés",
]

TRI_SYSTEM = """Tu es le rédacteur en chef d'un bulletin d'actualité du matin conçu pour UNE seule personne, décrite plus bas. Tu notes des articles de presse. Tu ne rédiges rien : tu réponds uniquement par un objet JSON valide.

Pour chaque article, donne :
- "id" : le numéro de l'article ;
- "importance" (0 à 10) : impact réel et durable sur la vie de cette personne ou sur le monde. 9-10 = événement majeur à connaître absolument, 7-8 = important, 4-6 = utile, 0-3 = anecdotique. Le bruit médiatique du jour, les polémiques et les petites phrases ne comptent pas ;
- "interet" (0 à 10) : à quel point le sujet correspond aux centres d'intérêt de la personne (tiens compte des poids) ;
- "ton" : "positif" (bonne nouvelle, avancée, découverte, sortie, réussite), "difficile" (mort, violence, guerre, attentat, catastrophe, accident grave, crime, scandale, sujet anxiogène) ou "neutre" ;
- "groupe" : un entier ; deux articles qui racontent la même histoire reçoivent le même entier, les autres des entiers différents ;
- "ignorer" : true si l'article entre dans « À ignorer », s'il est publicitaire (promo, bon plan, concours), ou s'il n'apporte aucune information nouvelle.

Réponds exactement sous la forme : {"articles":[{"id":1,"importance":6,"interet":8,"ton":"positif","groupe":1,"ignorer":false}]} avec un objet par article, et rien d'autre."""


# --------------------------------------------------------------------------- données
@dataclass
class Score:
    importance: float
    interet: float
    ton: str
    groupe: str
    ignorer: bool


@dataclass
class Choice:
    item: Item
    importance: float
    interet: float
    ton: str
    score: float
    couverture: int = 1


@dataclass
class Rejected:
    item: Item
    importance: float
    interet: float
    reason: str


@dataclass
class TriResult:
    ok: bool
    choices: list[Choice]
    rejected: list[Rejected] = field(default_factory=list)
    modele: str = ""
    nb_actus: int = 0
    calme: bool = False
    libre: bool = False  # tri en échec : le rédacteur choisit lui-même parmi les actus
    note: str = ""


# --------------------------------------------------------------------------- profil
def _read_yaml(path: Path):
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception as exc:
        log.warning("%s illisible (%s) : ignoré", path.name, exc)
        return None


def _topics(raw) -> list[dict]:
    """Normalise une liste de sujets (textes simples ou {sujet, poids}) en [{sujet, poids}]."""
    out: list[dict] = []
    for entry in raw if isinstance(raw, list) else []:
        if isinstance(entry, dict):
            sujet = str(entry.get("sujet") or "").strip()
            try:
                poids = max(1, min(3, int(entry.get("poids", 2))))
            except (TypeError, ValueError):
                poids = 2
        else:
            sujet, poids = str(entry or "").strip(), 2
        if sujet:
            out.append({"sujet": sujet, "poids": poids})
    return out


def _texts(raw, default: list[str]) -> list[str]:
    if not isinstance(raw, list):
        return list(default)
    return [str(v).strip() for v in raw if str(v or "").strip()]


def load_profile(racine: Path) -> dict:
    """Profil de lecture : sujets épinglés + sujets appris par la mémoire (sinon sujets provisoires)."""
    racine = Path(racine)
    moi = _read_yaml(racine / "moi.yaml")
    moi = moi if isinstance(moi, dict) else {}
    pinned = _topics(moi.get("epingles"))
    provisional = _topics(moi.get("provisoires")) if "provisoires" in moi else list(DEFAULT_PROVISOIRES)
    appris = _read_yaml(racine / "memoire" / "interets.yaml")
    appris = appris if isinstance(appris, dict) else {}
    learned = _topics(appris.get("aime"))
    avoided = _texts(appris.get("evite"), [])  # sujets que ton journal a appris à éviter

    if len(learned) >= MIN_APPRIS:
        topics = pinned + learned
        source = "memoire"
        origine = f"mémoire ({len(learned)} sujets appris)" + (f" + {len(pinned)} épinglés" if pinned else "")
    else:
        topics = pinned + learned + provisional
        source = "provisoire"
        origine = "PROVISOIRE (choisi par Claude, pas par ta mémoire) : le journal vocal n'a pas encore assez appris"
    return {
        "prenom": str(moi.get("prenom") or "").strip(),
        "priorites": topics,
        "toujours_important": _texts(moi.get("toujours_important"), DEFAULT_IMPORTANT),
        "a_ignorer": _texts(moi.get("a_ignorer"), DEFAULT_IGNORER) + avoided,
        "origine": origine,
        "source": source,
    }


def profile_block(profile: dict) -> str:
    lines = ["Profil de la personne :"]
    prenom = str(profile.get("prenom") or "").strip()
    if prenom:
        lines.append(f"- Prénom : {prenom}")
    lines.append("- Centres d'intérêt (poids de 1 à 3) :")
    for p in profile.get("priorites") or []:
        lines.append(f"  * [{p.get('poids', 2)}] {p.get('sujet', '')}")
    for key, title in (("toujours_important", "Toujours important pour la personne"), ("a_ignorer", "À ignorer")):
        lines.append(f"- {title} :")
        lines += [f"  * {v}" for v in profile.get(key) or []]
    return "\n".join(lines)


def build_prompt(items: list[Item], profile: dict, now: datetime) -> tuple[str, str]:
    lines = [profile_block(profile), "", f"Date : {date_fr(now)}.", "", "Articles :"]
    lines += [f"{i.id} | {i.brief(120)}" for i in items]
    return TRI_SYSTEM, "\n".join(lines)


# --------------------------------------------------------------------------- lecture de la réponse
_OBJ_RE = re.compile(r"\{[^{}]*\}")


def _num(value) -> float:
    try:
        x = float(str(value).replace(",", "."))
    except ValueError:
        return 0.0
    return max(0.0, min(10.0, x))


def _ton(value) -> str:
    s = str(value or "").strip().lower()
    if s.startswith(("posit", "good", "bon")):
        return "positif"
    if s.startswith(("diff", "neg", "nég", "grave", "anxi")):
        return "difficile"
    return "neutre"


def _bool(value) -> bool:
    return value is True or str(value).strip().lower() in {"true", "vrai", "oui", "yes", "1"}


def parse_scores(text: str, n_items: int) -> dict[int, Score]:
    """Lit la réponse du modèle. Tolère les ```json```, une liste nue, des synonymes, un JSON tronqué."""
    data = load_json(text)
    entries: list = []
    if isinstance(data, dict):
        for key in ("articles", "items", "notes", "resultats"):
            if isinstance(data.get(key), list):
                entries = data[key]
                break
    elif isinstance(data, list):
        entries = data
    if not entries:  # JSON tronqué ou entouré de texte : on récupère les objets complets
        for match in _OBJ_RE.finditer(text):
            try:
                obj = json.loads(match.group(0))
            except ValueError:
                continue
            if isinstance(obj, dict) and "id" in obj:
                entries.append(obj)

    scores: dict[int, Score] = {}
    for e in entries:
        if not isinstance(e, dict):
            continue
        try:
            idx = int(e.get("id"))
        except (TypeError, ValueError):
            continue
        if not 1 <= idx <= n_items:
            continue
        scores[idx] = Score(
            importance=_num(e.get("importance")),
            interet=_num(e.get("interet", e.get("intérêt"))),
            ton=_ton(e.get("ton")),
            groupe=str(e.get("groupe", "")).strip(),
            ignorer=_bool(e.get("ignorer")),
        )
    if n_items and len(scores) < math.ceil(n_items / 2):
        raise ValueError(f"tri incomplet : {len(scores)} articles notés sur {n_items}")
    return scores


# --------------------------------------------------------------------------- choix des sujets
def _score(sc: Score, couverture: int, cfg: dict) -> float:
    base = 0.6 * max(sc.importance, sc.interet) + 0.4 * min(sc.importance, sc.interet)
    if sc.ton == "positif":
        base += float(cfg.get("bonus_positif", 1.0))
    elif sc.ton == "difficile":
        base -= float(cfg.get("malus_difficile", 1.0))
    return base + float(cfg.get("bonus_couverture", 0.5)) * min(2, couverture - 1)


def arrange(chosen: list[Choice]) -> list[Choice]:
    """Ordre du podcast : une bonne nouvelle d'abord, les sujets difficiles au milieu, jamais en dernier."""
    calm = [c for c in chosen if c.ton != "difficile"]
    hard = [c for c in chosen if c.ton == "difficile"]
    if not calm:
        return hard
    positives = [c for c in calm if c.ton == "positif"]
    first = positives[0] if positives else calm[0]
    rest = [c for c in calm if c is not first]
    if not rest:
        return [first] + hard
    return [first] + rest[:-1] + hard + [rest[-1]]


def select(items: list[Item], scores: dict[int, Score], cfg: dict) -> tuple[list[Choice], list[Rejected], bool]:
    """Retourne (sujets retenus dans l'ordre du podcast, écartés avec la raison, journée_calme)."""
    nb = int(cfg.get("nb_sujets", 5))
    seuil_imp = float(cfg.get("seuil_importance", 7))
    seuil_int = float(cfg.get("seuil_interet", 7))
    minimum = min(nb, int(cfg.get("minimum", 3)))
    max_diff = int(cfg.get("max_difficiles", 1))
    exception = float(cfg.get("importance_exception", 9))
    max_cat = int(cfg.get("max_par_categorie", 2))

    rejected: list[Rejected] = []

    # 1) articles notés et non ignorés, regroupés par histoire
    stories: dict[str, list[Item]] = {}
    for it in items:
        sc = scores.get(it.id)
        if sc is None:
            rejected.append(Rejected(it, 0, 0, "non noté"))
        elif sc.ignorer:
            rejected.append(Rejected(it, sc.importance, sc.interet, "ignoré (hors sujet ou sans intérêt)"))
        else:
            stories.setdefault(sc.groupe or f"_{it.id}", []).append(it)

    # 2) un seul représentant par histoire (le mieux noté ; être couvert par plusieurs sources est un bonus)
    reps: list[Choice] = []
    for group in stories.values():
        coverage = len({g.source for g in group})
        best: Choice | None = None
        for it in group:
            sc = scores[it.id]
            cand = Choice(it, sc.importance, sc.interet, sc.ton, _score(sc, coverage, cfg), coverage)
            if best is None or (cand.score, -cand.item.id) > (best.score, -best.item.id):
                best = cand
        assert best is not None
        reps.append(best)
        for it in group:
            if it is not best.item:
                sc = scores[it.id]
                rejected.append(Rejected(it, sc.importance, sc.interet, "doublon (même histoire)"))
    reps.sort(key=lambda c: (-c.score, c.item.id))

    def passes(c: Choice) -> bool:
        return c.importance >= seuil_imp or c.interet >= seuil_int

    qualified = [c for c in reps if passes(c)]
    below = [c for c in reps if not passes(c)]

    chosen: list[Choice] = []
    per_cat: dict[str, int] = {}
    n_hard = 0

    def refusal(c: Choice, check_cat: bool = True) -> str:
        if c.ton == "difficile":
            allowed = max_diff + (1 if c.importance >= exception else 0)
            if n_hard >= allowed:
                return "trop de sujets difficiles"
        if check_cat and per_cat.get(c.item.categorie, 0) >= max_cat:
            return "limite par catégorie"
        return ""

    def take(c: Choice) -> None:
        nonlocal n_hard
        chosen.append(c)
        per_cat[c.item.categorie] = per_cat.get(c.item.categorie, 0) + 1
        if c.ton == "difficile":
            n_hard += 1

    # 3) sujets qui passent les seuils, du mieux noté au moins bien noté
    deferred: list[tuple[Choice, str]] = []
    for c in qualified:
        if len(chosen) >= nb:
            rejected.append(Rejected(c.item, c.importance, c.interet, f"au-delà de {nb} sujets"))
            continue
        why = refusal(c)
        if why:
            deferred.append((c, why))
        else:
            take(c)

    # 4) pas assez de sujets : on relâche d'abord la diversité (catégories), puis seulement les seuils
    for c, why in list(deferred):
        if len(chosen) >= minimum:
            break
        if why == "limite par catégorie" and not refusal(c, check_cat=False):
            take(c)
            deferred.remove((c, why))
    calme = len(chosen) < minimum  # même ainsi, trop peu de sujets passent les seuils : journée calme
    if calme:
        for c in below:
            if len(chosen) >= minimum:
                break
            if not refusal(c, check_cat=False):
                take(c)
        below = [c for c in below if c not in chosen]

    rejected += [Rejected(c.item, c.importance, c.interet, why) for c, why in deferred]
    rejected += [
        Rejected(c.item, c.importance, c.interet, "sous les seuils d'importance et d'intérêt") for c in below
    ]
    rejected.sort(key=lambda r: -max(r.importance, r.interet))
    return arrange(chosen), rejected, calme


# --------------------------------------------------------------------------- orchestration
def fallback(items: list[Item], cfg: dict, note: str) -> TriResult:
    """Tri impossible (IA en panne, réponse illisible) : le rédacteur choisira lui-même."""
    n = int(cfg.get("nb_sujets", 5)) * 2
    choices = [Choice(i, 0.0, 0.0, "neutre", 0.0) for i in items[:n]]
    return TriResult(False, choices, [], "", len(items), False, True, note[:300])


def run(items: list[Item], profile: dict, cfg: dict, keys: dict[str, str], now: datetime) -> TriResult:
    """Ne lève jamais d'exception : au pire, repli sans tri."""
    tri_cfg = cfg.get("tri") or {}
    chain = (cfg.get("llm") or {}).get("tri") or []
    try:
        system, prompt = build_prompt(items, profile, now)
        text, model = llm.generate(
            prompt, system=system, chain=chain, keys=keys, temperature=0.2, json_mode=True
        )
        scores = parse_scores(text, len(items))
        choices, rejected, calme = select(items, scores, tri_cfg)
        if not choices:
            raise ValueError("aucun sujet retenu")
        return TriResult(True, choices, rejected, model, len(items), calme)
    except Exception as exc:
        log.warning("Tri impossible, repli sans tri : %s", exc)
        return fallback(items, tri_cfg, str(exc))


def report(res: TriResult, now: datetime, feeds: list | None = None, profile: dict | None = None) -> str:
    """Compte rendu lisible : pourquoi ces actus, pourquoi pas celles-là (pour régler moi.yaml)."""
    lines = [f"Choix du {now.date().isoformat()} — {res.nb_actus} actus examinées"]
    if profile:
        lines.append(f"Profil de lecture : {profile.get('origine', '?')}")
    if not res.ok:
        lines += [
            f"Tri indisponible : le rédacteur a choisi lui-même parmi {len(res.choices)} actus.",
            f"Raison : {res.note}",
        ]
        lines += [f"- {c.item.title} ({c.item.source})" for c in res.choices]
    else:
        lines.append(f"Tri par {res.modele}" + (" — journée calme, seuils assouplis" if res.calme else ""))
        lines += ["", "RETENUES"]
        for n, c in enumerate(res.choices, 1):
            lines.append(
                f"{n}. [{c.ton}] importance {c.importance:g} · intérêt {c.interet:g} · score {c.score:.1f}"
                f" — {c.item.title} ({c.item.source}, {c.item.categorie})"
            )
        lines += ["", "ÉCARTÉES (les mieux notées)"]
        for r in res.rejected[:12]:
            lines.append(
                f"- importance {r.importance:g} · intérêt {r.interet:g} — {r.item.title} ({r.item.source})"
                f" → {r.reason}"
            )
    if feeds:
        lines += ["", "SOURCES"] + [f.line() for f in feeds]
    return "\n".join(lines) + "\n"
