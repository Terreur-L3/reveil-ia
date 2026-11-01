"""Les commandes /moi : lire et modifier ton document de contexte depuis Telegram (le document lui-même : contexte.py).

  /moi                                le sommaire (les sections et ce qu'elles contiennent)
  /moi hobbies                        une section (par son nom ou son numéro) · /moi tout : tout le document
  /moi ajouter hobbies : la pêche     ajoute un élément (plusieurs d'un coup : une ligne chacun, ou séparés par « ; »)
  /moi j'adore le cinéma              sans section : Groq choisit où le ranger (ton texte n'est jamais réécrit)
  /moi retirer hobbies 2              retire · /moi modifier hobbies 2 : … · /moi deplacer hobbies 2 vers gouts
  /moi section Voyages                nouvelle section · /moi renommer … · /moi supprimer section …
  /moi partager hobbies · /moi prive hobbies      🌐 peut aider à écrire le podcast · 🔒 seul Groq la lit
  /moi fichier · /moi apercu · /moi annuler · /moi organiser notes
  un fichier .md / .txt / .docx envoyé avec la légende /moi remplace (ou complète) le document
  « Ajoute à mon contexte que… » (message ou vocal) : comme /moi suivi de la phrase

Ici, aucune IA ne réécrit ce que tu dis. Groq (jamais Gemini) sert seulement à choisir la section quand tu n'en nommes pas.
Tout est annulable : /moi annuler.
"""
from __future__ import annotations

import functools
import logging
import re
import traceback
from dataclasses import dataclass
from pathlib import Path

from . import contexte, llm, telegram, triage
from .contexte import ContexteError, Doc, Section
from .extract import is_sensitive

log = logging.getLogger(__name__)

fold = contexte.fold
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_NEW_SECTIONS = 3       # sections que l'IA peut créer en une seule fois
SHOWN = 8                  # éléments détaillés dans une réponse (les autres sont comptés)
NEWS_SECTIONS = ("sujets", "important")   # leurs éléments sont envoyés tels quels à Gemini (choix des actus)

EMPTY_INTRO = (
    "📖 Ton document de contexte est vide pour l'instant.\n\n"
    "C'est ici que tu me dis qui tu es : tes hobbies, tes projets, tes goûts, les gens qui comptent, comment tu veux que je te "
    "parle, ce que tu veux entendre dans les actus… Plus tu en mets, plus mes questions et ton podcast te ressemblent. "
    "Les sections sont prêtes ; tu peux en ajouter.\n\n"
    "Pour commencer, écris /moi suivi de ce que tu veux me dire — une info par ligne, ou séparées par « ; » :\n"
    "/moi j'ai … ans et j'étudie le cinéma ; j'adore la pêche à la cuillère ; tutoie-moi\n"
    "Je range chaque info dans la bonne section (Groq, en privé) et je te dis où elle est allée.\n"
    "Tu préfères écrire tranquillement ? /moi fichier : je t'envoie le document ; modifie-le, puis renvoie-le avec la légende /moi."
)


def _guarded(default, message: str):
    """Une erreur imprévue dans une commande /moi est dite à l'utilisateur au lieu de faire tomber tout le passage du journal
    (Telegram ferait relire le même message à chaque passage). Le journal ne contient ni ton texte ni le message de l'erreur :
    seulement son type et l'endroit."""
    def wrap(fn):
        @functools.wraps(fn)
        def inner(ctx, *args, **kwargs):
            try:
                return fn(ctx, *args, **kwargs)
            except Exception as exc:  # noqa: BLE001 - voulu : rien ne doit bloquer le journal
                frame = traceback.extract_tb(exc.__traceback__)[-1]
                log.warning("/moi : erreur imprévue (%s, %s ligne %s)", exc.__class__.__name__, Path(frame.filename).name, frame.lineno)
                ctx.say(message)
                return default
        return inner
    return wrap


# --------------------------------------------------------------------------- les verbes de /moi
_VERBS: dict[str, tuple[str, ...]] = {
    "aide": ("aide", "help", "commandes"),
    "tout": ("tout", "complet", "integral"),
    "fichier": ("fichier", "document", "doc", "telecharger", "exporter", "export"),
    "apercu": ("apercu", "preview", "controle", "controler"),
    "annuler": ("annuler", "annule", "undo", "retablir", "retablis"),
    "ajouter": ("ajouter", "ajoute", "rajouter", "rajoute", "ajout", "add"),
    "retirer": ("retirer", "retire", "enlever", "enleve", "supprimer", "supprime", "effacer", "efface", "oublier", "oublie"),
    "modifier": ("modifier", "modifie", "corriger", "corrige", "remplacer", "remplace"),
    "deplacer": ("deplacer", "deplace"),
    "section": ("section", "rubrique", "nouvelle"),
    "renommer": ("renommer", "renomme"),
    "partager": ("partager", "partage", "partagee", "partagees"),
    "prive": ("prive", "privee", "prives", "privees", "cacher", "cache"),
    "organiser": ("organiser", "organise", "trier", "trie", "classer", "classe"),
}
_VERB_OF = {word: verb for verb, words in _VERBS.items() for word in words}
_SHORT_ONLY = {"tout", "fichier", "apercu"}      # un verbe suivi d'une longue phrase, c'est une phrase à ranger, pas l'ordre


# --------------------------------------------------------------------------- petits outils
def _load(ctx) -> Doc | None:
    """Le document, ou None après avoir dit pourquoi on n'y touche pas (jamais remplacé par un document vide)."""
    try:
        return contexte.load(ctx.racine)
    except (ContexteError, OSError) as exc:
        ctx.say(f"⚠️ Je n'arrive pas à lire ton document de contexte ({exc}). Je n'y touche pas : corrige-le sur GitHub "
                "(memoire/contexte.md), ou renvoie-le-moi en fichier avec la légende /moi.")
        return None


def _save(ctx, doc: Doc) -> bool:
    try:
        contexte.save(ctx.racine, doc)
        return True
    except (ContexteError, OSError) as exc:
        ctx.say(f"⚠️ Je n'ai pas pu enregistrer le document : {exc}. Rien n'a changé.")
        return False


def _label(section: Section) -> str:
    return f"{contexte.marker(section)} {section.title}"


def _short(text: str, limit: int = 90) -> str:
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n <= 1 else many}"


def _sections_list(doc: Doc) -> str:
    return "\n".join(f"{i}. {_label(s)}" for i, s in enumerate(doc.sections, 1))


