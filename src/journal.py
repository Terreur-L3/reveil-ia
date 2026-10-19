"""Journal vocal : lit ta conversation Telegram, transcrit, range dans la mémoire et pose des questions.

Usage :
    python -m src.journal               # une passe (le cron la lance toutes les 30 minutes)
    python -m src.journal --inspi       # pose une question tout de suite (comme le bouton)
    python -m src.journal --no-send     # essai : n'envoie rien dans Telegram

Ce que le bot comprend : vocaux et textes (journal), images (emploi du temps, documents), et des ordres
(/reveil, /cours, /consignes, /podcast…). Les ordres de réveil sont lus par des règles fixes, pas par une IA.

Codes de sortie : 0 = ok, 1 = Telegram ou Groq pas configurés, 2 = Telegram refuse (token ou webhook).
Les textes de tes messages ne sont jamais écrits dans les logs.
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

import yaml

from . import audio, edt, extract, github, inspi, llm, perso, perso_style, reglages, stt, telegram, triage, tts, vision
from .fr import MOIS, duree_fr, jour_court_fr
from .main import load_env
from .memoire import Memoire, mentions_perso, norm
from .pacer import Pacer

ROOT = Path(__file__).resolve().parent.parent
log = logging.getLogger("journal")

WELCOME = (
    "👋 Salut ! Je suis ton assistant du matin.\n\n"
    "🎙️ Vocaux (30 secondes ou plus d'une heure) et textes : raconte-moi ta vie, j'en retiens ce qui compte "
    "(anniversaires, projets, goûts, échéances…).\n"
    "🖼️ Images : envoie une photo ou une capture de ton emploi du temps (idéalement « en tant que fichier » pour "
    "qu'elle reste nette) : je lis tes cours et je te les rappelle chaque matin.\n"
    "⏰ /reveil 6h30 : l'heure à laquelle ton podcast se lance. Tu peux aussi le dire : « mets mon réveil à 6h30 ».\n"
    "📌 Dis-moi comment tu veux que je te parle (« plus décontracté », « phrases courtes », « cite mes cours sans la salle »…), "
    "éventuellement selon les jours (« le week-end… ») : je le retiens. /consignes pour voir la liste.\n"
    "💡 « 💡 Inspi » : je te pose une question quand tu manques d'inspiration.\n"
    "🎧 /podcast : je prépare le podcast tout de suite. 🧠 /memoire · ⚙️ /reglages\n\n"
    "Seul le texte est gardé, jamais l'audio ni les images. Je lis tes messages en général dans la demi-heure "
    "(en quelques minutes avec la « sonnette »)."
)
HELP_UNKNOWN = "Je ne connais pas cette commande. Tape /aide, ou utilise les boutons en bas."
HELP_FILE = ("📎 Je sais lire les vocaux, les textes et les images (photo ou capture d'écran). "
             "Pour un autre type de fichier (PDF…), envoie plutôt une capture d'écran.")
HELP_REVEIL = ("Je n'ai pas compris l'heure. Exemples : /reveil 6h30 · /reveil 7h semaine · /reveil 9h week-end · "
               "/reveil demain 5h45 · /reveil off · /reveil on")
SONNETTE_NOTE = ("ℹ️ Pour que le podcast se lance tout seul à l'heure exacte, il faut installer la « sonnette » "
                 "(README, étape 8). En attendant, il t'attend ici chaque matin.")
SHORT_ORDER_WORDS = 14   # un message plus court que ça, qui ne contient qu'un ordre de réveil, n'entre pas dans le journal


@dataclass
class Out:
    text: str
    audio: Path | None = None
    markup: dict | None = None
    on_sent: Callable[[dict], None] | None = None


@dataclass
class Ctx:
    bot: telegram.Bot
    mem: Memoire
    racine: Path
    cfg: dict
    keys: dict
    now: datetime
    tmp: Path
    state: dict
    pacer: Pacer | None = None
    deadline: float | None = None
    send: bool = True
    prenom: str = ""
    out: list[Out] = field(default_factory=list)
    wants_inspi: bool = False
    touched: set = field(default_factory=set)   # entrées déjà traitées pendant ce passage
    settings: dict = field(default_factory=lambda: reglages.normalize(None))   # réveil, voix… (memoire/reglages.json)
    settings_changed: bool = False
    origine: str = ""                           # « sonnette » quand c'est le petit programme Cloudflare qui m'a lancé
    stats: dict = field(default_factory=lambda: {
        "texte": 0, "audio": 0, "images": 0, "commandes": 0, "ignores": 0, "entrees": 0})

    @property
    def j(self) -> dict:
        return self.cfg.get("journal") or {}

    @property
    def tz(self):
        return self.now.tzinfo

    def say(self, text: str, **kw) -> None:
        self.out.append(Out(text, **kw))

    def out_of_time(self) -> bool:
        return self.deadline is not None and time.time() > self.deadline


# --------------------------------------------------------------------------- entrées
def _entry_id(m: telegram.Incoming) -> str:
    return f"m{m.update_id}-{m.message_id}"


def _stamp(ctx: Ctx, ts: int | float | None) -> datetime:
    return datetime.fromtimestamp(ts, ctx.tz) if ts else ctx.now


def _new_entry(ctx: Ctx, eid: str, ts: int, kind: str, text: str, duration: int | None, answers: str | None,
               forwarded: bool = False) -> dict:
    when = _stamp(ctx, ts)
    entry = {
        "id": eid, "date": when.date().isoformat(), "heure": when.strftime("%H:%M"), "type": kind,
        "duree_s": int(duration) if duration else None, "texte": text.strip(), "resume": "",
        "repond_a": answers, "extrait": False, "essais_extraction": 0,
    }
    if forwarded:
        entry["transfere"] = True   # ce texte ne vient pas de toi : il ne peut ni donner d'ordre ni changer un réglage
    return entry


def _link_question(ctx: Ctx, reply_to: int | None, entry_id: str) -> str | None:
    """À quelle question ce message répond-il ? (réponse explicite, sinon la dernière question en attente)"""
    question = ctx.mem.question_by_message(reply_to)
    if question and question.get("repondue_par"):
        question = None
    pending = ctx.state.get("question_en_attente")
    if question is None and pending:
        try:
            fresh = ctx.now - datetime.fromisoformat(pending["date"]) < timedelta(hours=36)
        except (KeyError, ValueError):
            fresh = False
        if fresh:
            question = ctx.mem.question(pending.get("id"))
    if question is None:
        return None
    ctx.mem.update_question(question["id"], repondue_par=entry_id)
    if pending and pending.get("id") == question["id"]:
        ctx.state["question_en_attente"] = None
    return question["id"]


def handle_text_entry(ctx: Ctx, m: telegram.Incoming) -> None:
    eid = _entry_id(m)
    if ctx.mem.has_entry(eid):
        return
    if not m.forwarded and apply_orders(ctx, m.text, _stamp(ctx, m.ts)):
        return  # ce message n'était qu'un ordre (réveil, durée du podcast)
    qid = _link_question(ctx, m.reply_to, eid)
    entry = _new_entry(ctx, eid, m.ts, "texte", m.text, None, qid, m.forwarded)
    ctx.mem.add_entry(entry, (ctx.mem.question(qid) or {}).get("texte"))
    ctx.stats["entrees"] += 1
    finish_entry(ctx, entry)


def enqueue_media(ctx: Ctx, m: telegram.Incoming) -> None:
    """Un vocal ou une image : mis en file, traité juste après (et repris au passage suivant si ça échoue)."""
    eid = _entry_id(m)
    if ctx.mem.has_entry(eid) or any(i["id"] == eid for i in ctx.state["file"]):
        return
    ctx.state["file"].append({
        "id": eid, "type": "image" if m.kind == "image" else "audio", "file_id": m.file_id, "taille": m.file_size,
        "duree": m.duration, "nom": m.name, "mime": m.mime, "recu": m.ts, "legende": m.text,
        "repond_a": _link_question(ctx, m.reply_to, eid) if m.kind == "audio" else None,
        "transfere": bool(m.forwarded), "textes": [], "essais": 0, "erreur": "",
    })


enqueue_audio = enqueue_media   # ancien nom


# --------------------------------------------------------------------------- ordres de réveil
def sonnette_recent(ctx: Ctx) -> bool:
    try:
        return ctx.now - datetime.fromisoformat(ctx.settings.get("sonnette_vue") or "") < timedelta(days=3)
    except ValueError:
        return False


def wake_reply(ctx: Ctx, orders: list, stamp: datetime) -> str:
    """Applique les ordres, retourne le message de confirmation (toujours : on voit ce qui a été compris)."""
    # période, exception ou jours flous : on le dit, on ne devine pas (et on applique quand même ce qui est clair)
    unclear = any(o.action in ("ask", "unclear") for o in orders)
    explain = reglages.ASK_TEXT if any(o.action == "ask" for o in orders) else reglages.UNCLEAR_TEXT
    changed = [o for o in orders if o.action not in ("show", "ask", "unclear")]
    if not changed:
        return explain if unclear else reglages.describe(ctx.settings, ctx.now)
    lines = [reglages.apply_request(ctx.settings, o, ctx.now, stamp) for o in changed]
    ctx.settings_changed = True
    lines += ["", reglages.describe(ctx.settings, ctx.now)]
    if unclear:
        lines += ["", explain]
    if not sonnette_recent(ctx):
        lines += ["", SONNETTE_NOTE]
    return "\n".join(lines)


def apply_wake_orders(ctx: Ctx, text: str, stamp: datetime) -> bool:
    """Cherche des ordres de réveil dans un message. Retourne True si le message n'était QUE cela."""
    orders = reglages.find_wake_requests(text, stamp.date())
    if not orders:
        return False
    ctx.say(wake_reply(ctx, orders, stamp))
    return len(text.split()) <= SHORT_ORDER_WORDS


