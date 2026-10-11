"""Tests hors ligne du journal vocal : Telegram, transcription, mémoire, questions « Inspi », tri des actus.

Lancer :  python -m unittest discover -s tests -t . -v
Aucun accès réseau : un faux serveur local imite Telegram, Groq (texte + transcription), Gemini et les flux RSS.
"""
from __future__ import annotations

import itertools
import json
import shutil
import subprocess
import tempfile
import time
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest import mock

import yaml

from src import audio, extract, inspi, journal, llm, main, pacer, perso, stt, telegram, tts
from src.memoire import Memoire
from tests.test_podcast import PARIS, WorldCase

needs_ffmpeg = unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg absent")


def make_audio(seconds: int) -> bytes:
    """Un vrai petit fichier audio (bip) au format d'un vocal Telegram."""
    tmp = Path(tempfile.mkdtemp())
    try:
        out = tmp / "a.ogg"
        subprocess.run(
            ["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
             "-ac", "1", "-c:a", "libopus", "-b:a", "24k", str(out)], check=True,
        )
        return out.read_bytes()
    finally:
        shutil.rmtree(tmp, True)


# =========================================================================== petits modules
class TestNormalize(unittest.TestCase):
    chat = 4242

    def upd(self, **msg):
        base = {"message_id": 7, "date": 1_700_000_000, "chat": {"id": self.chat, "type": "private"},
                "from": {"id": self.chat, "is_bot": False}}
        return {"update_id": 99, "message": {**base, **msg}}

    def test_kinds(self):
        m = telegram.normalize(self.upd(text="bonjour", reply_to_message={"message_id": 3}), self.chat)
        self.assertEqual((m.kind, m.text, m.reply_to, m.update_id, m.message_id), ("text", "bonjour", 3, 99, 7))
        v = telegram.normalize(self.upd(voice={"file_id": "F", "duration": 12, "file_size": 999}, caption="note"), self.chat)
        self.assertEqual((v.kind, v.file_id, v.duration, v.text), ("audio", "F", 12, "note"))
        a = telegram.normalize(self.upd(audio={"file_id": "A", "duration": 60, "file_name": "x.m4a"}), self.chat)
        self.assertEqual((a.kind, a.name), ("audio", "x.m4a"))
        d = telegram.normalize(self.upd(document={"file_id": "D", "file_name": "memo.m4a", "mime_type": "audio/mp4"}), self.chat)
        self.assertEqual(d.kind, "audio")

    def test_images_and_other_files(self):
        sizes = [{"file_id": "small", "width": 90, "height": 60, "file_size": 1500},
                 {"file_id": "big", "width": 1280, "height": 853, "file_size": 150_000},
                 {"file_id": "mid", "width": 800, "height": 533, "file_size": 60_000}]
        p = telegram.normalize(self.upd(photo=sizes, caption="mon emploi du temps"), self.chat)
        self.assertEqual((p.kind, p.file_id, p.text, p.mime), ("image", "big", "mon emploi du temps", "image/jpeg"))  # la plus grande
        self.assertEqual(telegram.normalize(self.upd(photo=[{"file_id": "P"}]), self.chat).file_id, "P")
        no_size = [{"file_id": "tiny", "width": 90, "height": 60, "file_size": 5000}, {"file_id": "large", "width": 1280, "height": 853}]
        self.assertEqual(telegram.normalize(self.upd(photo=no_size), self.chat).file_id, "large")  # file_size est facultatif
        png = telegram.normalize(self.upd(document={"file_id": "D", "file_name": "edt.png", "mime_type": "image/png"}), self.chat)
        self.assertEqual((png.kind, png.mime, png.name), ("image", "image/png", "edt.png"))
        heic = telegram.normalize(self.upd(document={"file_id": "H", "file_name": "IMG_0042.HEIC"}), self.chat)
        self.assertEqual(heic.kind, "image")                                                     # reconnue à l'extension
        pdf = telegram.normalize(self.upd(document={"file_id": "D", "file_name": "cv.pdf", "mime_type": "application/pdf"}), self.chat)
        self.assertEqual((pdf.kind, pdf.name), ("autre", "cv.pdf"))                              # reconnu, mais je ne sais pas le lire
        self.assertIsNone(telegram.normalize(self.upd(photo=[]), self.chat))
        self.assertIsNone(telegram.normalize(self.upd(photo=[{"width": 5}]), self.chat))

    def test_ignored(self):
        self.assertIsNone(telegram.normalize(self.upd(sticker={"file_id": "S"}), self.chat))
        self.assertIsNone(telegram.normalize(self.upd(text="salut"), 999))                       # autre conversation
        self.assertIsNone(telegram.normalize(self.upd(text="x", chat={"id": self.chat, "type": "group"}), self.chat))
        self.assertIsNone(telegram.normalize(self.upd(text="x", **{"from": {"id": 1, "is_bot": True}}), self.chat))
        self.assertIsNone(telegram.normalize({"update_id": 1, "edited_message": {"text": "x"}}, self.chat))