def _unknown_section(ctx, doc: Doc, query: str) -> None:
    ctx.say(f"🤔 Je ne trouve pas de section « {_short(query.strip(), 40)} ». Tes sections :\n{_sections_list(doc)}\n"
            "Dis-moi son nom ou son numéro (/moi pour le sommaire), ou crée-la : /moi section Voyages.")


def _ambiguous(ctx, many: list[Section], doc: Doc) -> None:
    lines = [f"{doc.sections.index(s) + 1}. {_label(s)}" for s in many]
    ctx.say("🤔 Plusieurs sections peuvent convenir :\n" + "\n".join(lines) + "\nDis-moi le numéro (ex. /moi " + lines[0].split(".")[0] + ").")


def _resolve(doc: Doc, query: str, *, create: bool = False) -> tuple[Section | None, list[Section]]:
    """La section désignée par `query`. `create` : une section prévue d'avance qui a disparu du document est recréée."""
    section, many = contexte.find_section(doc, query)
    if section is None and not many and create:
        key = contexte.builtin_key_for(query)
        if key:
            return contexte.ensure_builtin(doc, key), []
    return section, many


def _split_target(doc: Doc, text: str) -> tuple[Section | None, str, list[Section], str]:
    """« hobbies : la pêche » → (la section, « la pêche », [], « hobbies »). Sans « : » ou si le début n'est pas une section,
    (None, tout le texte, [], début) : c'est une phrase à ranger."""
    head, sep, tail = text.partition(":")
    if not sep or not tail.strip() or "\n" in head.strip() or not head.strip():
        return None, text, [], ""
    section, many = _resolve(doc, head, create=True)
    if section is None and not many:
        return None, text, [], head.strip()
    return section, tail.strip(), many, head.strip()


def _split_numbers(text: str) -> tuple[str, list[int], str]:
    """« mes goûts 2 et 4 » → (« mes goûts », [2, 4], «  ») ; « hobbies 2 : nouveau » → (« hobbies », [2], « nouveau »)."""
    first = re.search(r"(?<![\w'’])(?:n°\s*|no\s*|#)?(\d{1,3})(?![\w'’])", text)
    if not first:
        return text.strip(), [], ""
    head = text[:first.start()].strip(" ,:;-–")
    numbers: list[int] = []
    pos = first.start()
    sep = re.compile(r"(?:\s*(?:,|et|&)\s*|\s+)?(?:n°\s*|no\s*|#)?(\d{1,3})(?![\w'’])", re.IGNORECASE)
    while True:
        found = sep.match(text, pos)
        if not found:
            break
        numbers.append(int(found.group(1)))
        pos = found.end()
    tail = re.sub(r"^[\s:=\-–—>→]+", "", text[pos:]).strip()
    return head, numbers, tail


def _learned_notes(ctx) -> dict[str, str]:
    """Ce que le bot a appris de tes messages et qui s'ajoute à ton document (pour le sommaire)."""
    notes: dict[str, str] = {}
    try:
        chat = len(ctx.mem.consignes())
        if chat:
            notes["parler"] = f"{chat} dite{'s' if chat > 1 else ''} dans le chat"
        learned = ctx.mem.interests()
        if learned["aime"]:
            notes["sujets"] = f"{len(learned['aime'])} apprise{'s' if len(learned['aime']) > 1 else ''} de tes messages"
        if learned["evite"]:
            notes["eviter"] = f"{len(learned['evite'])} apprise{'s' if len(learned['evite']) > 1 else ''} de tes messages"
    except Exception as exc:    # le sommaire ne doit jamais échouer pour ça
        log.warning("Notes du sommaire ignorées : %s", exc)
    return notes


# --------------------------------------------------------------------------- voir
def _overview(ctx) -> None:
    doc = _load(ctx)
    if doc is None:
        return
    text = contexte.overview(doc, _learned_notes(ctx))
    ctx.say((EMPTY_INTRO + "\n\n" + text) if contexte.count_items(doc) == 0 else text)


def _view(ctx, doc: Doc, section: Section) -> None:
    extras: list[str] = []
    try:
        if section.key == "parler" and ctx.mem.consignes():
            extras += ["", "📌 Dites dans le chat (comme /consignes ; /oublier N pour en retirer) :"]
            extras += [f"• {c['texte']}" for c in ctx.mem.consignes()]
        elif section.key in ("sujets", "eviter"):
            learned = ctx.mem.interests()
            names = [e["sujet"] for e in learned["aime"]] if section.key == "sujets" else list(learned["evite"])
            if names:
                extras += ["", "🧠 Appris de tes messages (je m'en sers aussi) : " + ", ".join(names[:12])]
    except Exception as exc:
        log.warning("Section : compléments ignorés : %s", exc)
    ctx.say(contexte.section_view(doc, section, extras))


def _show(ctx, query: str) -> None:
    doc = _load(ctx)
    if doc is None:
        return
    section, many = _resolve(doc, query)
    if many:
        _ambiguous(ctx, many, doc)
    elif section is None:
        key = contexte.builtin_key_for(query)
        if key:     # une section prévue d'avance qui a disparu : on le dit et on explique comment la retrouver
            ctx.say(f"« {contexte.BY_KEY[key].title} » n'est plus dans ton document. Pour la retrouver, ajoute quelque chose : "
                    f"/moi ajouter {contexte.BY_KEY[key].names[0]} : …")
        else:
            _unknown_section(ctx, doc, query)
    else:
        _view(ctx, doc, section)


def _full(ctx) -> None:
    doc = _load(ctx)
    if doc is not None:
        ctx.say(contexte.full_view(doc))


def _file(ctx) -> None:
    """Envoie le document en fichier .md : il se modifie n'importe où, puis se renvoie avec la légende /moi."""
    try:
        doc = contexte.load(ctx.racine)
    except (ContexteError, OSError):
        doc = None
    dest = Path(ctx.tmp) / "contexte.md"
    dest.parent.mkdir(parents=True, exist_ok=True)
    source = contexte.path(ctx.racine)
    try:
        if source.is_file():
            dest.write_bytes(source.read_bytes())
        else:
            doc = contexte.template()
            dest.write_text(contexte.render(doc), encoding="utf-8")
    except OSError as exc:
        ctx.say(f"⚠️ Je n'arrive pas à préparer le fichier ({exc}).")
        return
    body = "ton document tel qu'il est" if source.is_file() else "un document vide, avec toutes les sections prêtes"
    caption = (f"📎 Voici {body}.\nModifie-le où tu veux (un titre « ## » par section, une ligne « - » par élément ; "
               "🔒 = privé, 🌐 = peut aider à écrire le podcast), puis renvoie-le-moi sous le même nom avec la légende /moi : "
               "il remplace le document (/moi annuler pour revenir en arrière).")
    fallback = "⚠️ Telegram n'a pas pris le fichier. Voici le document en texte :\n\n" + (
        contexte.full_view(doc) if doc is not None else "(illisible)")
    ctx.say(caption, document=dest, fallback_text=fallback)