ONLY_ORDER_LEFTOVER_WORDS = 6   # « merci », « salut, … » autour d'un ordre ne font pas de lui une entrée de journal


def apply_spoken_durations(ctx: Ctx, text: str) -> bool:
    """« Je voudrais un podcast de 3 minutes en semaine et de 5 minutes le week-end » : lu à règles fixes, appliqué tout de suite,
    et on répond toujours ce qui a été compris. Retourne True si le message n'était QUE cet ordre (il n'entre pas dans le journal)."""
    orders, leftover = reglages.scan_spoken_durations(text)
    if not orders:
        return False
    lines = [reglages.set_podcast(ctx.settings, "minutes", o.value, o.days, o.rest) for o in orders]
    ctx.settings_changed = True
    ctx.say("🔧 Réglé :\n" + "\n".join(f"• {line}" for line in lines) + "\n"
            + reglages.podcast_summary(ctx.settings, int(ctx.cfg.get("mots_cibles", 300))) + "\nÇa s'applique au prochain podcast.")
    return leftover <= ONLY_ORDER_LEFTOVER_WORDS and len(text.split()) <= 4 * SHORT_ORDER_WORDS


def apply_orders(ctx: Ctx, text: str, stamp: datetime) -> bool:
    """Les ordres du message (heure du réveil, durée du podcast par jour). Retourne True si le message n'était QUE cela :
    il n'entre alors pas dans le journal. Un message plus long est gardé, et ses ordres sont déjà pris en compte."""
    woke = apply_wake_orders(ctx, text, stamp)             # True : il y avait un ordre de réveil, et rien d'autre
    only_durations = apply_spoken_durations(ctx, text)     # True : il y avait un ordre de durée, et rien d'autre
    return woke or only_durations


# --------------------------------------------------------------------------- extraction et accusé
def _known_topics(ctx: Ctx) -> list[str]:
    data = ctx.mem.interests()
    return [e["sujet"] for e in data["aime"] + data["candidats"]]


def try_extract(ctx: Ctx, entry: dict) -> dict | None:
    """Range les infos d'une entrée. None = pas possible maintenant, on réessaiera au prochain passage."""
    if len(entry["texte"].split()) < int(ctx.j.get("min_mots_extraction", 8)):
        ctx.mem.update_entry(entry["id"], extrait=True)
        return {"court": True}
    partial = (ctx.state.setdefault("extractions", {})).get(entry["id"])

    def progress(done: list[dict]) -> None:
        ctx.state["extractions"][entry["id"]] = done
        ctx.mem.save_state(ctx.state)

    question = (ctx.mem.question(entry.get("repond_a")) or {}).get("texte", "")
    ctx.touched.add(entry["id"])
    own_words = entry.get("type") in ("texte", "vocal") and not entry.get("transfere")
    try:
        ex = extract.extract(
            entry["texte"], entry_date=entry["date"], cfg=ctx.cfg, keys=ctx.keys, pacer=ctx.pacer,
            prenom=ctx.prenom, known_topics=_known_topics(ctx), question_text=question,
            resume_from=partial, on_progress=progress, deadline=ctx.deadline,
            known_rules=[c["texte"] for c in ctx.mem.consignes()], allow_instructions=own_words,
        )
    except extract.ExtractPause:
        log.info("Extraction en pause (temps écoulé) : reprise au prochain passage")
        return None
    except (llm.LLMError, extract.ExtractError) as exc:
        log.warning("Extraction impossible pour l'instant : %s", str(exc).splitlines()[0])
        ctx.mem.update_entry(entry["id"], essais_extraction=int(entry.get("essais_extraction", 0)) + 1)
        return None
    if own_words:   # une durée par jour lue à règles fixes dans cette même entrée a déjà été appliquée : elle l'emporte sur l'IA
        ex.reglages = extract.drop_duree_conflicts(ex.reglages, reglages.parse_spoken_durations(entry["texte"]))
    applied = ctx.mem.apply_extraction(entry, ex, ctx.now.date(), settings=ctx.settings)
    if applied.get("reglages"):
        ctx.settings_changed = True      # écrits avec le reste en fin de passage (voir run)
    ctx.state["extractions"].pop(entry["id"], None)
    return applied