class TestBot(WorldCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(telegram, "API_ROOT", self.world.base + "/telegram")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.bot = telegram.Bot(self.world.tg_token, self.world.tg_chat)

    def test_from_env(self):
        self.assertIsNone(telegram.Bot.from_env({}))
        self.assertIsNone(telegram.Bot.from_env({"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "pas un nombre"}))
        bot = telegram.Bot.from_env({"TELEGRAM_BOT_TOKEN": " t ", "TELEGRAM_CHAT_ID": " 12 "})
        self.assertEqual((bot.token, bot.chat_id), ("t", 12))

    def test_long_messages_are_split(self):
        self.bot.send_message("mot " * 2500)
        sent = self.world.sent("sendMessage")
        self.assertEqual(len(sent), 3)
        self.assertTrue(all(len(s["text"]) <= 4096 for s in sent))

    def test_download_and_errors(self):
        self.world.tg_files["vo1"] = b"OggS-data"
        path = self.bot.download("vo1", self.tmp / "d" / "a.oga")
        self.assertEqual(path.read_bytes(), b"OggS-data")
        self.world.tg_files["toobig1"] = b"x"
        with self.assertRaises(telegram.FileTooBig):
            self.bot.download("toobig1", self.tmp / "b.oga")
        with self.assertRaises(telegram.TelegramError):
            self.bot.download("inconnu", self.tmp / "c.oga")

    def test_token_never_appears_in_errors(self):
        wrong = telegram.Bot("999:SECRET-TOKEN", 1)
        with self.assertRaises(telegram.TelegramError) as ctx:
            wrong.get_updates()
        self.assertNotIn("SECRET-TOKEN", str(ctx.exception))
        dead = telegram.Bot("999:SECRET-TOKEN", 1)
        with mock.patch.object(telegram, "API_ROOT", "http://127.0.0.1:1"):
            with self.assertRaises(telegram.TelegramError) as ctx:
                dead.send_message("x")
        self.assertNotIn("SECRET-TOKEN", str(ctx.exception))

    def test_conflict_is_reported(self):
        self.world.tg_conflict = True
        with self.assertRaises(telegram.Conflict):
            self.bot.get_updates()


class TestStt(WorldCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(stt, "GROQ_STT_URL", self.world.base + "/groq/audio/transcriptions")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.file = self.tmp / "a.ogg"
        self.file.write_bytes(b"OggS" + b"\0" * 100)

    def test_request_shape_and_text(self):
        self.world.transcripts = ["Bonjour tout le monde"]
        text = stt.transcribe(self.file, "q-key", model="whisper-large-v3-turbo", language="fr")
        self.assertEqual(text, "Bonjour tout le monde")
        req = self.world.calls("/groq/audio")[0]
        fields = req.multipart()
        self.assertEqual(req.headers.get("Authorization"), "Bearer q-key")
        self.assertEqual((fields["model"], fields["language"], fields["response_format"]),
                         ("whisper-large-v3-turbo", "fr", "verbose_json"))
        self.assertEqual(fields["file"][0], "a.ogg")

    def test_hallucinations_and_silence_are_dropped(self):
        payload = {"text": "x", "segments": [
            {"text": "Je commence mon montage ce matin.", "no_speech_prob": 0.02, "avg_logprob": -0.3, "compression_ratio": 1.4},
            {"text": "Sous-titres réalisés par la communauté d'Amara.org", "no_speech_prob": 0.1, "avg_logprob": -0.4, "compression_ratio": 1.2},
            {"text": "euh euh", "no_speech_prob": 0.9, "avg_logprob": -1.5, "compression_ratio": 1.0},
            {"text": "la la la la la la la la", "no_speech_prob": 0.1, "avg_logprob": -0.5, "compression_ratio": 3.1},
            {"text": "Merci d'avoir regardé cette vidéo.", "no_speech_prob": 0.1, "avg_logprob": -0.4, "compression_ratio": 1.1},
            {"text": "Et puis j'ai exporté la séquence.", "no_speech_prob": 0.03, "avg_logprob": -0.2, "compression_ratio": 1.3},
        ]}
        self.world.stt_fn = lambda req, fields: (200, payload)
        text = stt.transcribe(self.file, "q-key", model="m")
        self.assertEqual(text, "Je commence mon montage ce matin. Et puis j'ai exporté la séquence.")

    def test_rate_limit_and_bad_key(self):
        self.world.stt_fn = lambda req, fields: (429, "Rate limit reached. Please try again in 7m12.5s.")
        with self.assertRaises(stt.RateLimited) as ctx:
            stt.transcribe(self.file, "q-key", model="m")
        self.assertEqual(ctx.exception.retry_after, 1.0)  # l'en-tête retry-after du faux serveur
        self.assertEqual(len(self.world.calls("/groq/audio")), 1)  # pas d'insistance
        self.world.stt_fn = lambda req, fields: (401, "Invalid API Key")
        with self.assertRaises(stt.STTError):
            stt.transcribe(self.file, "mauvaise", model="m")

    def test_retry_after_is_read_from_the_message(self):
        class Resp:
            headers: dict = {}
        self.assertEqual(stt._retry_after(Resp(), "Please try again in 7m12.5s."), 7 * 60 + 12.5)
        self.assertEqual(stt._retry_after(Resp(), "Please try again in 1h2m."), 3720)
        self.assertEqual(stt._retry_after(Resp(), "rien"), 60.0)

    def test_server_errors_are_retried(self):
        calls = []

        def fn(req, fields):
            calls.append(1)
            return (503, "oups") if len(calls) < 3 else (200, {"text": "ok enfin", "segments": []})

        self.world.stt_fn = fn
        self.assertEqual(stt.transcribe(self.file, "q-key", model="m"), "ok enfin")

    def test_quota_arithmetic(self):
        self.assertEqual(stt.billed(3), 10)
        self.assertEqual(stt.billed(125.5), 125.5)
        history = [[1000.0, 600.0], [2000.0, 300.0], [4500.0, 100.0]]
        self.assertEqual(stt.usage_in_window(history, 4600.0), 400.0)  # ce qui date de plus d'une heure ne compte plus
        self.assertEqual(stt.prune(history, 4600.0), [[2000.0, 300.0], [4500.0, 100.0]])


class TestAudio(unittest.TestCase):
    @needs_ffmpeg
    def test_long_recording_is_cut_into_chunks(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "long.ogg"
            src.write_bytes(make_audio(25))
            chunks = audio.prepare(src, Path(tmp) / "w", 10)
            self.assertEqual(len(chunks), 3)
            self.assertAlmostEqual(sum(c.duration for c in chunks), 25, delta=0.5)
            self.assertTrue(all(c.path.suffix == ".ogg" for c in chunks))

    def test_without_ffmpeg_small_known_files_pass_and_others_are_refused(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(audio, "ensure_ffmpeg", return_value=None):
            small = Path(tmp) / "a.oga"
            small.write_bytes(b"x" * 100)
            self.assertEqual([c.path for c in audio.prepare(small, Path(tmp) / "w", 600, 42)], [small])
            odd = Path(tmp) / "a.amr"
            odd.write_bytes(b"x")
            with self.assertRaises(audio.AudioError):
                audio.prepare(odd, Path(tmp) / "w", 600)

    @needs_ffmpeg
    def test_unreadable_file_gives_a_clear_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            junk = Path(tmp) / "junk.ogg"
            junk.write_bytes(b"ceci n'est pas de l'audio")
            with self.assertRaises(audio.AudioError):
                audio.prepare(junk, Path(tmp) / "w", 10)


class TestPacer(unittest.TestCase):
    def test_waits_when_the_minute_is_full(self):
        clock = {"t": 0.0}
        slept = []

        def sleep(seconds):
            slept.append(seconds)
            clock["t"] += seconds

        p = pacer.Pacer(1000, clock=lambda: clock["t"], sleep=sleep)
        self.assertEqual(p.wait(600), 0)           # premier appel : libre
        self.assertEqual(p.wait(300), 0)           # 900 ≤ 1000
        waited = p.wait(400)                       # 1300 > 1000 : il faut laisser expirer le premier
        self.assertGreater(waited, 50)
        self.assertEqual(len(slept), 1)

    def test_oversized_request_never_blocks_forever(self):
        p = pacer.Pacer(100, clock=lambda: 0.0, sleep=lambda s: None)
        p.wait(90)
        p.wait(500)  # ne boucle pas à l'infini même si l'horloge est figée


# =========================================================================== mémoire
class TestMemoire(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.mem = Memoire(self.tmp)

    def test_birthdays_add_modify_keep_year(self):
        self.assertEqual(self.mem.upsert_birthday("Karim", 12, 3, 2005, "s"), "ajoute")
        self.assertEqual(self.mem.upsert_birthday("karim", 12, 3, 2005, "s"), "identique")
        self.assertEqual(self.mem.upsert_birthday("Karim", 21, 3, None, "s"), "modifie")
        (entry,) = self.mem.birthdays()
        self.assertEqual((entry["jour"], entry["annee"]), (21, 2005))
        self.assertIn("anniversaires.yaml", (self.mem.p_anniv).read_text(encoding="utf-8"))  # en-tête explicatif

    def test_reminders_dedupe_and_forget_old_ones(self):
        today = date(2026, 10, 4)
        self.assertTrue(self.mem.add_reminder("rendre le dossier", "2026-10-16", "s", today))
        self.assertFalse(self.mem.add_reminder("Rendre le dossier", "2026-10-16", "s", today))
        self.assertTrue(self.mem.add_reminder("ancien", "2026-08-01", "s", today))   # écrit…
        self.assertTrue(self.mem.add_reminder("autre", "2026-10-20", "s", today))    # …puis oublié (> 30 jours)
        self.assertEqual([r["texte"] for r in self.mem.reminders()], ["rendre le dossier", "autre"])

    def test_interests_need_two_mentions_or_real_enthusiasm(self):
        self.assertEqual(self.mem.merge_interests([{"sujet": "pêche", "intensite": 1}], [], "2026-10-01"), [])
        data = self.mem.interests()
        self.assertEqual((len(data["aime"]), len(data["candidats"])), (0, 1))
        promoted = self.mem.merge_interests([{"sujet": "Pêche", "intensite": 1}], [], "2026-10-02")
        self.assertEqual(promoted, ["pêche"])                       # 2e mention → retenu (libellé d'origine gardé)
        promoted = self.mem.merge_interests([{"sujet": "Houdini", "intensite": 3}], [], "2026-10-03")
        self.assertEqual(promoted, ["Houdini"])                     # passion nette → retenu tout de suite
        aime = {e["sujet"]: e["poids"] for e in self.mem.interests()["aime"]}
        self.assertEqual(aime, {"Houdini": 3, "pêche": 1})

    def test_avoided_topics_leave_the_liked_list(self):
        self.mem.merge_interests([{"sujet": "football", "intensite": 3}], [], "2026-10-01")
        self.mem.merge_interests([], [{"sujet": "Football"}], "2026-10-02")
        data = self.mem.interests()
        self.assertEqual(data["aime"], [])
        self.assertEqual(data["evite"], ["Football"])

    def test_state_roundtrip_and_corrupt_state(self):
        state = self.mem.load_state()
        self.assertEqual(state["offset"], 0)
        state["offset"] = 42
        self.mem.save_state(state)
        self.assertEqual(Memoire(self.tmp).load_state()["offset"], 42)
        self.mem.p_etat.write_text("{pas du json", encoding="utf-8")
        self.assertEqual(Memoire(self.tmp).load_state()["offset"], 0)

    def test_jsonl_survives_a_damaged_line(self):
        self.mem.add_facts([{"categorie": "projet", "texte": "Prépare un court-métrage"}], "e1", "2026-10-04")
        with open(self.mem.p_faits, "a", encoding="utf-8") as fh:
            fh.write("{ceci est cassé\n")
        self.mem.add_facts([{"categorie": "gout", "texte": "Aime le montage vidéo"}], "e2", "2026-10-04")
        self.assertEqual(len(self.mem.facts()), 2)
        self.assertEqual(self.mem.add_facts([{"categorie": "x", "texte": "prépare un COURT-métrage"}], "e3", "2026-10-05"), 0)

    def test_entry_markdown_and_digest(self):
        entry = {"id": "e1", "date": "2026-10-04", "heure": "14:00", "type": "vocal", "duree_s": 250,
                 "texte": "Bonjour journal", "resume": "Un début", "repond_a": None, "extrait": True}
        self.mem.add_entry(entry, "Parle-moi de ton école ?")
        md = (self.tmp / "memoire" / "journal" / "2026" / "2026-10-04.md").read_text(encoding="utf-8")
        self.assertIn("# 2026-10-04", md)
        self.assertIn("14:00 · vocal · 4 min 10", md)
        self.assertIn("Réponse à la question", md)
        self.assertIn("Un début", self.mem.digest())
        self.assertIn("encore vide", Memoire(self.tmp / "vide").digest())


# =========================================================================== extraction
class TestExtract(unittest.TestCase):
    def test_parse_keeps_only_valid_things(self):
        reply = json.dumps({
            "resume": "Une journée de montage.",
            "faits": [{"categorie": "projet", "texte": "Monte un court-métrage"}, {"categorie": "bizarre", "texte": "Aime la pêche à la cuillère"},
                      {"categorie": "x", "texte": "court"}, "Habite en banlieue parisienne"],
            "anniversaires": [{"nom": "Karim", "jour": 12, "mois": 3, "annee": 2005}, {"nom": "Léa", "jour": 29, "mois": 2},
                              {"nom": "Faux", "jour": 31, "mois": 2}, {"nom": "Futur", "jour": 1, "mois": 1, "annee": 2090}],
            "rappels": [{"texte": "rendre le dossier", "date": "2026-10-16"}, {"texte": "trop loin", "date": "2035-01-01"},
                        {"texte": "hier", "date": "2026-10-01"}, {"texte": "sans date", "date": "bientôt"}],
            "aime": [{"sujet": "Houdini", "intensite": 3}, {"sujet": "dépression", "intensite": 3}, {"sujet": "ok", "intensite": 2},
                     {"sujet": "photographie argentique", "intensite": 9, "sensible": False}, {"sujet": "foi", "sensible": True},
                     {"sujet": "argent de poche", "intensite": 2}],
            "evite": [{"sujet": "football"}, {"sujet": "politique française"}],
        })
        ex = extract.parse(reply, "2026-10-04")
        self.assertEqual([f["categorie"] for f in ex.faits], ["projet", "autre", "autre"])
        self.assertEqual([(b["nom"], b.get("annee")) for b in ex.anniversaires], [("Karim", 2005), ("Léa", None), ("Futur", None)])
        self.assertEqual([r["date"] for r in ex.rappels], ["2026-10-16"])
        self.assertEqual({a["sujet"]: a["intensite"] for a in ex.aime}, {"Houdini": 3, "photographie argentique": 3})
        self.assertEqual([e["sujet"] for e in ex.evite], ["football"])

    def test_parse_rejects_garbage_and_tolerates_fences(self):
        with self.assertRaises(ValueError):
            extract.parse("désolé, je ne peux pas", "2026-10-04")
        ex = extract.parse('```json\n{"resume": "ok", "faits": []}\n```', "2026-10-04")
        self.assertEqual(ex.resume, "ok")

    def test_sensitivity_uses_whole_words(self):
        self.assertTrue(extract.is_sensitive("Santé mentale"))
        self.assertTrue(extract.is_sensitive("argent de poche"))
        self.assertFalse(extract.is_sensitive("photographie argentique"))
        self.assertFalse(extract.is_sensitive("effets visuels 3D"))

    def test_windows(self):
        text = " ".join(f"Phrase numéro {i} avec quelques mots." for i in range(100))
        windows = extract.split_windows(text, 120)
        self.assertGreater(len(windows), 3)
        self.assertTrue(all(len(w.split()) <= 120 for w in windows))
        self.assertEqual(" ".join(windows).split(), text.split())
        giant = "mot " * 500
        self.assertEqual([len(w.split()) for w in extract.split_windows(giant, 200)], [200, 200, 100])
        self.assertEqual(extract.split_windows("   ", 100), [])

    def test_merge_and_roundtrip(self):
        a = extract.Extraction(resume="A.", faits=[{"categorie": "projet", "texte": "Monte un film"}], aime=[{"sujet": "Houdini", "intensite": 2}])
        b = extract.Extraction(resume="B.", faits=[{"categorie": "projet", "texte": "monte un FILM"}], aime=[{"sujet": "Blender", "intensite": 1}])
        a.merge(b)
        self.assertEqual(len(a.faits), 1)
        self.assertEqual([t["sujet"] for t in a.aime], ["Houdini", "Blender"])
        self.assertEqual(extract.Extraction.from_dict(a.to_dict()), a)


class TestExtractCalls(WorldCase):
    def test_long_text_is_cut_and_resumable(self):
        calls = []
        counter = itertools.count(1)  # jamais remis à zéro : chaque tranche reçoit un numéro unique

        def fn(req, model, body):
            prompt = body["messages"][1]["content"]
            calls.append(prompt)
            n = next(counter)
            return 200, json.dumps({"resume": f"Partie {n}.", "faits": [{"categorie": "projet", "texte": f"Fait numéro {n} très précis"}],
                                    "aime": [{"sujet": "Houdini", "intensite": 2}]})

        self.world.groq_fn = fn
        self.cfg["journal"]["fenetre_mots"] = 50
        text = " ".join(f"Phrase {i} avec cinq mots." for i in range(40))  # ≈ 200 mots → 4 tranches
        progress = []
        with self.assertRaises(extract.ExtractPause):
            extract.extract(text, entry_date="2026-10-04", cfg=self.cfg, keys=self.keys, on_progress=progress.append,
                            deadline=time.time() - 1)
        self.assertEqual(len(calls), 0)
        # deux tranches puis pause
        stop_after = {"n": 0}

        def stopper(done):
            progress.append(done)
            stop_after["n"] = len(done)

        ticks = iter([0, 0, 10])  # le temps est écoulé avant la 3e tranche
        with self.assertRaises(extract.ExtractPause):
            extract.extract(text, entry_date="2026-10-04", cfg=self.cfg, keys=self.keys, on_progress=stopper,
                            deadline=5, clock=lambda: next(ticks))
        self.assertEqual(stop_after["n"], 2)
        done = progress[-1]
        calls.clear()
        total = extract.extract(text, entry_date="2026-10-04", cfg=self.cfg, keys=self.keys, resume_from=done)
        self.assertEqual(len(calls), 2)                    # seules les tranches restantes sont envoyées
        self.assertEqual(len(total.faits), 4)
        self.assertEqual(len(total.aime), 1)               # « Houdini » fusionné
        self.assertIn("Houdini", calls[0])                 # les libellés connus sont réutilisés

    def test_unreadable_reply_gets_a_second_chance_then_fails(self):
        answers = iter(["bla bla", json.dumps({"resume": "ok", "faits": []})])
        self.world.groq_fn = lambda req, model, body: (200, next(answers))
        ex = extract.extract("un texte assez long pour être traité par l'IA ici", entry_date="2026-10-04", cfg=self.cfg, keys=self.keys)
        self.assertEqual(ex.resume, "ok")
        self.world.groq_fn = lambda req, model, body: (200, "toujours n'importe quoi")
        with self.assertRaises(extract.ExtractError):
            extract.extract("un texte assez long pour être traité par l'IA ici", entry_date="2026-10-04", cfg=self.cfg, keys=self.keys)

    def test_only_groq_is_ever_called(self):
        extract.extract("Voici un message privé assez long à propos de Karim et de son anniversaire.",
                        entry_date="2026-10-04", cfg=self.cfg, keys=self.keys)
        self.assertEqual(self.world.calls("/gemini/"), [])
        self.assertEqual(len(self.world.calls("/groq/chat")), 1)


# =========================================================================== questions Inspi
class TestInspi(WorldCase):
    def setUp(self):
        super().setUp()
        self.mem = Memoire(self.tmp)

    def test_bank_starts_with_basics_then_fills_the_emptiest_theme(self):
        first = inspi.make_question(self.mem, self.cfg, self.keys, self.now)
        self.assertEqual((first["origine"], first["banque_id"]), ("banque", "b01"))
        self.mem.add_question({"id": "q1", "texte": first["texte"], "banque_id": "b01", "date": "2026-10-04", "heure": "10:00"})
        for i in range(5):  # le thème « personne » devient bien rempli
            self.mem.add_facts([{"categorie": "personne", "texte": f"Connaît la personne numéro {i}"}], "e", "2026-10-04")
        second = inspi.make_question(self.mem, self.cfg, self.keys, self.now)
        self.assertNotEqual(second["banque_id"], "b01")
        self.assertNotEqual(second["categorie"], "personne")
        self.assertEqual(self.world.calls("/groq/"), [])  # pas assez d'infos : aucune IA

    def test_ai_takes_over_once_the_memory_has_enough_and_never_repeats(self):
        for i in range(12):
            self.mem.add_facts([{"categorie": "projet", "texte": f"Travaille sur le projet numéro {i}"}], "e", "2026-10-04")
        self.mem.merge_interests([{"sujet": "Houdini", "intensite": 3}], [], "2026-10-04")
        q = inspi.make_question(self.mem, self.cfg, self.keys, self.now)
        self.assertEqual(q["origine"], "ia")
        self.assertIn("Houdini", q["texte"])
        body = self.world.calls("/groq/chat")[0].json["messages"][1]["content"]
        self.assertIn("Travaille sur le projet", body)          # la mémoire guide la question
        self.assertEqual(self.world.calls("/gemini/"), [])       # …et reste chez Groq
        self.mem.add_question({"id": "q1", "texte": q["texte"], "date": "2026-10-04", "heure": "10:00"})
        again = inspi.make_question(self.mem, self.cfg, self.keys, self.now)
        self.assertNotEqual(again["origine"], "ia")              # même question refusée → retombe sur la banque

    def test_when_everything_fails_a_generic_question_remains(self):
        self.world.groq_fn = lambda req, model, body: (500, "down")
        for i in range(12):
            self.mem.add_facts([{"categorie": "projet", "texte": f"Travaille sur le projet numéro {i}"}], "e", "2026-10-04")
        for qid, _cat, text in inspi.BANQUE:
            self.mem.add_question({"id": qid, "texte": text, "banque_id": qid, "date": "2026-10-04", "heure": "10:00"})
        q = inspi.make_question(self.mem, self.cfg, self.keys, self.now)
        self.assertEqual(q["origine"], "secours")

    def test_too_close(self):
        self.assertTrue(inspi.too_close("Où en es-tu avec Houdini en ce moment ?", ["Où en es-tu avec Houdini en ce moment, vraiment ?"]))
        self.assertFalse(inspi.too_close("Parle-moi de ton école", ["Quel est ton plus beau souvenir de vacances ?"]))
        self.assertGreaterEqual(len({q[0] for q in inspi.BANQUE}), 25)


# =========================================================================== le journal complet
class JournalCase(WorldCase):
    tts_kwargs: list = []

    def setUp(self):
        super().setUp()
        JournalCase.tts_kwargs = []
        self.now = datetime(2026, 10, 4, 14, 0, tzinfo=PARIS)
        for target, attr, value in [
            (telegram, "API_ROOT", self.world.base + "/telegram"),
            (stt, "GROQ_STT_URL", self.world.base + "/groq/audio/transcriptions"),
        ]:
            patcher = mock.patch.object(target, attr, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        voice = mock.patch.object(tts, "synthesize", side_effect=self.fake_tts)
        voice.start()
        self.addCleanup(voice.stop)
        self.racine = self.project()
        self.bot = telegram.Bot(self.world.tg_token, self.world.tg_chat)
        self.mem = Memoire(self.racine)
        self.work = self.tmp / "work"
        self.work.mkdir()
        self.cfg["journal"]["morceau_s"] = 600
        self.ts = int(self.now.timestamp())

    @staticmethod
    def fake_tts(text, cfg, out_path, **kw):
        JournalCase.tts_kwargs.append(dict(kw))                 # la question « Inspi » doit demander private=True
        Path(out_path).write_bytes(b"ID3" + b"\0" * 6000)
        return "fr-FR-TestNeural"

    def run_once(self, **kw):
        now = kw.pop("now", self.now)
        kw.setdefault("tmp", self.work)
        return journal.run(self.bot, self.racine, self.cfg, self.keys, now, **kw)

    def text(self, message, **kw):
        return self.world.user_text(message, ts=kw.pop("ts", self.ts), **kw)

    def voice(self, data=None, **kw):
        return self.world.user_voice(data if data is not None else make_audio(5), ts=kw.pop("ts", self.ts), **kw)

    def messages(self):
        return [s["text"] for s in self.world.sent("sendMessage")]

    BIG = ("N'oublie pas l'anniversaire de Karim le 12 mars 2005, c'est important pour moi. "
           "Je dois rendre mon dossier de montage le 16 octobre. J'adore le montage vidéo et Houdini en ce moment.")


class TestJournalText(JournalCase):
    def test_a_text_message_fills_the_whole_memory_and_stays_away_from_gemini(self):
        self.text(self.BIG)
        summary = self.run_once()
        self.assertEqual((summary["texte"], summary["entrees"], summary["envoyes"]), (1, 1, 1))

        (entry,) = self.mem.entries()
        self.assertTrue(entry["extrait"])
        self.assertEqual((entry["date"], entry["heure"], entry["type"]), ("2026-10-04", "14:00", "texte"))
        self.assertEqual(len(self.mem.facts()), 3)
        (birthday,) = self.mem.birthdays()
        self.assertEqual((birthday["nom"], birthday["jour"], birthday["mois"], birthday["annee"]), ("Karim", 12, 3, 2005))
        (reminder,) = self.mem.reminders()
        self.assertEqual((reminder["texte"], reminder["date"]), ("rendre mon dossier de montage", "2026-10-16"))
        self.assertEqual({e["sujet"] for e in self.mem.interests()["aime"]}, {"Houdini", "montage vidéo"})
        self.assertTrue((self.racine / "memoire/journal/2026/2026-10-04.md").exists())

        (ack,) = self.messages()
        for expected in ("✅ Noté", "3 infos retenues", "anniversaire de Karim le 12 mars", "vendredi 16 octobre", "Houdini"):
            self.assertIn(expected, ack)
        self.assertEqual(self.world.calls("/gemini/"), [])                       # rien chez Gemini
        self.assertIn("Karim", self.world.bodies("/groq/chat"))                  # tout chez Groq
        state = self.mem.load_state()
        self.assertEqual(state["offset"], 502)

        # …et le podcast s'en sert, sans que l'IA du podcast ne voie jamais le nom
        text, n = perso.build_segment(self.racine, self.cfg["perso"], date(2026, 10, 13))
        self.assertEqual(n, 1)
        self.assertIn("Dans 3 jours : rendre mon dossier de montage.", text)
        text, _ = perso.build_segment(self.racine, self.cfg["perso"], date(2027, 3, 5))
        self.assertIn("Karim", text)

    def test_unknown_chat_and_unsupported_content_are_ignored(self):
        self.text("coucou", chat=999)
        self.world._push_update({"sticker": {"file_id": "S"}})
        summary = self.run_once()
        self.assertEqual(summary["ignores"], 2)
        self.assertEqual((self.mem.entries(), self.world.tg_sent), ([], []))
        self.assertEqual(self.mem.load_state()["offset"], 503)  # mais ils sont bien « lus »

    def test_replayed_message_is_not_stored_twice(self):
        self.text(self.BIG)
        self.run_once()
        state = self.mem.load_state()
        state["offset"] = 0                    # la sauvegarde de l'état a été perdue : Telegram renvoie le message
        self.mem.save_state(state)
        before = len(self.messages())
        self.run_once()
        self.assertEqual(len(self.mem.entries()), 1)
        self.assertEqual(len(self.messages()), before)                   # pas de second accusé

    def test_failed_extraction_keeps_the_text_and_is_retried_later(self):
        self.text(self.BIG)
        self.world.groq_fn = lambda req, model, body: (500, "down")
        self.run_once()
        (entry,) = self.mem.entries()
        self.assertFalse(entry["extrait"])
        self.assertEqual(entry["essais_extraction"], 1)
        self.assertEqual(self.mem.facts(), [])
        self.assertIn("prochain passage", self.messages()[0])

        self.world.groq_fn = self.world.smart_groq
        self.run_once(now=self.now.replace(hour=15))
        (entry,) = self.mem.entries()
        self.assertTrue(entry["extrait"])
        self.assertEqual(len(self.mem.facts()), 3)
        self.assertIn("Infos retenues de ton message", self.messages()[-1])

    def test_sensitive_topics_are_kept_out_of_the_news_profile(self):
        self.text("L'an dernier j'ai fait une dépression, mais maintenant j'adore le montage vidéo tous les soirs.")
        self.run_once()
        self.assertEqual({e["sujet"] for e in self.mem.interests()["aime"]}, {"montage vidéo"})
        self.assertNotIn("dépression", (self.racine / "memoire/interets.yaml").read_text(encoding="utf-8"))
        self.assertTrue(any("dépression" in f["texte"] for f in self.mem.facts()))  # gardé en privé, dans les faits

    def test_birthday_can_be_corrected_by_saying_it_again(self):
        self.text("N'oublie pas l'anniversaire de Karim le 12 mars 2005, c'est important pour moi.")
        self.run_once()
        self.text("Non je me suis trompé, l'anniversaire de Karim le 21 mars, pas le douze, voilà.", ts=self.ts + 60)
        self.run_once()
        (birthday,) = self.mem.birthdays()
        self.assertEqual((birthday["jour"], birthday["mois"], birthday["annee"]), (21, 3, 2005))
        self.assertIn("(corrigé)", self.messages()[-1])

    def test_short_messages_are_stored_without_calling_the_ai(self):
        self.text("ok merci")
        self.run_once()
        (entry,) = self.mem.entries()
        self.assertTrue(entry["extrait"])
        self.assertEqual(self.world.calls("/groq/chat"), [])
        self.assertEqual(self.messages(), ["✅ Noté."])


class TestJournalVoice(JournalCase):
    @needs_ffmpeg
    def test_a_voice_note_is_downloaded_cut_transcribed_and_filed(self):
        self.world.transcripts = ["Aujourd'hui j'ai monté la séquence d'ouverture de mon court-métrage avec Houdini pour les effets."]
        self.voice(make_audio(5), duration=5)
        summary = self.run_once()
        self.assertEqual((summary["audio"], summary["entrees"], summary["en_file"]), (1, 1, 0))
        (entry,) = self.mem.entries()
        self.assertEqual((entry["type"], entry["duree_s"]), ("vocal", 5))
        self.assertIn("séquence d'ouverture", entry["texte"])
        self.assertTrue(entry["extrait"])
        self.assertIn("5 s de vocal", self.messages()[0])
        req = self.world.calls("/groq/audio")[0]
        self.assertEqual(req.headers["Authorization"], "Bearer q-key")
        self.assertEqual(req.multipart()["file"][0], "morceau-0000.ogg")
        self.assertEqual(self.world.calls("/gemini/"), [])
        self.assertEqual([p.name for p in self.work.rglob("morceau-*.ogg")], ["morceau-0000.ogg"])

    def test_without_ffmpeg_a_short_note_goes_straight_through(self):
        self.world.transcripts = ["Un petit vocal envoyé sans passer par ffmpeg du tout, juste pour voir."]
        self.voice(b"OggS" + b"\0" * 200, duration=8)
        with mock.patch.object(audio, "ensure_ffmpeg", return_value=None):
            self.run_once()
        self.assertEqual(self.world.stt_calls, 1)
        self.assertEqual(self.world.calls("/groq/audio")[0].multipart()["file"][0], "audio.oga")
        self.assertEqual(len(self.mem.entries()), 1)

    @needs_ffmpeg
    def test_long_recording_resumes_where_the_quota_stopped_it(self):
        self.cfg["journal"]["morceau_s"] = 10
        self.voice(make_audio(25), duration=25)
        count = []

        def stt_fn(req, fields):
            count.append(1)
            if len(count) == 2:
                return 429, "Rate limit reached. Please try again in 7m12s."
            return self.world.smart_stt(req, fields)

        self.world.stt_fn = stt_fn
        self.world.transcripts = ["Premier morceau du long enregistrement.", "Deuxième morceau où je parle de Houdini.", "Troisième et dernier morceau."]

        first = self.run_once()
        self.assertEqual((first["entrees"], first["en_file"]), (0, 1))
        (item,) = self.mem.load_state()["file"]
        self.assertEqual(item["textes"], ["Premier morceau du long enregistrement."])  # progression gardée
        self.assertEqual(self.mem.entries(), [])
        self.assertEqual(self.messages(), [])                                       # pas d'accusé tant que ce n'est pas fini

        second = self.run_once(now=self.now.replace(hour=15))
        self.assertEqual((second["entrees"], second["en_file"]), (1, 0))
        (entry,) = self.mem.entries()
        self.assertEqual(entry["texte"], "Premier morceau du long enregistrement. Deuxième morceau où je parle de Houdini. Troisième et dernier morceau.")
        self.assertEqual(len(count), 4)                                             # le 1er morceau n'est jamais retranscrit

    @needs_ffmpeg
    def test_hourly_audio_quota_is_respected_before_calling_groq(self):
        self.cfg["journal"]["audio_s_par_heure"] = 15
        self.world.transcripts = ["Premier vocal assez long pour être rangé sans problème dans la mémoire.",
                                  "Second vocal assez long pour être rangé sans problème dans la mémoire."]
        self.voice(make_audio(5))
        self.voice(make_audio(5), ts=self.ts + 1)
        first = self.run_once()
        self.assertEqual((first["entrees"], first["en_file"], self.world.stt_calls), (1, 1, 1))  # 10 s + 10 s > 15 s
        state = self.mem.load_state()
        state["stt_historique"] = []                                                               # une heure plus tard
        self.mem.save_state(state)
        second = self.run_once()
        self.assertEqual((second["entrees"], second["en_file"], self.world.stt_calls), (1, 0, 2))

    def test_deadline_leaves_the_queue_untouched(self):
        self.voice(b"OggS" + b"\0" * 200, duration=8)
        with mock.patch.object(audio, "ensure_ffmpeg", return_value=None):
            summary = self.run_once(deadline=time.time() - 1)
            self.assertEqual((summary["entrees"], summary["en_file"], self.world.stt_calls), (0, 1, 0))
            self.run_once()
        self.assertEqual(len(self.mem.entries()), 1)

    def test_silence_and_oversized_files_get_a_clear_message(self):
        self.world.transcripts = [""]
        self.voice(b"OggS" + b"\0" * 200, duration=8)
        self.voice(b"x", file_id="toobig1", ts=self.ts + 1)
        with mock.patch.object(audio, "ensure_ffmpeg", return_value=None):
            summary = self.run_once()
        self.assertEqual((summary["entrees"], summary["en_file"]), (0, 0))
        texts = " ".join(self.messages())
        self.assertIn("rien entendu", texts)
        self.assertIn("20 Mo", texts)

    def test_a_note_that_never_works_is_dropped_after_four_tries(self):
        self.world.stt_fn = lambda req, fields: (400, "could not process file")
        self.voice(b"OggS" + b"\0" * 200, duration=8)
        with mock.patch.object(audio, "ensure_ffmpeg", return_value=None):
            for hour in range(14, 17):
                self.assertEqual(self.run_once(now=self.now.replace(hour=hour))["en_file"], 1)
            self.assertEqual(self.run_once(now=self.now.replace(hour=17))["en_file"], 0)
        self.assertIn("Je l'abandonne", self.messages()[-1])

    def test_missing_groq_key_keeps_the_notes_for_later(self):
        self.voice(b"OggS" + b"\0" * 200, duration=8)
        self.keys = {"gemini": "g", "groq": ""}
        with mock.patch.object(audio, "ensure_ffmpeg", return_value=None):
            self.assertEqual(self.run_once()["en_file"], 1)
        self.assertEqual(self.world.stt_calls, 0)


class TestJournalInspi(JournalCase):
    def test_the_button_asks_a_spoken_question_and_the_answer_is_linked_to_it(self):
        self.text(telegram.BTN_INSPI)
        self.run_once()
        (voice,) = self.world.sent("sendVoice")
        self.assertIn("raconte-moi qui tu es", voice["caption"])
        self.assertEqual((voice["bytes"], voice["chat_id"]), (6003, "4242"))
        self.assertEqual(self.world.sent("sendMessage"), [])
        (question,) = self.mem.questions()
        self.assertEqual((question["banque_id"], question["message_id"], question["repondue_par"]), ("b01", voice["message_id"], None))
        self.assertEqual(self.mem.load_state()["question_en_attente"]["id"], question["id"])

        # réponse en « répondant » au message de la question
        self.text("Je m'appelle Léo, j'ai vingt ans et j'étudie pour devenir chef opérateur à Paris.", reply_to=voice["message_id"])
        self.run_once()
        (answer,) = self.mem.entries()
        self.assertEqual(answer["repond_a"], question["id"])
        self.assertEqual(self.mem.questions()[0]["repondue_par"], answer["id"])
        self.assertIsNone(self.mem.load_state()["question_en_attente"])
        self.assertIn("Réponse à la question", (self.racine / "memoire/journal/2026/2026-10-04.md").read_text(encoding="utf-8"))

        # la question suivante est une autre
        self.text("/inspi")
        self.run_once()
        self.assertEqual([q["banque_id"] for q in self.mem.questions()], ["b01", "b02"])

    def test_a_plain_message_after_a_question_also_answers_it(self):
        self.text("/inspi")
        self.run_once()
        self.text("Alors en ce moment je travaille sur un court-métrage d'école en super 16, c'est génial.")
        self.run_once()
        (answer,) = self.mem.entries()
        self.assertEqual(answer["repond_a"], self.mem.questions()[0]["id"])

    def test_old_unanswered_question_is_not_linked_to_a_new_message(self):
        self.text("/inspi")
        self.run_once()
        self.text("Un message beaucoup plus tard sans rapport avec la question du bot, rien de spécial.")
        self.run_once(now=self.now.replace(day=6))
        (entry,) = self.mem.entries()
        self.assertIsNone(entry["repond_a"])

    def test_pressing_the_button_twice_in_a_row_asks_one_question(self):
        self.text("/inspi")
        self.text("💡 Inspi")
        self.run_once()
        self.assertEqual(len(self.mem.questions()), 1)

    def test_evening_question_comes_once_a_day_between_the_set_hour_and_22h(self):
        self.run_once()
        self.assertEqual(self.world.sent("sendVoice"), [])                                  # 14 h : rien
        self.run_once(now=self.now.replace(hour=19, minute=30))
        self.assertEqual(len(self.world.sent("sendVoice")), 1)
        self.run_once(now=self.now.replace(hour=20, minute=30))
        self.assertEqual(len(self.world.sent("sendVoice")), 1)                              # déjà posée ce soir
        self.run_once(now=self.now.replace(day=5, hour=23))
        self.assertEqual(len(self.world.sent("sendVoice")), 1)                              # trop tard
        self.run_once(now=self.now.replace(day=5, hour=19, minute=10))
        self.assertEqual(len(self.world.sent("sendVoice")), 2)                              # le lendemain
        first, second = self.mem.questions()
        self.assertTrue(first.get("ignoree"))                                               # restée sans réponse
        self.assertNotEqual(first["texte"], second["texte"])

    def test_evening_question_can_be_switched_off(self):
        self.cfg["journal"]["heure_question"] = None
        self.run_once(now=self.now.replace(hour=20))
        self.assertEqual(self.world.sent(), [])

    def test_no_voice_available_falls_back_to_text(self):
        with mock.patch.object(tts, "synthesize", side_effect=RuntimeError("edge en panne")):
            self.text("/inspi")
            self.run_once()
        self.assertEqual(self.world.sent("sendVoice"), [])
        self.assertTrue(self.messages()[0].startswith("💡 "))
        self.assertEqual(len(self.mem.questions()), 1)

    def test_voice_upload_failure_falls_back_to_text_and_text_failure_retries_next_time(self):
        self.world.tg_fail["sendVoice"] = 500
        self.text("/inspi")
        self.run_once()
        self.assertEqual(len(self.world.sent("sendVoice")), 0)
        self.assertTrue(self.messages()[0].startswith("💡 "))
        self.assertEqual(len(self.mem.questions()), 1)
        self.world.tg_fail.clear()
        self.world.tg_fail["sendMessage"] = 500
        self.cfg["journal"]["question_vocale"] = False
        self.run_once(now=self.now.replace(hour=20))                       # question du soir
        self.assertEqual(len(self.mem.questions()), 1)                      # rien n'est noté si rien n'est parti
        self.assertEqual(self.mem.load_state()["derniere_question_du_soir"], "")
        self.world.tg_fail.clear()
        self.run_once(now=self.now.replace(hour=20, minute=30))
        self.assertEqual(len(self.mem.questions()), 2)

    def test_ai_questions_use_the_memory_once_it_is_rich_enough(self):
        for text in ("J'adore Houdini, c'est ma passion pour les effets en ce moment.",
                     "J'adore le montage vidéo, je passe mes soirées dessus en ce moment.",
                     "J'adore Minecraft et le modding le week-end avec mes amis proches."):
            self.text(text)
            self.run_once()
        self.assertGreaterEqual(len(self.mem.facts()), 3)
        for i in range(8):
            self.mem.add_facts([{"categorie": "projet", "texte": f"Fait de remplissage numéro {i}"}], "e", "2026-10-04")
        self.text("/inspi")
        self.run_once()
        (voice,) = self.world.sent("sendVoice")
        self.assertIn("Houdini", voice["caption"])
        self.assertEqual(self.mem.questions()[-1]["origine"], "ia")
        self.assertEqual(self.world.calls("/gemini/"), [])


class TestJournalCommands(JournalCase):
    def test_start_installs_buttons_and_menu(self):
        self.text("/start")
        self.run_once()
        (welcome,) = self.world.sent("sendMessage")
        for expected in ("Vocaux", "emploi du temps", "/reveil", "/consignes", "/podcast"):
            self.assertIn(expected, welcome["text"])
        rows = [[b["text"] for b in row] for row in welcome["markup"]["keyboard"]]
        self.assertEqual(rows, [[telegram.BTN_INSPI, telegram.BTN_MEMOIRE], [telegram.BTN_REVEIL, telegram.BTN_COURS]])
        (menu,) = self.world.sent("setMyCommands")
        self.assertEqual({c["command"] for c in menu["commands"]},
                         {"inspi", "reveil", "cours", "podcast", "consignes", "reglages", "memoire", "aide"})

    def test_unknown_command(self):
        self.text("/zzz")
        self.run_once()
        self.assertEqual(self.messages(), [journal.HELP_UNKNOWN])

    def test_memoire_says_where_the_news_interests_come_from(self):
        self.text(telegram.BTN_MEMOIRE)
        self.run_once()
        first = self.messages()[0]
        self.assertIn("PROVISOIRE", first)
        self.assertIn("Il me manque 3 centres d'intérêt", first)
        for text in ("J'adore Houdini, c'est ma passion pour les effets en ce moment.",
                     "J'adore le montage vidéo, je passe mes soirées dessus en ce moment.",
                     "J'adore Minecraft et le modding le week-end avec mes amis proches."):
            self.text(text)
        self.run_once()
        self.text("/memoire")
        self.run_once()
        last = self.messages()[-1]
        self.assertIn("TA mémoire", last)
        self.assertIn("Houdini", last)
        self.assertIn("3 messages", last)

    def test_webhook_conflict_does_not_break_the_run(self):
        self.world.tg_conflict = True
        self.voice(b"OggS" + b"\0" * 200)  # un vocal déjà en file d'attente reste traitable
        state = self.mem.load_state()
        state["file"].append({"id": "mold", "file_id": "voice-old", "taille": 10, "duree": 8, "nom": "", "recu": self.ts,
                              "legende": "", "repond_a": None, "textes": [], "essais": 0, "erreur": ""})
        self.mem.save_state(state)
        self.world.tg_files["voice-old"] = b"OggS" + b"\0" * 200
        self.world.transcripts = ["Un ancien vocal resté dans la file avant le changement de mode, sans souci."]
        with mock.patch.object(audio, "ensure_ffmpeg", return_value=None):
            summary = self.run_once()
        self.assertEqual((summary["messages"], summary["entrees"]), (0, 1))


class TestFromJournalToPodcast(JournalCase):
    """Ce que veut Léo : les actus ne dépendent plus de ma liste, mais de ce que son journal a appris."""

    def test_news_profile_switches_to_the_memory_and_reaches_the_triage(self):
        self.use_feeds({
            "tech": ("tech", [
                {"title": "Houdini 21 arrive avec de nouveaux outils"},
                {"title": "Montage : une mise à jour majeure du logiciel"},
                {"title": "Minecraft ajoute une nouvelle fonction de modding"},
                {"title": "Un nouveau casque VR sort"},
            ]),
            "info": ("general", [{"title": "Grève des transports en Île-de-France demain"}]),
        })
        out = self.tmp / "out"

        # avant : liste provisoire de Claude
        main.generate(self.racine, self.cfg, out, keys=self.keys, now=self.now, no_audio=True)
        before = json.loads((out / "meta.json").read_text(encoding="utf-8"))
        self.assertEqual(before["tri"]["profil"], "provisoire")
        first_request = self.world.calls("/gemini/")[0].plain
        self.assertIn("réalité virtuelle", first_request)
        self.assertIn("PROVISOIRE", (out / "choix.txt").read_text(encoding="utf-8"))

        # le journal apprend trois passions
        for text in ("J'adore Houdini, c'est ma passion pour les effets en ce moment.",
                     "J'adore le montage vidéo, je passe mes soirées dessus en ce moment.",
                     "J'adore Minecraft et le modding le week-end avec mes amis proches."):
            self.text(text)
        self.run_once()
        self.world.requests.clear()

        # après : ta mémoire
        main.generate(self.racine, self.cfg, out, keys=self.keys, now=self.now, no_audio=True)
        after = json.loads((out / "meta.json").read_text(encoding="utf-8"))
        self.assertEqual(after["tri"]["profil"], "memoire")
        triage_request = next(r.plain for r in self.world.calls("/gemini/") if "rédacteur en chef" in r.plain)
        for learned in ("Houdini", "montage vidéo", "Minecraft"):
            self.assertIn(learned, triage_request)
        self.assertNotIn("réalité virtuelle", triage_request)       # la liste provisoire a disparu
        self.assertIn("mémoire (3 sujets appris)", (out / "choix.txt").read_text(encoding="utf-8"))
        script = (out / "latest.txt").read_text(encoding="utf-8")
        self.assertIn("Houdini", script)
        self.assertNotIn("casque VR", script)                       # hors de ses vrais centres d'intérêt
        # le contenu privé des messages n'a jamais touché Gemini
        gemini = self.world.bodies("/gemini/")
        self.assertNotIn("mes amis proches", gemini)
        self.assertNotIn("ma passion", gemini)


class TestPodcastToTelegram(JournalCase):
    def setUp(self):
        super().setUp()
        self.use_feeds({"tech": ("tech", [{"title": "Houdini 21 arrive"}, {"title": "Une découverte, bonne nouvelle"}])})
        self.out = self.tmp / "out"
        env = mock.patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": self.world.tg_token, "TELEGRAM_CHAT_ID": str(self.world.tg_chat)})
        env.start()
        self.addCleanup(env.stop)

    def generate(self, **kw):
        return main.generate(self.racine, self.cfg, self.out, keys=self.keys, now=self.now, no_audio=False, **kw)

    def test_the_morning_podcast_lands_in_the_chat(self):
        self.assertEqual(self.generate(), main.EXIT_OK)
        (voice,) = self.world.sent("sendVoice")
        self.assertEqual(voice["caption"], "Podcast du dimanche 4 octobre 2026")
        self.assertEqual(voice["filename"], "latest.mp3")
        self.assertTrue(json.loads((self.out / "meta.json").read_text(encoding="utf-8"))["telegram"])

    def test_a_fallback_podcast_is_labelled_as_such_in_the_chat(self):
        self.world.gemini_fn = lambda req, model, body: (500, "down")
        self.world.groq_fn = lambda req, model, body: (500, "down")
        self.assertEqual(self.generate(), main.EXIT_SECOURS)
        (voice,) = self.world.sent("sendVoice")
        self.assertIn("Podcast de secours du dimanche 4 octobre 2026", voice["caption"])

    def test_a_telegram_failure_never_breaks_the_podcast(self):
        self.world.tg_fail["sendVoice"] = 500
        self.assertEqual(self.generate(), main.EXIT_OK)
        meta = json.loads((self.out / "meta.json").read_text(encoding="utf-8"))
        self.assertFalse(meta["telegram"])
        self.assertTrue((self.out / "latest.mp3").exists())

    def test_nothing_is_sent_when_telegram_is_not_configured_or_switched_off(self):
        with mock.patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": ""}):
            self.assertEqual(self.generate(), main.EXIT_OK)
        self.assertNotIn("telegram", json.loads((self.out / "meta.json").read_text(encoding="utf-8")))
        self.cfg["telegram"]["envoyer_podcast"] = False
        self.assertEqual(self.generate(), main.EXIT_OK)
        self.assertEqual(self.world.sent("sendVoice"), [])
        self.assertEqual(self.generate(send_telegram=False), main.EXIT_OK)
        self.assertEqual(self.world.sent("sendVoice"), [])


class TestCli(JournalCase):
    def test_main_requires_telegram_and_groq_settings(self):
        with mock.patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": "", "GROQ_API_KEY": ""}, clear=False):
            with mock.patch("builtins.print") as printed:
                self.assertEqual(journal.main(["--racine", str(self.racine)]), 1)
            self.assertIn("TELEGRAM", printed.call_args[0][0])
        env = {"TELEGRAM_BOT_TOKEN": self.world.tg_token, "TELEGRAM_CHAT_ID": "4242", "GROQ_API_KEY": ""}
        with mock.patch.dict("os.environ", env):
            with mock.patch("builtins.print") as printed:
                self.assertEqual(journal.main(["--racine", str(self.racine)]), 1)
            self.assertIn("GROQ_API_KEY", printed.call_args[0][0])

    def test_main_fails_loudly_when_telegram_refuses_the_token_or_a_webhook_blocks(self):
        shutil.copy(ROOT_CONFIG, self.racine / "config.yaml")
        good = {"TELEGRAM_BOT_TOKEN": self.world.tg_token, "TELEGRAM_CHAT_ID": "4242", "GROQ_API_KEY": "q-key"}
        with mock.patch.dict("os.environ", {**good, "TELEGRAM_BOT_TOKEN": "999:WRONG"}), mock.patch("builtins.print") as printed:
            self.assertEqual(journal.main(["--racine", str(self.racine)]), 2)
        self.assertIn("TELEGRAM_BOT_TOKEN", printed.call_args[0][0])
        self.assertNotIn("999:WRONG", printed.call_args[0][0])         # le token n'apparaît jamais
        self.world.tg_conflict = True
        with mock.patch.dict("os.environ", good), mock.patch("builtins.print") as printed:
            self.assertEqual(journal.main(["--racine", str(self.racine)]), 2)
        self.assertIn("webhook", printed.call_args[0][0])
        self.world.tg_conflict = False
        with mock.patch.dict("os.environ", good):
            self.assertEqual(journal.main(["--racine", str(self.racine), "--no-send"]), 0)

    def test_main_runs_one_pass_and_the_force_flag_asks_a_question(self):
        shutil.copy(ROOT_CONFIG, self.racine / "config.yaml")
        env = {"TELEGRAM_BOT_TOKEN": self.world.tg_token, "TELEGRAM_CHAT_ID": "4242", "GROQ_API_KEY": "q-key"}
        with mock.patch.dict("os.environ", env):
            self.assertEqual(journal.main(["--racine", str(self.racine), "--inspi"]), 0)
        self.assertEqual(len(self.world.sent("sendVoice")), 1)
        with mock.patch.dict("os.environ", env), mock.patch("builtins.print"):
            self.assertEqual(journal.main(["--racine", str(self.racine), "--inspi", "--no-send"]), 0)
        self.assertEqual(len(self.world.sent("sendVoice")), 1)  # --no-send : rien de plus


ROOT_CONFIG = Path(__file__).resolve().parent.parent / "config.yaml"


class TestShippedJournalFiles(unittest.TestCase):
    def test_config_has_every_journal_setting(self):
        cfg = yaml.safe_load(ROOT_CONFIG.read_text(encoding="utf-8"))
        for key in ("heure_question", "question_vocale", "stt_modele", "langue", "morceau_s", "audio_s_par_heure",
                    "tpm", "fenetre_mots", "duree_max_min", "min_mots_extraction"):
            self.assertIn(key, cfg["journal"], key)
        self.assertTrue(cfg["telegram"]["envoyer_podcast"])
        self.assertTrue(llm.KEY_ENV["groq"])


if __name__ == "__main__":
    unittest.main()