def _preview(ctx) -> None:
    """Ce que chaque IA reçoit de ton document : pour vérifier, sans rien deviner."""
    doc = _load(ctx)
    if doc is None:
        return
    shared_budget, private_budget = contexte.budgets(ctx.cfg)
    in_podcast = (ctx.cfg.get("contexte") or {}).get("dans_podcast", True)
    shared = contexte.shared_context(doc, shared_budget) if in_podcast else ""
    day = ctx.now.date()
    rules = ctx.mem.consignes_for("podcast", allow_private=False, day=day) if (ctx.cfg.get("perso") or {}).get(
        "consignes_dans_podcast", True) else []
    profile = triage.load_profile(ctx.racine)
    lines = ["🔎 Ce que chaque IA reçoit de ton document", ""]
    lines.append("🎙️ Gemini, pour écrire le podcast :")
    if not in_podcast:
        lines.append("• rien : « dans_podcast: false » (section contexte de config.yaml)")
    elif shared:
        lines.append(shared)
    else:
        free = [s for s in doc.sections if s.key not in contexte.SEMANTIC and s.items]
        lines.append("• aucune section 🌐 libre à envoyer"
                     + (f" (pour que le podcast connaisse « {free[0].title} » : /moi partager {contexte.short_name(doc, free[0])})"
                        if free else ""))
    if rules:
        lines.append("• tes consignes de style : " + " · ".join(_short(r, 60) for r in rules[:6]))
    lines += ["", "📰 Gemini, pour choisir les actus (avec les titres de presse) :"]
    topics = ", ".join(f"{t['sujet']} ({t.get('poids', 2)})" for t in (profile.get("priorites") or [])[:12])
    lines.append("• sujets : " + (topics or "aucun"))
    lines.append("• toujours important : " + (", ".join(_short(t, 50) for t in profile.get("toujours_important") or []) or "rien"))
    lines.append("• à ignorer : " + (", ".join(_short(t, 50) for t in profile.get("a_ignorer") or []) or "rien"))
    private = contexte.private_context(doc, private_budget)
    lines += ["", f"🔒 Groq (jamais Gemini), pour te poser des questions et ranger tes messages : jusqu'à {private_budget} "
                  f"caractères de ton document ({len(private)} aujourd'hui)."]
    if private:
        lines.append(_short(private.replace("\n", " "), 300))
    lines += ["", "Rien d'autre de ton document ne part vers Gemini. Pour retirer une section du podcast : /moi prive <section>."]
    ctx.say("\n".join(lines))


# --------------------------------------------------------------------------- ajouter
def _rule_lines(doc: Doc, section: Section, numbers: list[int]) -> list[str]:
    """Comment l'IA va comprendre les consignes qu'on vient d'écrire dans « Comment l'IA doit me parler »."""
    try:
        index = doc.sections.index(section)
    except ValueError:
        return []
    lines: list[str] = []
    for rule in contexte.style_rules(doc):
        if rule.get("section_index") != index or rule.get("item") not in numbers:
            continue
        where = contexte.SCOPE_LABEL.get(rule["portee"], "tout")
        if rule.get("jours"):
            where += f" · {contexte.days_label(rule['jours'])}"
        lines.append(f"📌 « {_short(rule['texte'], 70)} » → concerne : {where}" + (" · 🔒 jamais envoyée à Gemini" if rule.get("prive") else ""))
    return lines[:3]


def _notes_after_add(doc: Doc, section: Section, added: list[str], numbers: list[int]) -> list[str]:
    notes: list[str] = []
    if section.key == "parler":
        notes += _rule_lines(doc, section, numbers)
    elif section.shared and section.key in ("sujets", "eviter", "important"):
        sensitive = [t for t in added if is_sensitive(t)]
        notes.append("📰 Gemini voit ces libellés pour choisir tes actus (jamais le reste de ton document).")
        if sensitive:
            notes.append(f"⚠️ « {_short(sensitive[0], 40)} » touche un sujet sensible : tu l'as demandé, il servira donc à choisir tes actus.")
    elif section.shared:
        sensitive = [t for t in added if is_sensitive(t)]
        if sensitive:
            notes.append(f"🔒 {_plural(len(sensitive), 'élément sensible', 'éléments sensibles')} : gardé{'s' if len(sensitive) > 1 else ''} "
                         "privé{} même dans une section 🌐 (jamais envoyé à Gemini).".format("s" if len(sensitive) > 1 else ""))
    elif section.key != "notes" and not any(s.shared and s.key not in contexte.SEMANTIC for s in doc.sections):
        # tant que rien n'est partagé avec le podcast, on rappelle (une ligne) comment faire
        notes.append(f"🔒 Privé (Groq seul). Pour le podcast aussi : /moi partager {contexte.short_name(doc, section)}")
    return notes


def _add_explicit(ctx, doc: Doc, section: Section, payload: str) -> None:
    items = contexte.split_items(payload)
    if not items:
        ctx.say("Écris ce qu'il faut ajouter après les deux-points. Exemple : /moi ajouter hobbies : la pêche à la cuillère")
        return
    added: list[str] = []
    numbers: list[int] = []
    already: list[str] = []
    for item in items:
        if contexte.add_item(section, item):
            added.append(item)
            numbers.append(len(section.items))
        else:
            already.append(item)
    if not added:
        ctx.say(f"📖 Déjà dans « {section.title} » : rien à ajouter.")
        return
    if not _save(ctx, doc):
        return
    name = contexte.short_name(doc, section)
    lines = [f"✅ Ajouté à {_label(section)} :"]
    lines += [f"{n}. {_short(t, 160)}" for n, t in list(zip(numbers, added))[:SHOWN]]
    if len(added) > SHOWN:
        lines.append(f"… et {len(added) - SHOWN} autres")
    if already:
        lines.append(f"({_plural(len(already), 'était déjà là', 'étaient déjà là')}.)")
    lines += _notes_after_add(doc, section, added, numbers)
    lines.append(f"Retirer : /moi retirer {name} {numbers[0]} · tout annuler : /moi annuler")
    ctx.say("\n".join(lines))