def _details(applied: dict) -> list[str]:
    lines: list[str] = []
    n = applied.get("faits", 0)
    if n:
        lines.append(f"• {n} info{'s' if n > 1 else ''} retenue{'s' if n > 1 else ''}")
    for b, status in applied.get("anniversaires", []):
        lines.append(f"• 🎂 anniversaire de {b['nom']} le {b['jour']} {MOIS[b['mois'] - 1]}" + (" (corrigé)" if status == "modifie" else ""))
    for r in applied.get("rappels", []):
        lines.append(f"• 🔔 {r['texte']} — {jour_court_fr(date.fromisoformat(r['date']))}")
    if applied.get("promus"):
        lines.append("• ⭐ centres d'intérêt retenus pour tes actus : " + ", ".join(applied["promus"]))
    for c in applied.get("consignes", []):
        where = [_days_label(c["jours"])] if c.get("jours") else []
        if c.get("portee") == "perso" or (c.get("portee") in ("podcast", "tout") and mentions_perso(c["texte"])):
            where.append("pour tes infos perso")
        lines.append(f"• 📌 consigne retenue : « {c['texte']} »" + (f" ({', '.join(where)})" if where else ""))
    if applied.get("consignes"):
        lines.append("  (pour la retirer : /consignes puis /oublier N)")
    for r in applied.get("reglages", []):
        lines.append(r if r.startswith("⚠️") else f"• 🔧 {r}")
    return lines[:10]


def ack_text(entry: dict, applied: dict | None, head: str | None = None) -> str:
    head = head or "✅ Noté"
    if head == "✅ Noté" and entry.get("type") == "vocal" and entry.get("duree_s"):
        head += f" ({duree_fr(entry['duree_s'])} de vocal)"
    if applied is None:
        return head + ". Je rangerai les infos au prochain passage."
    lines = _details(applied)
    return head + (".\n" + "\n".join(lines) if lines else ".")


def finish_entry(ctx: Ctx, entry: dict, head: str | None = None) -> None:
    ctx.say(ack_text(entry, try_extract(ctx, entry), head))


def retry_pending_extractions(ctx: Ctx) -> None:
    for entry in ctx.mem.entries_to_extract(int(ctx.j.get("min_mots_extraction", 8))):
        if entry["id"] in ctx.touched:
            continue
        if ctx.out_of_time():
            break
        applied = try_extract(ctx, entry)
        lines = _details(applied) if applied and not applied.get("court") else []
        if lines:
            ctx.say(f"🧠 Infos retenues de ton message de {entry['date']} à {entry['heure']} :\n" + "\n".join(lines))


# --------------------------------------------------------------------------- vocaux
def _give_up(ctx: Ctx, item: dict, reason: str) -> str:
    item["essais"] = int(item.get("essais", 0)) + 1
    item["erreur"] = reason[:200]
    what = "image" if item.get("type") == "image" else "vocal"
    log.warning("%s %s : %s (essai %d)", what.capitalize(), item["id"], reason, item["essais"])
    if item["essais"] >= 4:
        when = datetime.fromtimestamp(item["recu"], ctx.tz).strftime("%d/%m à %H:%M") if item.get("recu") else ""
        ctx.say(f"⚠️ Je n'arrive pas à traiter ton {what} du {when} ({reason[:120]}). Je l'abandonne : tu peux le renvoyer.")
        return "abandon"
    return "attente"


# --------------------------------------------------------------------------- images
_THIS_WEEK_ONLY_RE = re.compile(r"cette semaine\s+(?:seulement|uniquement)|exceptionnel|seulement cette semaine|juste cette semaine",
                                re.IGNORECASE)


def timetable_ack(report: dict, edt_data: dict, today: date, source: str) -> str:
    days = ", ".join(report["jours"]) if report["jours"] else "aucun jour"
    head = f"📅 J'ai lu {source} : {report['cours']} cours ({days})."
    scope = ("Je les répète chaque semaine, jusqu'à ce que tu m'envoies autre chose."
             if report["recurrent"] else "C'est noté pour cette semaine seulement.")
    return "\n".join([head, scope, "", edt.week_text(edt_data, today)])


def process_image(ctx: Ctx, item: dict) -> str:
    """Lit une image (emploi du temps, document…) avec Groq ; rien d'autre que le texte lu n'est gardé."""
    if not ctx.keys.get("groq"):
        return "attente"
    if ctx.out_of_time():
        return "attente_temps"
    workdir = ctx.tmp / item["id"]
    ext = Path(item.get("nom") or "").suffix.lower() or ".jpg"
    try:
        source = ctx.bot.download(item["file_id"], workdir / f"image{ext}")
    except telegram.FileTooBig:
        ctx.say("⚠️ Cette image dépasse 20 Mo : Telegram ne me laisse pas la télécharger. Réduis-la et renvoie-la.")
        return "abandon"
    except telegram.TelegramError as exc:
        return _give_up(ctx, item, f"téléchargement ({exc})")
    try:
        mime, data = vision.prepare_image(source)
    except vision.ImageError as exc:
        ctx.say(f"⚠️ Je ne peux pas lire cette image : {exc}.")
        return "abandon"
    try:
        reading = vision.read_images([(mime, data)], caption=item.get("legende") or "", now=ctx.now, cfg=ctx.cfg,
                                     keys=ctx.keys, prenom=ctx.prenom, pacer=ctx.pacer)
    except llm.LLMError as exc:
        return _give_up(ctx, item, "lecture de l'image : " + str(exc).splitlines()[0][:80])

    today = _stamp(ctx, item.get("recu", 0)).date()
    caption = item.get("legende") or ""
    if reading.type == "emploi_du_temps":
        if not reading.cours:
            ctx.say("📅 J'ai bien vu un emploi du temps, mais je n'arrive pas à le lire"
                    + (f" ({reading.description})" if reading.description else "")
                    + ". Renvoie-le « en tant que fichier » (Telegram écrase les photos) ou recadre-le sur quelques jours.")
            return "fini"
        recurring = not _THIS_WEEK_ONLY_RE.search(caption)
        week = edt.resolve_week(reading.semaine_du, today)
        if not recurring and week is None:
            week = edt.school_week(today)
        data_edt = edt.load(ctx.mem.p_edt)
        report = edt.merge(data_edt, reading.cours, reading.jours_vus, week=week, recurring=recurring, today=today)
        if not report["cours"]:
            ctx.say("📅 J'ai lu l'image mais aucun cours n'est exploitable (jours ou horaires illisibles). "
                    "Renvoie-la en fichier, plus nette.")
            return "fini"
        edt.save(ctx.mem.p_edt, data_edt)
        text = f"Emploi du temps lu sur une image : {report['cours']} cours ({', '.join(report['jours'])})."
        entry = _new_entry(ctx, item["id"], item.get("recu", 0), "image", text, None, None, bool(item.get("transfere")))
        entry["extrait"] = True
        if not ctx.mem.has_entry(entry["id"]):
            ctx.mem.add_entry(entry)
            ctx.stats["entrees"] += 1
        ctx.say(timetable_ack(report, data_edt, today, "ton emploi du temps"))
        return "fini"

    body = "\n".join(x for x in (reading.description, reading.texte) if x).strip()
    if not body:
        ctx.say("🖼️ Je n'ai rien pu lire d'utile sur cette image.")
        return "fini"
    entry = _new_entry(ctx, item["id"], item.get("recu", 0), "image", body, None, None, bool(item.get("transfere")))
    if not ctx.mem.has_entry(entry["id"]):
        ctx.mem.add_entry(entry)
        ctx.stats["entrees"] += 1
        finish_entry(ctx, entry, head="🖼️ Image lue" + (f" : {reading.description}" if reading.description else ""))
    return "fini"


