"""Journal vocal : lit ta conversation Telegram, transcrit, range dans la mémoire et pose des questions.

Usage :
    python -m src.journal               # une passe (le cron la lance toutes les 30 minutes)
    python -m src.journal --inspi       # pose une question tout de suite (comme le bouton)
    python -m src.journal --no-send     # essai : n'envoie rien dans Telegram

Codes de sortie : 0 = ok, 1 = Telegram ou Groq pas configurés, 2 = Telegram refuse (token ou webhook).
Les textes de tes messages ne sont jamais écrits dans les logs.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

import yaml

from . import audio, extract, inspi, llm, stt, telegram, triage, tts
from .fr import MOIS, duree_fr, jour_court_fr
from .main import load_env
from .memoire import Memoire
from .pacer import Pacer

ROOT = Path(__file__).resolve().parent.parent
log = logging.getLogger("journal")

WELCOME = (
    "👋 Salut ! Je suis ton journal vocal.\n\n"
    "🎙️ Envoie-moi des vocaux (30 secondes ou plus d'une heure) ou des textes pour me raconter ta vie : "
    "je les transcris et j'en retiens ce qui compte (anniversaires, projets, goûts, échéances…).\n"
    "💡 Plus d'inspiration ? Appuie sur « 💡 Inspi » : je te pose une question.\n"
    "🧠 « 🧠 Mémoire » te montre ce que j'ai appris et d'où viennent tes actus.\n"
    "🌅 Chaque matin, ton podcast arrive ici.\n\n"
    "Je lis tes messages en général dans la demi-heure. Seul le texte est gardé, jamais l'audio."
)
HELP_UNKNOWN = "Je ne connais pas cette commande. Tape /aide, ou utilise les boutons en bas."


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
    stats: dict = field(default_factory=lambda: {"texte": 0, "audio": 0, "commandes": 0, "ignores": 0, "entrees": 0})

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


def _new_entry(ctx: Ctx, eid: str, ts: int, kind: str, text: str, duration: int | None, answers: str | None) -> dict:
    when = datetime.fromtimestamp(ts, ctx.tz) if ts else ctx.now
    return {
        "id": eid, "date": when.date().isoformat(), "heure": when.strftime("%H:%M"), "type": kind,
        "duree_s": int(duration) if duration else None, "texte": text.strip(), "resume": "",
        "repond_a": answers, "extrait": False, "essais_extraction": 0,
    }


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
    qid = _link_question(ctx, m.reply_to, eid)
    entry = _new_entry(ctx, eid, m.ts, "texte", m.text, None, qid)
    ctx.mem.add_entry(entry, (ctx.mem.question(qid) or {}).get("texte"))
    ctx.stats["entrees"] += 1
    finish_entry(ctx, entry)


def enqueue_audio(ctx: Ctx, m: telegram.Incoming) -> None:
    eid = _entry_id(m)
    if ctx.mem.has_entry(eid) or any(i["id"] == eid for i in ctx.state["file"]):
        return
    ctx.state["file"].append({
        "id": eid, "file_id": m.file_id, "taille": m.file_size, "duree": m.duration, "nom": m.name,
        "recu": m.ts, "legende": m.text, "repond_a": _link_question(ctx, m.reply_to, eid),
        "textes": [], "essais": 0, "erreur": "",
    })


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
    try:
        ex = extract.extract(
            entry["texte"], entry_date=entry["date"], cfg=ctx.cfg, keys=ctx.keys, pacer=ctx.pacer,
            prenom=ctx.prenom, known_topics=_known_topics(ctx), question_text=question,
            resume_from=partial, on_progress=progress, deadline=ctx.deadline,
        )
    except extract.ExtractPause:
        log.info("Extraction en pause (temps écoulé) : reprise au prochain passage")
        return None
    except (llm.LLMError, extract.ExtractError) as exc:
        log.warning("Extraction impossible pour l'instant : %s", str(exc).splitlines()[0])
        ctx.mem.update_entry(entry["id"], essais_extraction=int(entry.get("essais_extraction", 0)) + 1)
        return None
    applied = ctx.mem.apply_extraction(entry, ex, ctx.now.date())
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
    return lines[:7]


def ack_text(entry: dict, applied: dict | None) -> str:
    head = "✅ Noté"
    if entry.get("type") == "vocal" and entry.get("duree_s"):
        head += f" ({duree_fr(entry['duree_s'])} de vocal)"
    if applied is None:
        return head + ". Je rangerai les infos au prochain passage."
    lines = _details(applied)
    return head + (".\n" + "\n".join(lines) if lines else ".")


def finish_entry(ctx: Ctx, entry: dict) -> None:
    ctx.say(ack_text(entry, try_extract(ctx, entry)))


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
    log.warning("Vocal %s : %s (essai %d)", item["id"], reason, item["essais"])
    if item["essais"] >= 4:
        when = datetime.fromtimestamp(item["recu"], ctx.tz).strftime("%d/%m à %H:%M") if item.get("recu") else ""
        ctx.say(f"⚠️ Je n'arrive pas à traiter ton vocal du {when} ({reason[:120]}). Je l'abandonne : tu peux le renvoyer.")
        return "abandon"
    return "attente"


def process_item(ctx: Ctx, item: dict) -> str:
    """'fini', 'abandon' (à retirer de la file) ou 'attente' / 'attente_quota' / 'attente_temps' (à reprendre)."""
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
    duration = item.get("duree") or int(sum(c.duration for c in chunks))
    entry = _new_entry(ctx, item["id"], item.get("recu", 0), "vocal", transcript, duration, item.get("repond_a"))
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
                           pending=pending.get("texte", ""))
    qid = ctx.mem.next_question_id(ctx.now)
    record = {
        "id": qid, "date": today, "heure": ctx.now.strftime("%H:%M"), "texte": q["texte"], "categorie": q["categorie"],
        "origine": q["origine"], "banque_id": q.get("banque_id"), "message_id": None, "repondue_par": None,
    }
    voice: Path | None = None
    if ctx.j.get("question_vocale", True):
        try:
            voice = ctx.tmp / f"{qid}.mp3"
            tts.synthesize(q["texte"], ctx.cfg.get("voix", {}), voice)
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


def handle_command(ctx: Ctx, m: telegram.Incoming) -> None:
    text = m.text.strip()
    command = text.split()[0].lower().split("@")[0] if text.startswith("/") else ""
    ctx.stats["commandes"] += 1
    if command in ("/start", "/aide", "/help"):
        ctx.say(WELCOME, markup=telegram.KEYBOARD)
        if command == "/start":
            try:
                ctx.bot.set_commands()
            except telegram.TelegramError as exc:
                log.warning("Menu de commandes non installé : %s", exc)
    elif command == "/inspi" or text == telegram.BTN_INSPI:
        ctx.wants_inspi = True
    elif command in ("/memoire", "/mémoire") or text == telegram.BTN_MEMOIRE:
        ctx.say(memoire_text(ctx))
    else:
        ctx.say(HELP_UNKNOWN)


def handle_message(ctx: Ctx, m: telegram.Incoming) -> None:
    if m.kind == "audio":
        ctx.stats["audio"] += 1
        enqueue_audio(ctx, m)
        return
    text = m.text.strip()
    if not text:
        return
    if text.startswith("/") or text in (telegram.BTN_INSPI, telegram.BTN_MEMOIRE):
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
                      force_inspi=args.inspi, send=not args.no_send)
    if summary.get("alerte"):  # le travail déjà fait est gardé, mais on le fait savoir (GitHub t'envoie un mail)
        print("⚠️ " + summary["alerte"])
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