@dataclass
class Placed:
    section: Section
    number: int
    text: str


def _target_for(doc: Doc, sections: list[Section], index: int | None, new_title: str, state: dict) -> Section | None:
    """La section choisie par l'IA : une section du document, ou une nouvelle (trois au plus par fois)."""
    if index is not None:
        return sections[index - 1]
    if new_title:
        existing, _many = contexte.find_section(doc, new_title)
        if existing is not None:
            return existing
        if state["created"] < MAX_NEW_SECTIONS:
            made = contexte.add_section(doc, new_title)
            if made is not None:
                state["created"] += 1
                return made
    return None


def _classify(ctx, doc: Doc, items: list[str]) -> tuple[list[tuple[int | None, str]], list[str]]:
    try:
        return contexte.classify(items, doc, cfg=ctx.cfg, keys=ctx.keys, prenom=ctx.prenom, pacer=ctx.pacer), []
    except llm.LLMError as exc:
        log.warning("Rangement par l'IA impossible : %s", str(exc).splitlines()[0])
        return [(None, "")] * len(items), [
            "⚠️ L'IA qui range n'a pas répondu : tout est dans « Notes libres ». Plus tard : /moi organiser notes."]


def _place(ctx, doc: Doc, items: list[str]) -> tuple[list[Placed], list[str], list[str]]:
    """Range des phrases dans le document (modifié en place). Retourne (rangées, déjà là, remarques)."""
    verdicts, notes = _classify(ctx, doc, items)
    sections = list(doc.sections)          # les numéros donnés par l'IA sont ceux du document tel qu'il était à l'appel
    state = {"created": 0}
    placed: list[Placed] = []
    already: list[str] = []
    kept_private = 0
    for item, (index, new_title) in zip(items, verdicts):
        target = _target_for(doc, sections, index, new_title, state)
        if target is not None and target.shared and target.key in NEWS_SECTIONS and is_sensitive(item):
            target, kept_private = None, kept_private + 1      # jamais envoyé à Gemini sans que tu l'aies demandé
        if target is None:
            target = contexte.ensure_builtin(doc, "notes")
        if contexte.add_item(target, item):
            placed.append(Placed(target, len(target.items), item))
        else:
            already.append(item)
    if kept_private:
        notes.append(f"🔒 {_plural(kept_private, 'phrase touche', 'phrases touchent')} un sujet sensible : rangée{'s' if kept_private > 1 else ''} "
                     "en privé (Notes libres), pas dans une liste envoyée à Gemini. Pour l'y mettre quand même : /moi sujets : …")
    created = [p.section for p in placed if p.section not in sections]
    if created:
        names = ", ".join(dict.fromkeys(f"« {s.title} »" for s in created))
        notes.append(f"🆕 Nouvelle section : {names} (privée). /moi renommer · /moi supprimer section · /moi partager")
    return placed, already, notes


def _add_free(ctx, doc: Doc, text: str, head: str = "") -> None:
    items = contexte.split_items(text)
    if not items:
        ctx.say("Écris ce qu'il faut ajouter. Exemples : /moi ajouter hobbies : la pêche à la cuillère — ou /moi j'adore le cinéma d'animation")
        return
    placed, already, notes = _place(ctx, doc, items)
    if not placed:
        ctx.say("📖 Tout était déjà dans ton document : rien à ajouter.")
        return
    if not _save(ctx, doc):
        return
    lines = ["📖 C'est rangé dans ton document :"]
    for p in placed[:SHOWN]:
        lines.append(f"• {_label(p.section)}, n°{p.number} : {_short(p.text)}")
    if len(placed) > SHOWN:
        lines.append(f"… et {len(placed) - SHOWN} autres (/moi pour le sommaire)")
    if already:
        lines.append(f"({_plural(len(already), 'était déjà là', 'étaient déjà là')}.)")
    if head and len(head.split()) <= 3:
        lines.append(f"(« {head} » n'est pas une section : pour la créer, /moi section {head}.)")
    lines += notes
    first = placed[0]
    lines.append(f"Mal rangé ? /moi deplacer {contexte.short_name(doc, first.section)} {first.number} vers <section> · "
                 "retirer : /moi retirer … · tout annuler : /moi annuler")
    ctx.say("\n".join(lines))


def _add(ctx, rest: str) -> None:
    if not rest.strip():
        ctx.say("Dis-moi quoi ajouter. Exemples : /moi ajouter hobbies : la pêche à la cuillère — "
                "ou, sans section, /moi ajouter j'adore le cinéma d'animation (je choisis où le ranger).")
        return
    doc = _load(ctx)
    if doc is None:
        return
    section, payload, many, head = _split_target(doc, rest)
    if many:
        _ambiguous(ctx, many, doc)
    elif section is not None:
        _add_explicit(ctx, doc, section, payload)
    else:
        _add_free(ctx, doc, payload, head)


def add_text(ctx, text: str) -> None:
    """Ajoute des phrases (message ou vocal « ajoute à mon contexte… ») : une section nommée avant « : », sinon l'IA choisit."""
    _add(ctx, text)


# --------------------------------------------------------------------------- retirer, modifier, déplacer
def _find_for_numbers(ctx, doc: Doc, head: str) -> Section | None:
    section, many = _resolve(doc, head)
    if many:
        _ambiguous(ctx, many, doc)
    elif section is None:
        _unknown_section(ctx, doc, head)
    return section


def _remove_by_text(ctx, doc: Doc, rest: str) -> bool:
    """« retirer hobbies pêche » : la section est le plus long début qui en désigne une, le reste un bout du texte à retirer."""
    tokens = rest.split()
    for k in range(len(tokens) - 1, 0, -1):
        section, _many = contexte.find_section(doc, " ".join(tokens[:k]))
        if section is None:
            continue
        wanted = fold(" ".join(tokens[k:]))
        hits = [n for n, t in enumerate(section.items, 1) if wanted and wanted in fold(t)]
        if len(hits) == 1:
            gone = contexte.remove_item(section, hits[0])
            if gone is not None and _save(ctx, doc):
                ctx.say(f"🗑️ Retiré de {_label(section)} : « {_short(gone, 120)} »\n/moi annuler pour le remettre.")
            return True
        if not hits:
            ctx.say(f"Je ne trouve rien qui ressemble à « {' '.join(tokens[k:])} » dans « {section.title} ». /moi {contexte.short_name(doc, section)} pour voir les numéros.")
        else:
            ctx.say(f"Plusieurs éléments de « {section.title} » ressemblent à ça : dis-moi le numéro "
                    f"(/moi {contexte.short_name(doc, section)}), puis /moi retirer {contexte.short_name(doc, section)} 2.")
        return True
    return False