def process_item(ctx: Ctx, item: dict) -> str:
    """'fini', 'abandon' (à retirer de la file) ou 'attente' / 'attente_quota' / 'attente_temps' (à reprendre)."""
    if item.get("type") == "image":
        return process_image(ctx, item)
    j = ctx.j
    key = ctx.keys.get("groq", "")
    if not key:
        return "attente"
    workdir = ctx.tmp / item["id"]
    ext = Path(item.get("nom") or "").suffix.lower() or ".oga"
    try:
        source = ctx.bot.download(item["file_id"], workdir / f"audio{ext}")
    except telegram.FileTooBig:
        ctx.say("⚠️ Ce fichier dépasse 20 Mo : Telegram ne me laisse pas le télécharger. Coupe-le en deux et renvoie-le.")
        return "abandon"
    except telegram.TelegramError as exc:
        return _give_up(ctx, item, f"téléchargement ({exc})")
    try:
        chunks = audio.prepare(source, workdir, int(j.get("morceau_s", 600)), item.get("duree") or 0)
    except audio.AudioError as exc:
        return _give_up(ctx, item, str(exc))

    quota = float(j.get("audio_s_par_heure", 6500))
    history = ctx.state["stt_historique"]
    for idx, chunk in enumerate(chunks):
        if idx < len(item["textes"]):
            continue  # déjà transcrit lors d'un passage précédent
        need = stt.billed(chunk.duration)
        if stt.usage_in_window(history, time.time()) + need > quota:
            log.info("Quota d'audio de l'heure atteint : suite au prochain passage")
            return "attente_quota"
        if ctx.out_of_time():
            return "attente_temps"
        try:
            text = stt.transcribe(chunk.path, key, model=j.get("stt_modele", "whisper-large-v3-turbo"),
                                  language=j.get("langue", "fr"))
        except stt.RateLimited as exc:
            log.info("Groq limite le débit : %s", exc)
            return "attente_quota"
        except stt.STTError as exc:
            return _give_up(ctx, item, f"transcription ({exc})")
        history.append([time.time(), need])
        ctx.state["stt_historique"] = history = stt.prune(history, time.time())
        item["textes"].append(text)
        ctx.mem.save_state(ctx.state)  # progression gardée si ça s'arrête en route

    transcript = " ".join(t.strip() for t in item["textes"] if t and t.strip()).strip()
    if item.get("legende"):
        transcript = (item["legende"].strip() + "\n\n" + transcript).strip()
    if len(transcript.split()) < 2:
        ctx.say("🎙️ Je n'ai rien entendu dans ce vocal (silence ?).")
        return "fini"
    if not item.get("transfere") and apply_orders(ctx, transcript, _stamp(ctx, item.get("recu", 0))):
        return "fini"  # ce vocal n'était qu'un ordre (réveil, durée du podcast)
    duration = item.get("duree") or int(sum(c.duration for c in chunks))
    entry = _new_entry(ctx, item["id"], item.get("recu", 0), "vocal", transcript, duration, item.get("repond_a"),
                       bool(item.get("transfere")))
    if not ctx.mem.has_entry(entry["id"]):
        ctx.mem.add_entry(entry, (ctx.mem.question(entry["repond_a"]) or {}).get("texte"))
        ctx.stats["entrees"] += 1
        finish_entry(ctx, entry)
    return "fini"


def process_queue(ctx: Ctx) -> None:
    for item in list(ctx.state["file"]):
        if ctx.out_of_time():
            break
        result = process_item(ctx, item)
        if result in ("fini", "abandon"):
            ctx.state["file"].remove(item)
            if result == "abandon":
                ctx.state.get("extractions", {}).pop(item["id"], None)
        if result == "attente_quota":
            break  # inutile d'insister avec les autres vocaux
    ctx.mem.save_state(ctx.state)


# --------------------------------------------------------------------------- questions
def _last_question_age(ctx: Ctx) -> timedelta | None:
    questions = ctx.mem.questions()
    if not questions:
        return None
    last = questions[-1]
    try:
        asked = datetime.fromisoformat(f"{last['date']}T{last['heure']}").replace(tzinfo=ctx.tz)
    except (KeyError, ValueError):
        return None
    return ctx.now - asked