def _remove(ctx, rest: str) -> None:
    text = rest.strip()
    doc = _load(ctx)
    if doc is None:
        return
    whole = re.match(r"^(?:la |une |cette )?(?:section|rubrique)\s+(.+)$", text, re.IGNORECASE | re.DOTALL)
    if whole:
        section, many = _resolve(doc, whole.group(1))
        if many:
            _ambiguous(ctx, many, doc)
        elif section is None:
            _unknown_section(ctx, doc, whole.group(1))
        else:
            count = len(section.items)
            contexte.delete_section(doc, section)
            if _save(ctx, doc):
                ctx.say(f"🗑️ Section « {section.title} » supprimée ({_plural(count, 'élément', 'éléments')}). /moi annuler pour la remettre.")
        return
    head, numbers, _tail = _split_numbers(text)
    if not numbers:
        if text and _remove_by_text(ctx, doc, text):
            return
        ctx.say("Dis-moi quel élément retirer : /moi hobbies pour voir les numéros, puis par exemple /moi retirer hobbies 2 "
                "(ou 2 et 4). Pour supprimer toute une section : /moi supprimer section hobbies.")
        return
    if not head and len(numbers) >= 2:           # « retirer 3 2 » : la section n° 3, son élément n° 2
        head, numbers = str(numbers[0]), numbers[1:]
    if not head:
        ctx.say("Dis-moi aussi la section : par exemple /moi retirer hobbies 2.")
        return
    section = _find_for_numbers(ctx, doc, head)
    if section is None:
        return
    gone: list[str] = []
    missing: list[int] = []
    for number in sorted(set(numbers), reverse=True):      # du dernier au premier : les numéros restent valables
        text_gone = contexte.remove_item(section, number)
        (gone if text_gone is not None else missing).append(text_gone if text_gone is not None else number)
    if not gone:
        ctx.say(f"« {section.title} » n'a pas d'élément n° {', '.join(map(str, sorted(missing)))} "
                f"(elle en a {len(section.items)}). /moi {contexte.short_name(doc, section)} pour voir les numéros.")
        return
    if not _save(ctx, doc):
        return
    lines = [f"🗑️ Retiré de {_label(section)} :"] + [f"• {_short(t, 120)}" for t in reversed(gone)]
    if missing:
        lines.append(f"(pas d'élément n° {', '.join(map(str, sorted(missing)))})")
    lines.append("/moi annuler pour le remettre.")
    ctx.say("\n".join(lines))


def _modify(ctx, rest: str) -> None:
    doc = _load(ctx)
    if doc is None:
        return
    first = re.search(r"(?<![\w'’])(?:n°\s*|no\s*|#)?(\d{1,3})(?![\w'’])", rest)
    if not first:
        ctx.say("Dis-moi quel élément modifier et par quoi le remplacer : /moi modifier hobbies 2 : la pêche au lancer léger "
                "(les numéros : /moi hobbies).")
        return
    head = rest[:first.start()].strip(" ,:;-–")
    number = int(first.group(1))
    new_text = re.sub(r"^[\s:=\-–—>→]+", "", rest[first.end():]).strip()
    if not head:
        ctx.say("Dis-moi aussi la section : par exemple /moi modifier hobbies 2 : la pêche au lancer léger.")
        return
    section = _find_for_numbers(ctx, doc, head)
    if section is None:
        return
    if not new_text:
        ctx.say("Écris le nouveau texte après les deux-points : /moi modifier "
                f"{contexte.short_name(doc, section)} {number} : …")
        return
    old = contexte.edit_item(section, number, new_text)
    if old is None:
        ctx.say(f"« {section.title} » n'a pas d'élément n° {number} (elle en a {len(section.items)}). "
                f"/moi {contexte.short_name(doc, section)} pour voir les numéros.")
        return
    if not _save(ctx, doc):
        return
    notes = _notes_after_add(doc, section, [new_text], [number]) if section.key == "parler" else []
    ctx.say("\n".join([f"✏️ Modifié dans {_label(section)}, n°{number} :", f"avant : {_short(old, 120)}",
                       f"après : {_short(contexte.fit_item(new_text), 120)}", *notes, "/moi annuler pour revenir en arrière."]))


def _move(ctx, rest: str) -> None:
    doc = _load(ctx)
    if doc is None:
        return
    pieces = re.split(r"\s+(?:vers|dans|en|à|->|→|>)\s+", rest.strip(), maxsplit=1, flags=re.IGNORECASE)
    if len(pieces) != 2 or not pieces[1].strip():
        ctx.say("Dis-moi quoi déplacer et où : /moi deplacer hobbies 2 vers gouts (les numéros : /moi hobbies).")
        return
    head, numbers, _tail = _split_numbers(pieces[0])
    if not head and len(numbers) >= 2:
        head, numbers = str(numbers[0]), numbers[1:]
    if not numbers or not head:
        ctx.say("Dis-moi la section et le numéro de l'élément : /moi deplacer hobbies 2 vers gouts.")
        return
    source = _find_for_numbers(ctx, doc, head)
    if source is None:
        return
    target, many = _resolve(doc, pieces[1], create=True)
    if many:
        _ambiguous(ctx, many, doc)
        return
    if target is None:
        _unknown_section(ctx, doc, pieces[1])
        return
    if target is source:
        ctx.say(f"Ces éléments sont déjà dans « {source.title} ».")
        return
    moved: list[str] = []
    for number in sorted(set(numbers), reverse=True):
        text = contexte.remove_item(source, number)
        if text is not None:
            moved.append(text)
    if not moved:
        ctx.say(f"« {source.title} » n'a pas d'élément n° {', '.join(map(str, numbers))} (elle en a {len(source.items)}).")
        return
    for text in reversed(moved):
        contexte.add_item(target, text)
    if not _save(ctx, doc):
        return
    lines = [f"➡️ Déplacé de {_label(source)} vers {_label(target)} :"] + [f"• {_short(t, 120)}" for t in reversed(moved)]
    if target.shared and target.key in NEWS_SECTIONS and any(is_sensitive(t) for t in moved):
        lines.append("⚠️ Au moins un élément touche un sujet sensible : tu l'as demandé, il servira à choisir tes actus.")
    lines.append("/moi annuler pour revenir en arrière.")
    ctx.say("\n".join(lines))


# --------------------------------------------------------------------------- sections
def _new_section(ctx, rest: str) -> None:
    title = re.sub(r"^(?:section|rubrique)\s+", "", rest.strip(), flags=re.IGNORECASE)
    doc = _load(ctx)
    if doc is None:
        return
    clean = contexte.clean_title(contexte._split_marker(title)[1])
    if len(clean) < 2:
        ctx.say("Donne un nom à la section. Exemple : /moi section Voyages")
        return
    existing, _many = contexte.find_section(doc, clean)
    if existing is not None:
        ctx.say(f"« {existing.title} » existe déjà : /moi {contexte.short_name(doc, existing)}")
        return
    section = contexte.add_section(doc, title)
    if section is None:
        ctx.say(f"⚠️ Je ne peux pas créer cette section (le maximum est de {contexte.MAX_SECTIONS}).")
        return
    if _save(ctx, doc):
        name = contexte.short_name(doc, section)
        ctx.say(f"🆕 Section {_label(section)} créée (n°{doc.sections.index(section) + 1}). Elle est privée : seul Groq la lit.\n"
                f"Ajouter : /moi ajouter {name} : … · la partager avec le podcast : /moi partager {name}")


def _rename(ctx, rest: str) -> None:
    head, sep, new_title = rest.partition(":")
    if not sep:
        head, new_title = rest, ""
        words = rest.split(None, 1)
        pieces = re.split(r"\s+(?:en|par|->|→)\s+", rest.strip(), maxsplit=1, flags=re.IGNORECASE)
        if len(pieces) == 2:
            head, new_title = pieces
        elif words:
            head = ""
    doc = _load(ctx)
    if doc is None:
        return
    if not head.strip() or not new_title.strip():
        ctx.say("Dis-moi la section et son nouveau nom : /moi renommer voyages : Mes voyages")
        return
    section = _find_for_numbers(ctx, doc, head)
    if section is None:
        return
    new_clean = contexte.clean_title(contexte._split_marker(new_title)[1])
    if len(new_clean) < 2:
        ctx.say("Ce nouveau nom est trop court.")
        return
    if section.key in contexte.SEMANTIC and contexte.key_of(new_clean) != section.key:
        ctx.say(f"« {section.title} » sert à choisir tes actus ou à te parler : je ne peux pas la renommer, je ne la reconnaîtrais "
                "plus. Tu peux en revanche créer d'autres sections.")
        return
    old = section.title
    section.title = new_clean
    if _save(ctx, doc):
        ctx.say(f"✏️ « {old} » s'appelle maintenant {_label(section)}.")


def _set_sharing(ctx, rest: str, shared: bool) -> None:
    query = rest.strip()
    doc = _load(ctx)
    if doc is None:
        return
    if not query:
        ctx.say("Dis-moi quelle section : /moi " + ("partager" if shared else "prive") + " hobbies (les sections : /moi).")
        return
    section, many = _resolve(doc, query, create=True)
    if many:
        _ambiguous(ctx, many, doc)
        return
    if section is None:
        _unknown_section(ctx, doc, query)
        return
    if section.shared == shared:
        ctx.say(f"{_label(section)} est déjà {'partagée' if shared else 'privée'}.")
        return
    section.shared = shared
    if not _save(ctx, doc):
        return
    name = contexte.short_name(doc, section)
    sensitive = sum(1 for t in section.items if is_sensitive(t)) if shared else 0
    if shared:
        if section.key == "parler":
            what = "tes consignes de style peuvent de nouveau accompagner l'écriture du podcast (Gemini)."
        elif section.key in ("sujets", "eviter", "important"):
            what = "ses éléments serviront à choisir tes actus (Gemini voit ces libellés, avec les titres de presse)."
        else:
            what = "son contenu peut accompagner l'écriture du podcast (Gemini), pour trouver le bon angle ; il n'est jamais récité."
        text = f"🌐 {section.title} est maintenant partagée : {what}"
        if sensitive and section.key not in NEWS_SECTIONS:
            text += f"\n🔒 {_plural(sensitive, 'élément sensible reste', 'éléments sensibles restent')} privé{'s' if sensitive > 1 else ''}."
        text += f"\nPour la remettre en privé : /moi prive {name}"
    else:
        if section.key == "parler":
            what = "tes consignes de style ne partent plus vers Gemini : elles ne servent plus qu'aux questions et aux phrases perso (Groq)."
        elif section.key in ("sujets", "eviter", "important"):
            what = "elle ne sert plus à choisir tes actus (Gemini ne la voit plus)."
        else:
            what = "seul Groq la lit désormais (questions, rangement) ; Gemini ne la voit plus."
        text = f"🔒 {section.title} est maintenant privée : {what}\nPour la partager : /moi partager {name}"
    ctx.say(text)


def _organize(ctx, rest: str) -> None:
    doc = _load(ctx)
    if doc is None:
        return
    query = rest.strip() or "notes"
    source, many = _resolve(doc, query)
    if many:
        _ambiguous(ctx, many, doc)
        return
    if source is None:
        _unknown_section(ctx, doc, query)
        return
    items = source.items[:contexte.MAX_BULK]
    if not items:
        ctx.say(f"« {source.title} » est vide : rien à ranger.")
        return
    verdicts, notes = _classify(ctx, doc, items)
    if notes:
        ctx.say(notes[0].replace("tout est dans « Notes libres »", "rien n'a bougé"))
        return
    sections = list(doc.sections)
    state = {"created": 0}
    moved: list[tuple[str, Section]] = []
    kept_private = 0
    for item, (index, new_title) in zip(items, verdicts):
        target = _target_for(doc, sections, index, new_title, state)
        if target is None or target is source:
            continue
        if target.shared and target.key in NEWS_SECTIONS and is_sensitive(item):
            kept_private += 1
            continue
        contexte.add_item(target, item)
        moved.append((item, target))
    if not moved:
        ctx.say(f"📖 Je n'ai rien trouvé de mieux pour les éléments de « {source.title} » : ils restent là.")
        return
    gone = {item for item, _target in moved}
    source.entries = [e for e in source.entries if not (e.kind == "item" and e.text in gone)]
    if not _save(ctx, doc):
        return
    lines = [f"📖 J'ai rangé {_plural(len(moved), 'élément', 'éléments')} de {_label(source)} :"]
    by_section: dict[str, int] = {}
    for _item, target in moved:
        by_section[_label(target)] = by_section.get(_label(target), 0) + 1
    lines += [f"• {name} : {n}" for name, n in by_section.items()]
    left = len(source.items)
    if left:
        lines.append(f"({_plural(left, 'reste', 'restent')} dans « {source.title} »"
                     + (f", dont {_plural(kept_private, 'sensible', 'sensibles')}" if kept_private else "") + ".)")
    lines.append("/moi annuler pour tout remettre comme avant.")
    ctx.say("\n".join(lines))