def maybe_question(ctx: Ctx) -> None:
    reason = "bouton" if ctx.wants_inspi else ""
    today = ctx.now.date().isoformat()
    hour = ctx.j.get("heure_question")
    if not reason and hour is not None and int(hour) <= ctx.now.hour <= 22:
        age = _last_question_age(ctx)
        if ctx.state.get("derniere_question_du_soir") != today and (age is None or age > timedelta(hours=4)):
            reason = "soir"
    if not reason:
        return

    pending = ctx.state.get("question_en_attente") or {}
    q = inspi.make_question(ctx.mem, ctx.cfg, ctx.keys, ctx.now, pacer=ctx.pacer, prenom=ctx.prenom,
                           pending=pending.get("texte", ""),
                           consignes=ctx.mem.consignes_for("questions", allow_private=True, day=ctx.now.date()))
    qid = ctx.mem.next_question_id(ctx.now)
    record = {
        "id": qid, "date": today, "heure": ctx.now.strftime("%H:%M"), "texte": q["texte"], "categorie": q["categorie"],
        "origine": q["origine"], "banque_id": q.get("banque_id"), "message_id": None, "repondue_par": None,
    }
    voice: Path | None = None
    if ctx.j.get("question_vocale", True):
        try:
            voice = ctx.tmp / f"{qid}.mp3"
            # La question vient de ta mémoire : elle reste sur Edge (private=True), elle n'est jamais envoyée à Gemini.
            tts.synthesize(q["texte"], reglages.apply_to_cfg(ctx.cfg, ctx.settings, ctx.now.date()).get("voix", {}), voice,
                           private=True)
        except Exception as exc:  # pas de voix ? la question reste lisible
            log.warning("Voix de la question impossible : %s", str(exc).splitlines()[0])
            voice = None

    def sent(result: dict) -> None:
        record["message_id"] = result.get("message_id")
        ctx.mem.add_question(record)
        if pending.get("id"):
            ctx.mem.update_question(pending["id"], ignoree=True)
        ctx.state["question_en_attente"] = {"id": qid, "date": ctx.now.isoformat(), "texte": q["texte"]}
        if reason == "soir":
            ctx.state["derniere_question_du_soir"] = today

    ctx.out.append(Out("💡 " + q["texte"], audio=voice, on_sent=sent))


# --------------------------------------------------------------------------- commandes
def memoire_text(ctx: Ctx) -> str:
    s = ctx.mem.stats()
    profile = triage.load_profile(ctx.racine)
    lines = ["🧠 Ce que je sais de toi", ""]
    lines.append(f"• {s['entrees']} messages ({duree_fr(s['secondes'])} de vocal, {s['mots']} mots)")
    cats = ", ".join(f"{k} {v}" for k, v in s["categories"].most_common(4))
    lines.append(f"• {s['faits']} infos retenues" + (f" ({cats})" if cats else ""))
    lines.append(f"• {s['anniversaires']} anniversaires et {s['rappels']} rappels appris")
    lines.append(f"• {s['repondues']} réponses à mes {s['questions']} questions")
    if s["consignes"]:
        lines.append(f"• {s['consignes']} consigne{'s' if s['consignes'] > 1 else ''} sur ta façon d'être servi (/consignes)")
    courses = edt.count(edt.load(ctx.mem.p_edt))
    if courses:
        lines.append(f"• {courses} cours dans ton emploi du temps (/cours)")
    if s["aime"]:
        lines.append("• Tes centres d'intérêt : " + ", ".join(e["sujet"] for e in s["aime"][:8]))
    lines.append("")
    if profile["source"] == "memoire":
        lines.append("📰 Pour choisir tes actus, je me base sur TA mémoire.")
    else:
        missing = max(0, triage.MIN_APPRIS - len(s["aime"]))
        lines.append(
            "📰 Pour tes actus, j'utilise encore la liste PROVISOIRE choisie par Claude. "
            f"Il me manque {missing} centre{'s' if missing > 1 else ''} d'intérêt appris de tes messages pour passer sur ta mémoire."
        )
    return "\n".join(lines)


def settings_text(ctx: Ctx) -> str:
    courses = edt.count(edt.load(ctx.mem.p_edt))
    consignes = len(ctx.mem.consignes())
    lines = [
        "⚙️ Tes réglages", "",
        reglages.describe(ctx.settings, ctx.now),
        reglages.podcast_summary(ctx.settings, int(ctx.cfg.get("mots_cibles", 300))),
        f"📌 {consignes} consigne{'s' if consignes > 1 else ''} (/consignes)",
        f"📅 emploi du temps : {courses} cours (/cours)" if courses else "📅 emploi du temps : pas encore (envoie une image, ou /cours)",
        "",
        "Pour changer : /reveil 6h30 · /duree 3 (ou /duree 3 semaine 5 week-end) · /voix essai (écouter les voix) · "
        "/vitesse lent, normal ou rapide · /consigne … · /cours · /perso (écouter comment je dis tes cours et rappels)",
    ]
    if not sonnette_recent(ctx):
        lines += ["", SONNETTE_NOTE]
    return "\n".join(lines)


def cmd_reveil(ctx: Ctx, args: str, stamp: datetime) -> None:
    orders = reglages.parse_wake_args(args, stamp.date())
    ctx.say(wake_reply(ctx, orders, stamp) if orders else HELP_REVEIL)


def _set_podcast(ctx: Ctx, key: str, value, days: tuple[int, ...] | None = None) -> None:
    line = reglages.set_podcast(ctx.settings, key, value, days)
    ctx.settings_changed = True
    ctx.say(f"🔧 Réglé : {line}.\n" + reglages.podcast_summary(ctx.settings, int(ctx.cfg.get("mots_cibles", 300)))
            + "\nÇa s'applique au prochain podcast.")


def _set_scoped(ctx: Ctx, key: str, parsed: reglages.ScopedOrders) -> None:
    """Applique ce que /duree, /voix ou /vitesse ont compris : une valeur pour tous les jours, ou une par type de jour."""
    if len(parsed.orders) == 1 and not parsed.orders[0].rest:
        _set_podcast(ctx, key, parsed.orders[0].value, parsed.orders[0].days)
        return
    lines = [reglages.set_podcast(ctx.settings, key, o.value, o.days, o.rest) for o in parsed.orders]
    ctx.settings_changed = True
    ctx.say("🔧 Réglé :\n" + "\n".join(f"• {line}" for line in lines) + "\n"
            + reglages.podcast_summary(ctx.settings, int(ctx.cfg.get("mots_cibles", 300))) + "\nÇa s'applique au prochain podcast.")


def _scope_problem(parsed: reglages.ScopedOrders) -> str | None:
    """Le message à envoyer quand les jours n'ont pas été compris (None = tout va bien)."""
    if parsed.problem == "periode":
        return reglages.SCOPE_PERIOD_TEXT
    if parsed.problem == "flou":
        return reglages.SCOPE_UNCLEAR_TEXT
    return None


def cmd_duree(ctx: Ctx, args: str) -> None:
    parsed = reglages.parse_scoped(args, reglages.minutes_spans) if args else None
    problem = _scope_problem(parsed) if parsed else None
    if problem:
        ctx.say(problem)
        return
    if not parsed or not parsed.orders:
        ctx.say(reglages.podcast_summary(ctx.settings, int(ctx.cfg.get("mots_cibles", 300)))
                + "\nPour changer : /duree 3 (de 1 à 8 minutes ; 1,5 est accepté).\n"
                  "Selon les jours : /duree 3 semaine 5 week-end · /duree 4 lundi · /duree 5 week-end, 3 sinon.")
        return
    _set_scoped(ctx, "minutes", parsed)