def _undo(ctx) -> None:
    try:
        done = contexte.undo(ctx.racine)
    except OSError as exc:
        ctx.say(f"⚠️ Je n'ai pas pu annuler ({exc}).")
        return
    if not done:
        ctx.say("↩️ Rien à annuler : je n'ai pas gardé de version précédente.")
        return
    doc = _load(ctx)
    if doc is not None:
        ctx.say(f"↩️ C'est annulé : ton document est revenu à la version d'avant ({_plural(contexte.count_items(doc), 'élément', 'éléments')}, "
                f"{_plural(len(doc.sections), 'section', 'sections')}). Annuler encore te ramène à celle d'aujourd'hui.")


# --------------------------------------------------------------------------- phrase libre
def _free(ctx, text: str) -> None:
    """/moi suivi d'autre chose qu'un verbe : une section (« hobbies », « 2 »), « section : texte » à ajouter, ou une phrase à ranger."""
    doc = _load(ctx)
    if doc is None:
        return
    section, payload, many, head = _split_target(doc, text)
    if many:
        _ambiguous(ctx, many, doc)
        return
    if section is not None:
        _add_explicit(ctx, doc, section, payload)
        return
    if "\n" not in text and len(text) <= 60:
        whole, candidates = _resolve(doc, text)
        if whole is not None:
            _view(ctx, doc, whole)
            return
        if candidates:
            _ambiguous(ctx, candidates, doc)
            return
        if len(text.split()) == 1:          # un seul mot qui n'est pas une section : sans doute une faute de frappe, pas un élément
            ctx.say(f"🤔 Je ne trouve pas de section « {_short(text, 40)} ». Tes sections :\n{_sections_list(doc)}\n"
                    f"Pour ajouter « {_short(text, 40)} » à ton document : /moi ajouter {text} — ou écris une phrase complète.")
            return
    _add_free(ctx, doc, text, head)


# --------------------------------------------------------------------------- aide, point d'entrée
def _help(ctx) -> None:
    ctx.say(contexte.HELP)


_HANDLERS = {
    "aide": lambda ctx, rest: _help(ctx),
    "tout": lambda ctx, rest: _full(ctx),
    "fichier": lambda ctx, rest: _file(ctx),
    "apercu": lambda ctx, rest: _preview(ctx),
    "annuler": lambda ctx, rest: _undo(ctx),
    "ajouter": lambda ctx, rest: _add(ctx, rest),
    "retirer": lambda ctx, rest: _remove(ctx, rest),
    "modifier": lambda ctx, rest: _modify(ctx, rest),
    "deplacer": lambda ctx, rest: _move(ctx, rest),
    "section": lambda ctx, rest: _new_section(ctx, rest),
    "renommer": lambda ctx, rest: _rename(ctx, rest),
    "partager": lambda ctx, rest: _set_sharing(ctx, rest, True),
    "prive": lambda ctx, rest: _set_sharing(ctx, rest, False),
    "organiser": lambda ctx, rest: _organize(ctx, rest),
}


@_guarded(None, "⚠️ Une erreur imprévue m'a gêné avec ton document. Tape /moi pour voir ce qui a été pris en compte (/moi annuler pour revenir en arrière).")
def handle(ctx, args: str) -> None:
    """/moi et tout ce qui suit."""
    args = (args or "").strip()
    if not args:
        _overview(ctx)
        return
    parts = args.split(None, 1)
    verb = _VERB_OF.get(fold(parts[0]))
    rest = parts[1].strip() if len(parts) > 1 else ""
    if verb in _SHORT_ONLY and len(rest.split()) > 2:
        verb = None                         # « tout ce qui touche à la 3D m'intéresse » : une phrase à ranger
    if verb == "section" and not rest:
        ctx.say("Donne un nom à la section. Exemple : /moi section Voyages")
        return
    if verb is not None:
        _HANDLERS[verb](ctx, rest)
        return
    _free(ctx, args)


# --------------------------------------------------------------------------- « ajoute à mon contexte que… » (message ou vocal)
_DOC = r"(?:document|docu|doc|contexte|profil|portrait|fiche)\b(?:\s+(?:de contexte|perso|personnel|de moi)\b)?"
_FILLER = r"(?:(?:s'il (?:te|vous) pla[iî]t|stp|ok|alors|bon|dis|dis-moi|hey|et|bah|ben|eh)\W+)*"
_SPOKEN_ADD_RE = re.compile(
    r"^\W*" + _FILLER + r"(?:ajoute|rajoute|ajouter|note|inscris|[ée]cris|mets|mettre|garde|enregistre|retiens)"
    r"(?:\s+(?:[çc]a|cela|[çc]e qui suit|l'info|l'information))?"
    r"\s+(?:dans|[àa]|au|sur|en)\s+(?:mon|ma|le|la|ton|ta)\s+" + _DOC + r"\s*[:,;.!\-–—]*\s*(?:que\s+|qu['’]\s*)?(?P<rest>\S.*)$",
    re.IGNORECASE | re.DOTALL)
_SPOKEN_ADD_REVERSE_RE = re.compile(
    r"^\W*dans\s+(?:mon|ma|ton|ta)\s+" + _DOC + r"\s*[:,;.!\-–—]*\s*(?:ajoute|rajoute|note|inscris|mets)\s+"
    r"(?:que\s+|qu['’]\s*)?(?P<rest>\S.*)$", re.IGNORECASE | re.DOTALL)


def spoken_add_payload(text: str) -> str | None:
    """« Ajoute à mon contexte que j'adore la pêche » → « j'adore la pêche » (None si le message n'est pas cet ordre)."""
    for pattern in (_SPOKEN_ADD_RE, _SPOKEN_ADD_REVERSE_RE):
        found = pattern.match(text or "")
        if found:
            rest = found.group("rest").strip()
            if len(rest) >= 3:
                return rest
    return None