VOICE_HELP = (
    "🎙️ Voix réalistes (Gemini) : /voix sulafat (chaleureuse) · /voix achird (amicale) · /voix kore · /voix charon · "
    "/voix orus · /voix algieba · /voix puck · /voix zephyr (il y en a 30)\n"
    "👂 /voix essai → je t'envoie des échantillons à écouter (ou /voix essai puck zephyr pour choisir lesquels)\n"
    "Voix classiques (Edge) : /voix homme · /voix femme · /voix remy · /voix vivienne"
)


def _voix_essai(ctx: Ctx, args: str) -> None:
    if not ctx.send:
        ctx.say("(essai) J'enverrais des échantillons de voix.")
        return
    last = ctx.state.get("dernier_essai_voix") or ""
    try:
        recent = bool(last) and ctx.now - datetime.fromisoformat(last) < timedelta(minutes=10)
    except ValueError:
        recent = False
    if recent:
        ctx.say("🎙️ Les échantillons viennent d'être demandés : patience, ils arrivent (1 à 3 minutes).")
        return
    names = []
    for word in re.split(r"[\s,;]+", norm(args)):
        name = reglages.GEMINI_BY_LOWER.get(word)
        if name and name not in names:
            names.append(name)
    try:
        github.dispatch("voix.yml", {"voix": " ".join(names[:8]), "origine": "telegram"})
    except github.GitHubError as exc:
        log.warning("Lancement de l'essai des voix impossible : %s", exc)
        if exc.missing:
            ctx.say("⚠️ Je ne peux pas le lancer d'ici. Dans GitHub : onglet Actions → « Essai de voix » → Run workflow.")
        else:
            ctx.say(f"⚠️ GitHub n'a pas lancé l'essai : {exc}")
        return
    ctx.state["dernier_essai_voix"] = ctx.now.isoformat(timespec="seconds")
    ctx.say("🎙️ C'est lancé ! Les échantillons arrivent ici dans 1 à 3 minutes (quelques secondes entre chaque). "
            "Quand tu as choisi : /voix suivi du nom, par exemple /voix sulafat.")


def cmd_voix(ctx: Ctx, args: str) -> None:
    if args and norm(args).split()[:1] == ["essai"]:
        _voix_essai(ctx, args)
        return
    parsed = reglages.parse_scoped(args, reglages.voice_spans) if args else None
    problem = _scope_problem(parsed) if parsed else None
    if problem:
        ctx.say(problem)
        return
    if not parsed or not parsed.orders:
        ctx.say(VOICE_HELP + "\n" + reglages.podcast_summary(ctx.settings, int(ctx.cfg.get("mots_cibles", 300))))
        return
    _set_scoped(ctx, "voix", parsed)


def cmd_vitesse(ctx: Ctx, args: str) -> None:
    parsed = reglages.parse_scoped(args, reglages.speed_spans) if args else None
    problem = _scope_problem(parsed) if parsed else None
    if problem:
        ctx.say(problem)
        return
    if not parsed or not parsed.orders:
        ctx.say("🐢🐇 Choisis : /vitesse lent · /vitesse normal · /vitesse rapide (ou un pourcentage, ex. /vitesse +15%) "
                "· selon les jours : /vitesse lent semaine, rapide week-end\n"
                + reglages.podcast_summary(ctx.settings, int(ctx.cfg.get("mots_cibles", 300))))
        return
    _set_scoped(ctx, "vitesse", parsed)


_SCOPE_LABEL = {"podcast": "le podcast", "questions": "mes questions", "perso": "mes infos perso (cours, anniversaires, rappels)",
                "tout": "tout"}


def _days_label(days: list[str]) -> str:
    return reglages.days_phrase(tuple(reglages.DAYS.index(d) for d in days if d in reglages.DAYS))


def _guess_scope(text: str) -> str:
    low = norm(text)
    if re.search(r"\b(question|questions|inspi)\b", low):
        return "questions"
    if mentions_perso(text):                    # « cite mes cours sans la salle » : la façon de dire tes infos perso
        return "perso"
    if re.search(r"\b(podcast|actu|actus|matin|nouvelles|journal)\b", low):
        return "podcast"
    return "tout"


PERSO_PRIVACY_NOTE = ("ℹ️ Pour l'appliquer, je fais reformuler tes phrases perso (cours, anniversaires, rappels) par Groq, jamais par Gemini, "
                      "et je vérifie qu'aucune heure ni aucun nom ne change. /perso pour voir le résultat.")


def cmd_consignes(ctx: Ctx) -> None:
    rules = ctx.mem.consignes()
    if not rules:
        ctx.say("📌 Aucune consigne pour l'instant. Dis-moi par exemple « parle-moi de façon plus décontractée » "
                "(message ou vocal), ou écris /consigne suivi de ce que tu veux.")
        return
    lines = [f"📌 Tes consignes ({len(rules)})", ""]
    for n, c in enumerate(rules, 1):
        mark = " 🔒" if c.get("prive") else ""
        scope = _SCOPE_LABEL.get(c.get("portee", "tout"), "tout")
        if c.get("portee", "tout") in ("podcast", "tout") and mentions_perso(c["texte"]):
            scope += " + mes infos perso"
        days = f" · {_days_label(c['jours'])}" if c.get("jours") else ""
        lines.append(f"{n}. {c['texte']} — {scope}{days}{mark}")
    lines += ["", "Pour en retirer une : /oublier 2 · pour en ajouter : /consigne … ou dis-le-moi simplement "
                  "(« le week-end, … » pour la limiter à certains jours).",
              "🔒 = personnelle : elle ne part jamais vers Gemini (seulement Groq)."]
    ctx.say("\n".join(lines))


def cmd_consigne(ctx: Ctx, args: str, stamp: datetime) -> None:
    if len(args) < 6:
        ctx.say("Écris la consigne après la commande. Exemple : /consigne parle-moi de façon décontractée et fais des phrases courtes")
        return
    scope = _guess_scope(args)
    private = extract.is_sensitive(args)
    when = reglages.parse_day_scope(args, topic_guard=True, strict=False)
    if when.problem:
        ctx.say("🤔 Une consigne vaut pour toujours, ou pour certains jours de la semaine (« en semaine », « le week-end », « le lundi »). "
                "Je ne sais pas encore en faire une pour une date précise ni avec une exception (« sauf le dimanche »).")
        return
    days = [reglages.DAYS[d] for d in when.days or ()]
    if ctx.mem.add_consigne(args, scope, stamp.date().isoformat(), "commande", private, days):
        where = _SCOPE_LABEL[scope] + (f" · {_days_label(days)}" if days else "")
        lines = [f"📌 Consigne retenue : « {args[:160]} » (concerne : {where}).",
                 "/consignes pour la liste, /oublier N pour la retirer."]
        if scope == "perso" or (scope in ("podcast", "tout") and mentions_perso(args)):
            lines.append(PERSO_PRIVACY_NOTE)
        ctx.say("\n".join(lines))
    else:
        ctx.say("📌 Cette consigne existe déjà (ou elle est trop courte). /consignes pour voir la liste.")