@_guarded(False, "⚠️ Je n'ai pas pu utiliser ton document pour ça (erreur imprévue). Ton message est gardé tel quel dans le journal.")
def spoken_add(ctx, text: str) -> bool:
    """Un message ou un vocal qui commence par « ajoute à mon contexte… » : le reste est rangé dans le document, comme /moi.
    Retourne True si le message n'était que cela (il n'entre alors pas dans le journal)."""
    payload = spoken_add_payload(text)
    if payload is None:
        return False
    add_text(ctx, payload)
    return True


_SPOKEN_VIEW_RE = re.compile(
    r"^\W*" + _FILLER + r"(?P<verb>montre|affiche|donne|envoie|envoi|fais voir|ouvre|redonne|rappelle)(?:[-\s]moi)?(?:\s+voir)?"
    r"\s+(?:mon|ma|ton|ta)\s+" + _DOC + r"\W*(?:s'il (?:te|vous) pla[iî]t|stp)?\W*$", re.IGNORECASE)


@_guarded(False, "⚠️ Je n'ai pas pu utiliser ton document pour ça (erreur imprévue). Ton message est gardé tel quel dans le journal.")
def spoken_view(ctx, text: str) -> bool:
    """« Montre-moi mon contexte » / « envoie-moi mon document de contexte » (message ou vocal) : comme /moi, ou /moi fichier.
    Seulement si le message n'est que cela : une phrase du journal qui contient ces mots n'est pas un ordre."""
    found = _SPOKEN_VIEW_RE.match(text or "")
    if not found or len((text or "").split()) > 10:
        return False
    if fold(found.group("verb")).startswith("envoi"):
        _file(ctx)
    else:
        _overview(ctx)
    return True


# --------------------------------------------------------------------------- un fichier reçu
_FILE_EXT = (".md", ".markdown", ".txt", ".docx")


def looks_like_context_file(m) -> bool:
    """Un fichier envoyé avec la légende /moi, ou nommé comme le fichier que je t'envoie (contexte.md)."""
    if getattr(m, "forwarded", False):
        return False                           # un fichier transféré ne vient pas de toi : il ne change rien
    if (m.text or "").strip().lower().startswith("/moi"):
        return True
    return bool(re.match(r"^contexte\b[^/\\]*\.(?:md|markdown|txt)$", (m.name or "").strip().lower()))


def _import_mode(m) -> str:
    """« replace » (le fichier devient le document) ou « merge » (il s'ajoute à ce qui existe)."""
    caption = fold(re.sub(r"^/moi\S*", "", (m.text or "").strip(), flags=re.IGNORECASE))
    if re.search(r"\b(?:remplace\w*|ecrase\w*)\b", caption):
        return "replace"
    if re.search(r"\b(?:ajout\w*|rajout\w*|fusion\w*|complet\w*|merge)\b", caption):
        return "merge"
    return "replace" if re.match(r"^contexte\b.*\.(?:md|markdown)$", (m.name or "").strip().lower()) else "merge"


def _sections_summary(doc: Doc, limit: int = 8) -> str:
    filled = [s for s in doc.sections if s.items]
    shown = ", ".join(f"{s.title} ({len(s.items)})" for s in filled[:limit])
    return shown + (f" … et {len(filled) - limit} autres" if len(filled) > limit else "")


@_guarded(None, "⚠️ Une erreur imprévue m'a gêné avec ton document. Tape /moi pour voir ce qui a été pris en compte (/moi annuler pour revenir en arrière).")
def import_file(ctx, m) -> None:
    """Un fichier .md, .txt ou .docx envoyé pour ton document de contexte."""
    name = m.name or "ton fichier"
    ext = Path(name).suffix.lower()
    if ext not in _FILE_EXT:
        ctx.say("📎 Pour ton document de contexte, je lis les fichiers .md, .txt et .docx (envoyés avec la légende /moi).")
        return
    if m.file_size and m.file_size > MAX_FILE_BYTES:
        ctx.say("📎 Ce fichier est trop gros pour un document de contexte (2 Mo au maximum).")
        return
    try:
        path = ctx.bot.download(m.file_id, Path(ctx.tmp) / f"import-{m.update_id}{ext}")
    except telegram.FileTooBig:
        ctx.say("📎 Ce fichier est trop gros : Telegram ne me laisse pas le télécharger.")
        return
    except telegram.TelegramError as exc:
        log.warning("Fichier de contexte non téléchargé : %s", exc)
        ctx.say("⚠️ Je n'ai pas pu télécharger ton fichier. Renvoie-le-moi dans un moment.")
        return
    try:
        text = contexte.docx_to_text(path) if ext == ".docx" else contexte.decode(path.read_bytes())
        incoming = contexte.parse(text)
    except (ContexteError, OSError) as exc:
        ctx.say(f"⚠️ {exc}. Je n'ai rien changé.")
        return
    if contexte.count_items(incoming) == 0:
        ctx.say("📎 Je n'ai rien trouvé à lire dans ce fichier (il faut un texte : des titres « ## » pour les sections et des lignes "
                "« - » pour les éléments). Je n'ai rien changé.")
        return
    mode = _import_mode(m)
    current = _load(ctx)
    if current is None:
        return
    before = (len(current.sections), contexte.count_items(current))
    if mode == "replace":
        final = incoming
        head = f"📖 Ton document est remplacé par « {name} »."
    else:
        final = current
        added = contexte.merge_into(final, incoming)
        head = f"📖 J'ai ajouté {_plural(added, 'élément nouveau', 'éléments nouveaux')} à ton document, depuis « {name} »."
        if added == 0:
            ctx.say("📖 Tout ce qui est dans ce fichier était déjà dans ton document : rien à ajouter.")
            return
    if not _save(ctx, final):
        return
    lines = [head, f"Maintenant : {_plural(len(final.sections), 'section', 'sections')} · {_plural(contexte.count_items(final), 'élément', 'éléments')}"
             + (f" (avant : {before[0]} · {before[1]})." if mode == "replace" else ".")]
    summary = _sections_summary(final)
    if summary:
        lines.append("Sections remplies : " + summary + ".")
    shared = [s.title for s in final.sections if s.shared]
    lines.append("🌐 partagées : " + (", ".join(shared) if shared else "aucune") + " — le reste est privé (🔒).")
    notes = contexte.doc_first(final, "notes")
    if notes is not None and len(notes.items) >= 5:
        lines.append(f"{_plural(len(notes.items), 'élément est', 'éléments sont')} dans « {notes.title} » : /moi organiser notes pour que "
                     "je les range par thème.")
    lines.append("Pas ce que tu voulais ? /moi annuler.")
    ctx.say("\n".join(lines))