def cmd_perso(ctx: Ctx, args: str, stamp: datetime) -> None:
    """Écoute à l'avance (en texte) comment tes cours, anniversaires et rappels seront dits : /perso, /perso demain, /perso lundi…"""
    today = stamp.date()
    day = reglages.target_day(args, today) or today
    label = reglages.day_label(day, today)
    segment = perso.build_segment_detail(ctx.racine, ctx.cfg.get("perso"), day)
    if not segment.text:
        ctx.say(f"🗣️ {label} : rien à dire (pas d'anniversaire, de rappel ni de cours ce jour-là).")
        return
    rules = ctx.mem.consignes_for("perso", allow_private=True, day=day) if (ctx.cfg.get("perso") or {}).get("style_ia", True) else []
    if not rules:
        note = ("Aucune consigne sur tes infos perso : c'est le texte de base. Pour le changer, dis-moi par exemple "
                "« cite mes cours sans la salle » ou écris /consigne cite mes cours sans la salle.")
        result = perso_style.Result(segment.text)
    else:
        result = perso_style.restyle(segment.text, rules, cfg=ctx.cfg, keys=ctx.keys, prenom=ctx.prenom,
                                     anchors=segment.anchors, pacer=ctx.pacer)
        if result.fallback:
            what = "ta consigne" if len(rules) == 1 else f"tes {len(rules)} consignes"
            note = (f"⚠️ Je n'ai pas pu appliquer {what} : {result.status.partition(':')[2].strip()}. "
                    "Voici le texte de base ; je réessaie à chaque podcast.")
        else:
            note = (f"✅ {len(rules)} consigne{'s' if len(rules) > 1 else ''} appliquée{'s' if len(rules) > 1 else ''} "
                    "(reformulé par Groq, puis contrôlé : aucune heure, aucun nom ni aucun jour n'a changé).")
    ctx.say(f"🗣️ Voici comment je te dirais tes infos perso {label} :\n\n« {result.text} »\n\n{note}")


def cmd_oublier(ctx: Ctx, args: str) -> None:
    m = re.search(r"\d+", args or "")
    gone = ctx.mem.remove_consigne(int(m.group(0))) if m else None
    if gone:
        ctx.say(f"🗑️ Consigne retirée : « {gone['texte']} ».")
    else:
        ctx.say("Dis-moi quelle consigne retirer : /consignes pour la liste, puis par exemple /oublier 2.")


def cmd_cours(ctx: Ctx, args: str, stamp: datetime) -> None:
    today = stamp.date()
    data = edt.load(ctx.mem.p_edt)
    if not args:
        ctx.say(edt.week_text(data, today))
        return
    if norm(args) in ("effacer", "reset", "vider", "supprimer", "tout effacer", "efface"):
        edt.clear(data)
        edt.save(ctx.mem.p_edt, data)
        ctx.say("🗑️ Emploi du temps effacé. Renvoie une image ou écris /cours suivi de tes cours pour le refaire.")
        return
    try:
        reading = vision.read_text(args, now=ctx.now, cfg=ctx.cfg, keys=ctx.keys, prenom=ctx.prenom, pacer=ctx.pacer)
    except llm.LLMError as exc:
        log.warning("Emploi du temps écrit illisible : %s", str(exc).splitlines()[0])
        ctx.say("⚠️ Je n'arrive pas à lire ça pour l'instant : réessaie dans un moment.")
        return
    if not reading.cours:
        ctx.say("Je n'ai trouvé aucun cours dans ton texte. Exemple : /cours lundi 9h-12h étalonnage salle 204 ; mardi 14h-17h histoire du cinéma")
        return
    recurring = not _THIS_WEEK_ONLY_RE.search(args)
    week = edt.resolve_week(reading.semaine_du, today)
    if not recurring and week is None:
        week = edt.school_week(today)
    report = edt.merge(data, reading.cours, reading.jours_vus, week=week, recurring=recurring, today=today)
    if not report["cours"]:
        ctx.say("Je n'ai pas pu lire de jour ni d'horaire dans ton texte. Exemple : /cours lundi 9h-12h étalonnage")
        return
    edt.save(ctx.mem.p_edt, data)
    ctx.say(timetable_ack(report, data, today, "ton texte"))


def cmd_podcast(ctx: Ctx) -> None:
    if not ctx.send:
        ctx.say("(essai) Je lancerais le podcast maintenant.")
        return
    last = ctx.state.get("dernier_podcast") or ""
    try:
        recent = bool(last) and ctx.now - datetime.fromisoformat(last) < timedelta(minutes=10)
    except ValueError:
        recent = False
    if recent:
        ctx.say("🎧 Un podcast vient déjà d'être demandé : patience, il arrive (3 à 5 minutes).")
        return
    try:
        github.dispatch("podcast.yml", {"origine": "telegram"})
    except github.GitHubError as exc:
        log.warning("Lancement du podcast impossible : %s", exc)
        if exc.missing:
            ctx.say("⚠️ Je ne peux pas le lancer d'ici. Dans GitHub : onglet Actions → « Podcast du matin » → Run workflow.")
        else:
            ctx.say(f"⚠️ GitHub n'a pas lancé le podcast : {exc}")
        return
    ctx.state["dernier_podcast"] = ctx.now.isoformat(timespec="seconds")
    ctx.say("🎧 C'est lancé ! Le podcast arrive ici dans 3 à 5 minutes.")


_BUTTON_COMMANDS = {
    telegram.BTN_INSPI: "/inspi", telegram.BTN_MEMOIRE: "/memoire",
    telegram.BTN_REVEIL: "/reveil", telegram.BTN_COURS: "/cours",
}


def handle_command(ctx: Ctx, m: telegram.Incoming) -> None:
    text = m.text.strip()
    stamp = _stamp(ctx, m.ts)
    ctx.stats["commandes"] += 1
    if text in _BUTTON_COMMANDS:
        command, args = _BUTTON_COMMANDS[text], ""
    else:
        command, args = text.split()[0].lower().split("@")[0], text.partition(" ")[2].strip()
    if command in ("/start", "/aide", "/help"):
        ctx.say(WELCOME, markup=telegram.KEYBOARD)
        if command == "/start":
            try:
                ctx.bot.set_commands()
            except telegram.TelegramError as exc:
                log.warning("Menu de commandes non installé : %s", exc)
    elif command == "/inspi":
        ctx.wants_inspi = True
    elif command in ("/memoire", "/mémoire"):
        ctx.say(memoire_text(ctx))
    elif command in ("/reveil", "/réveil"):
        cmd_reveil(ctx, args, stamp)
    elif command in ("/reglages", "/réglages", "/reglage"):
        ctx.say(settings_text(ctx))
    elif command in ("/duree", "/durée"):
        cmd_duree(ctx, args)
    elif command == "/voix":
        cmd_voix(ctx, args)
    elif command == "/vitesse":
        cmd_vitesse(ctx, args)
    elif command == "/consignes":
        cmd_consignes(ctx)
    elif command == "/consigne":
        cmd_consigne(ctx, args, stamp)
    elif command == "/oublier":
        cmd_oublier(ctx, args)
    elif command == "/perso":
        cmd_perso(ctx, args, stamp)
    elif command == "/cours":
        cmd_cours(ctx, args, stamp)
    elif command == "/podcast":
        cmd_podcast(ctx)
    else:
        ctx.say(HELP_UNKNOWN)


def handle_message(ctx: Ctx, m: telegram.Incoming) -> None:
    if m.kind == "audio":
        ctx.stats["audio"] += 1
        enqueue_media(ctx, m)
        return
    if m.kind == "image":
        ctx.stats["images"] += 1
        enqueue_media(ctx, m)
        return
    if m.kind == "autre":
        ctx.say(HELP_FILE)
        return
    text = m.text.strip()
    if not text:
        return
    if text.startswith("/") or text in _BUTTON_COMMANDS:
        handle_command(ctx, m)
    else:
        ctx.stats["texte"] += 1
        handle_text_entry(ctx, m)


# --------------------------------------------------------------------------- une passe complète
def flush(ctx: Ctx) -> int:
    sent = 0
    for out in ctx.out:
        if not ctx.send:
            print("\n[Telegram] " + out.text + (" (+ voix)" if out.audio else ""))
            continue
        result = None
        try:
            if out.audio is not None and out.audio.exists():
                try:
                    result = ctx.bot.send_voice(out.audio, caption=out.text)
                except telegram.TelegramError as exc:
                    log.warning("Envoi de la voix impossible (%s) : j'envoie le texte", exc)
            if result is None:
                result = ctx.bot.send_message(out.text, reply_markup=out.markup)
        except telegram.TelegramError as exc:
            log.warning("Envoi Telegram impossible : %s", exc)
            continue
        sent += 1
        if out.on_sent is not None:
            out.on_sent(result)
    return sent


def run(
    bot: telegram.Bot,
    racine: Path,
    cfg: dict,
    keys: dict,
    now: datetime,
    *,
    tmp: Path,
    pacer: Pacer | None = None,
    deadline: float | None = None,
    force_inspi: bool = False,
    send: bool = True,
    origine: str = "",
) -> dict:
    mem = Memoire(racine)
    state = mem.load_state()
    profile = triage.load_profile(racine)
    ctx = Ctx(
        bot=bot, mem=mem, racine=Path(racine), cfg=cfg, keys=keys, now=now, tmp=Path(tmp), state=state,
        pacer=pacer or Pacer(int((cfg.get("journal") or {}).get("tpm", 6500))), deadline=deadline, send=send,
        prenom=profile.get("prenom") or str(cfg.get("prenom") or "").strip(),
    )
    ctx.wants_inspi = force_inspi
    ctx.origine = origine
    ctx.settings = mem.load_reglages()
    if origine == "sonnette":  # la sonnette m'a lancé : elle est installée et vivante
        ctx.settings["sonnette_vue"] = now.isoformat(timespec="seconds")
        ctx.settings_changed = True

    try:
        updates = bot.get_updates(offset=state["offset"] or None)
    except telegram.Conflict:
        log.warning("Telegram : un webhook est actif ou un autre programme lit les messages (409). Passage sans lecture.")
        ctx.stats["alerte"] = "Telegram renvoie 409 : un webhook est actif sur ce bot, ou un autre programme lit ses messages."
        updates = []
    except telegram.TelegramError as exc:
        log.warning("Lecture de Telegram impossible : %s", exc)
        if exc.code in (401, 404):
            ctx.stats["alerte"] = "Telegram refuse le token du bot : vérifie le secret TELEGRAM_BOT_TOKEN."
        updates = []

    for update in updates:
        state["offset"] = max(int(state["offset"]), int(update.get("update_id", 0)) + 1)
        m = telegram.normalize(update, bot.chat_id)
        if m is None:
            ctx.stats["ignores"] += 1
            continue
        handle_message(ctx, m)

    process_queue(ctx)
    retry_pending_extractions(ctx)
    maybe_question(ctx)
    if int(ctx.settings.get("journal_offset") or 0) != int(state["offset"]):
        # La sonnette lit cette valeur : elle sait ainsi quels messages sont déjà traités (Telegram, lui, ne les
        # oublie qu'au passage suivant) et ne relance pas le journal pour rien.
        ctx.settings["journal_offset"] = int(state["offset"])
        ctx.settings_changed = True
    if ctx.settings_changed:
        mem.save_reglages(ctx.settings)
    mem.save_state(state)  # tout est rangé avant d'écrire à Telegram
    sent = flush(ctx)
    mem.save_state(state)  # numéro du message-question, date de la question du soir
    summary = {**ctx.stats, "messages": len(updates), "envoyes": sent, "en_file": len(state["file"])}
    log.info("Passage terminé : %s", summary)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Journal vocal (Telegram).")
    parser.add_argument("--racine", default=str(ROOT))
    parser.add_argument("--config", default=None)
    parser.add_argument("--inspi", action="store_true", help="pose une question maintenant")
    parser.add_argument("--no-send", action="store_true", help="n'envoie rien dans Telegram")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    racine = Path(args.racine).resolve()
    load_env(racine / ".env")
    keys = llm.keys_from_env(os.environ)
    bot = telegram.Bot.from_env(os.environ)
    if bot is None:
        print("Il manque TELEGRAM_BOT_TOKEN et/ou TELEGRAM_CHAT_ID (voir le README, étape Telegram).")
        return 1
    if not keys.get("groq"):
        print("Il manque GROQ_API_KEY (voir le README, étape Groq).")
        return 1
    cfg = yaml.safe_load(Path(args.config or racine / "config.yaml").read_text(encoding="utf-8")) or {}
    now = datetime.now(ZoneInfo(cfg.get("fuseau", "Europe/Paris")))
    minutes = float((cfg.get("journal") or {}).get("duree_max_min", 14))
    with tempfile.TemporaryDirectory(prefix="journal-") as tmp:
        summary = run(bot, racine, cfg, keys, now, tmp=Path(tmp), deadline=time.time() + minutes * 60,
                      force_inspi=args.inspi, send=not args.no_send,
                      origine=os.environ.get("JOURNAL_ORIGINE", "").strip()[:20])
    if summary.get("alerte"):  # le travail déjà fait est gardé, mais on le fait savoir (GitHub t'envoie un mail)
        print("⚠️ " + summary["alerte"])
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
