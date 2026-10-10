"""Tests hors ligne de la lecture d'images (emploi du temps, documents…) et de l'envoi d'images à l'IA.

Lancer :  python -m unittest discover -s tests -t . -v
Règle de confidentialité vérifiée ici : une image ne part JAMAIS vers Gemini, seulement vers Groq.
"""
from __future__ import annotations

import base64
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src import llm, vision
from tests.fakes import fake_jpeg, fake_png, timetable_reading
from tests.test_podcast import WorldCase

needs_ffmpeg = unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg absent")


def make_image(path: Path, size: str) -> Path:
    """Une vraie image (mire de test) créée avec ffmpeg."""
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=size={size}:rate=1",
                    "-frames:v", "1", str(path)], check=True)
    return path


# =========================================================================== préparer l'image
class TestSniff(unittest.TestCase):
    def test_known_formats(self):
        self.assertEqual(vision.sniff_mime(fake_jpeg()[:16]), "image/jpeg")
        self.assertEqual(vision.sniff_mime(fake_png()[:16]), "image/png")
        self.assertEqual(vision.sniff_mime(b"RIFF\x10\0\0\0WEBPVP8 "), "image/webp")

    def test_unknown_formats(self):
        for head in (b"", b"GIF89a", b"%PDF-1.7", b"\0\0\0\x18ftypheic", b"hello"):
            with self.subTest(head):
                self.assertIsNone(vision.sniff_mime(head))


class TestPrepare(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def file(self, name: str, data: bytes) -> Path:
        path = self.tmp / name
        path.write_bytes(data)
        return path

    def test_a_normal_image_is_sent_as_is(self):
        data = fake_jpeg("edt")
        self.assertEqual(vision.prepare_image(self.file("a.jpg", data)), ("image/jpeg", data))
        data = fake_png("edt")
        self.assertEqual(vision.prepare_image(self.file("a.bin", data)), ("image/png", data))     # reconnu au contenu, pas au nom

    @needs_ffmpeg
    def test_a_heavy_image_is_shrunk_to_a_jpeg_and_leaves_no_copy(self):
        path = make_image(self.tmp / "big.png", "3200x2000")
        self.assertGreater(path.stat().st_size, 400_000)
        mime, data = vision.prepare_image(path, max_bytes=150_000)
        self.assertEqual(mime, "image/jpeg")
        self.assertLessEqual(len(data), 150_000)
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), ["big.png"])                 # pas de « -prep.jpg » oublié

    @needs_ffmpeg
    def test_an_image_that_cannot_be_shrunk_enough_is_refused(self):
        path = make_image(self.tmp / "big.png", "3200x2000")
        with self.assertRaises(vision.ImageError) as ctx:
            vision.prepare_image(path, max_bytes=20_000)
        self.assertIn("trop lourde", str(ctx.exception))
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), ["big.png"])

    @needs_ffmpeg
    def test_an_unknown_format_ffmpeg_cannot_read_is_refused_clearly(self):
        with self.assertRaises(vision.ImageError) as ctx:
            vision.prepare_image(self.file("photo.heic", b"\0\0\0\x18ftypheic" + b"\0" * 300))
        self.assertIn("format d'image non pris en charge", str(ctx.exception))

    def test_the_conversion_gets_ffmpeg_the_same_way_audio_does(self):
        """Sur GitHub Actions, audio.ensure_ffmpeg() l'installe s'il manque : les images passent par lui aussi."""
        with mock.patch.object(vision.audio, "ensure_ffmpeg", return_value=None) as ensure:
            with self.assertRaises(vision.ImageError):
                vision.prepare_image(self.file("big.jpg", fake_jpeg() + b"\0" * 5000), max_bytes=1000)
        ensure.assert_called()

    def test_without_ffmpeg(self):
        with mock.patch("shutil.which", return_value=None):
            small = fake_jpeg()
            self.assertEqual(vision.prepare_image(self.file("a.jpg", small))[1], small)           # rien à convertir : ça passe
            with self.assertRaises(vision.ImageError) as heavy:
                vision.prepare_image(self.file("b.jpg", fake_jpeg() + b"\0" * 5000), max_bytes=1000)
            self.assertIn("trop lourde", str(heavy.exception))
            with self.assertRaises(vision.ImageError) as unknown:
                vision.prepare_image(self.file("c.heic", b"\0" * 300))
            self.assertIn("format", str(unknown.exception))


# =========================================================================== lire la réponse de l'IA
class TestParse(unittest.TestCase):
    def test_a_full_timetable_reply(self):
        reading = vision.parse("```json\n" + json.dumps(timetable_reading(week="2026-10-05"), ensure_ascii=False) + "\n```")
        self.assertEqual((reading.type, reading.semaine_du, reading.jours_vus), ("emploi_du_temps", "2026-10-05", ["lun", "mar", "mer", "jeu", "ven"]))
        self.assertEqual(len(reading.cours), 4)
        self.assertEqual(reading.cours[0]["intitule"], "Étalonnage")

    def test_the_type_is_recognised_loosely(self):
        for raw, kind in (("emploi_du_temps", "emploi_du_temps"), ("Emploi du temps", "emploi_du_temps"), ("EMPLOI", "emploi_du_temps"),
                          ("autre", "autre"), ("", "autre"), (None, "autre"), ("document", "autre")):
            with self.subTest(raw):
                self.assertEqual(vision.parse(json.dumps({"type": raw})).type, kind)

    def test_a_visible_chain_of_thought_is_ignored(self):
        reply = "<think>Voyons : lundi 9h…</think>\n" + json.dumps({"type": "autre", "description": "Une affiche", "texte": "Festival le 12 octobre"})
        reading = vision.parse(reply)
        self.assertEqual((reading.type, reading.description, reading.texte), ("autre", "Une affiche", "Festival le 12 octobre"))

    def test_text_around_the_json_is_ignored(self):
        self.assertEqual(vision.parse('Voici le résultat : {"type": "autre", "description": "ok"} Voilà.').description, "ok")

    def test_odd_fields_are_cleaned(self):
        reading = vision.parse(json.dumps({"type": "emploi_du_temps", "jours_vus": "lun", "cours": [{"jour": "lun"}, "x", 3, None, {"jour": "mar"}],
                                           "semaine_du": None, "description": None, "texte": None}))
        self.assertEqual((reading.jours_vus, reading.semaine_du, reading.description, reading.texte), ([], "", "", ""))
        self.assertEqual(reading.cours, [{"jour": "lun"}, {"jour": "mar"}])                     # seuls les objets sont gardés
        self.assertEqual(vision.parse(json.dumps({"cours": "rien"})).cours, [])

    def test_sizes_are_capped(self):
        reading = vision.parse(json.dumps({"description": "d" * 900, "texte": "t" * 9000, "jours_vus": list("abcdefghij"),
                                           "cours": [{"jour": "lun"}] * 500, "semaine_du": "s" * 100}))
        self.assertEqual((len(reading.description), len(reading.texte), len(reading.jours_vus), len(reading.cours), len(reading.semaine_du)),
                         (300, 2500, 7, 120, 40))

    def test_unreadable_replies_raise(self):
        for reply in ("", "pas du json", "[1, 2, 3]", '"texte"', "{cassé"):
            with self.subTest(reply):
                with self.assertRaises(ValueError):
                    vision.parse(reply)


# =========================================================================== l'appel à l'IA
class FakePacer:
    def __init__(self):
        self.asked: list[int] = []

    def wait(self, tokens: int) -> float:
        self.asked.append(tokens)
        return 0.0


class TestRead(WorldCase):
    def setUp(self):
        super().setUp()
        self.images = [("image/jpeg", fake_jpeg("page1"))]

    def read(self, images=None, **kw):
        kw.setdefault("caption", "")
        return vision.read_images(images if images is not None else self.images, now=self.now, cfg=self.cfg, keys=self.keys, **kw)

    def test_the_image_goes_to_groq_with_the_instructions_and_the_date(self):
        self.world.vision_reading = timetable_reading(week="2026-10-05")
        reading = self.read(caption="mon emploi du temps", prenom="Léo")
        self.assertEqual((reading.type, len(reading.cours), reading.semaine_du), ("emploi_du_temps", 4, "2026-10-05"))

        (call,) = self.world.vision_calls
        self.assertEqual(call["model"], self.cfg["llm"]["vision"][0]["modele"])
        self.assertEqual(call["images"], self.images)                                   # les octets arrivent intacts
        for expected in ("Léo", "Date du jour : dimanche 4 octobre 2026", "« mon emploi du temps »", "exactement ces clés", "Tu n'inventes RIEN"):
            self.assertIn(expected, call["text"])
        self.assertNotIn("<<", call["text"])                                           # tous les marqueurs sont remplacés
        (req,) = self.world.calls("/groq/")
        self.assertEqual(req.headers.get("Authorization"), "Bearer q-key")
        self.assertEqual(req.json["response_format"], {"type": "json_object"})
        self.assertEqual(req.json["reasoning_effort"], "low")                          # les réglages du modèle (config.yaml) suivent
        self.assertEqual(req.json["temperature"], 0.1)

    def test_several_images_are_all_sent_in_order(self):
        images = [("image/jpeg", fake_jpeg("a")), ("image/png", fake_png("b")), ("image/jpeg", fake_jpeg("c"))]
        self.read(images)
        self.assertEqual(self.world.vision_calls[0]["images"], images)

    def test_an_image_never_goes_to_gemini_even_if_the_chain_lists_it_first(self):
        self.cfg["llm"]["vision"] = [{"fournisseur": "gemini", "modele": "gemini-3-flash-preview"},
                                     {"fournisseur": "groq", "modele": "qwen/qwen3.8-27b"}]
        self.read()
        self.assertEqual(self.world.calls("/gemini/"), [])
        self.assertEqual(len(self.world.vision_calls), 1)

    def test_a_chain_with_only_gemini_fails_without_sending_anything(self):
        self.cfg["llm"]["vision"] = [{"fournisseur": "gemini", "modele": "gemini-3-flash-preview"}]
        with self.assertRaises(llm.LLMError) as ctx:
            self.read()
        self.assertIn("ne partent que vers Groq", str(ctx.exception))
        self.assertEqual(self.world.requests, [])

    def test_no_groq_key_means_no_call(self):
        self.keys["groq"] = ""
        with self.assertRaises(llm.LLMError) as ctx:
            self.read()
        self.assertIn("GROQ_API_KEY", str(ctx.exception))
        self.assertEqual(self.world.requests, [])

    def test_the_next_model_of_the_chain_takes_over(self):
        self.world.vision_fn = lambda req, model, body, text, images: (
            (503, "overloaded") if model == self.cfg["llm"]["vision"][0]["modele"] and "reasoning_format" in body
            else self.world.smart_vision(req, model, body, text, images))
        self.world.vision_reading = timetable_reading()
        self.assertEqual(self.read().type, "emploi_du_temps")
        self.assertGreater(len(self.world.vision_calls), 1)

    def test_an_unreadable_reply_gets_a_second_chance(self):
        replies = iter(["désolé, voilà : pas de json", json.dumps(timetable_reading())])
        self.world.vision_reading = lambda text, images: next(replies)
        self.assertEqual(self.read().type, "emploi_du_temps")
        self.assertEqual(len(self.world.vision_calls), 2)

    def test_two_unreadable_replies_raise(self):
        self.world.vision_reading = "toujours pas du json"
        with self.assertRaises(llm.LLMError) as ctx:
            self.read()
        self.assertIn("illisible", str(ctx.exception))
        self.assertEqual(len(self.world.vision_calls), 2)

    def test_a_reasoning_block_in_the_reply_is_dropped(self):
        self.world.vision_reading = "<think>Je regarde la grille…</think>" + json.dumps(timetable_reading())
        self.assertEqual(len(self.read().cours), 4)

    def test_the_pacer_is_told_that_images_are_expensive(self):
        pacer = FakePacer()
        self.read([("image/jpeg", fake_jpeg("a")), ("image/jpeg", fake_jpeg("b"))], pacer=pacer)
        (asked,) = pacer.asked
        self.assertGreaterEqual(asked, 2 * vision.IMAGE_TOKENS + vision.OUTPUT_TOKENS)

    def test_written_timetables_use_the_private_chain_and_send_no_image(self):
        self.world.groq_fn = lambda req, model, body: (200, json.dumps(timetable_reading()))      # message sans image : texte seul
        reading = vision.read_text("lundi 9h-12h étalonnage salle 204 ; mardi 14h-17h histoire du cinéma",
                                   now=self.now, cfg=self.cfg, keys=self.keys, prenom="Léo")
        self.assertEqual(len(reading.cours), 4)
        self.assertEqual(self.world.vision_calls, [])                                   # pas de message à images
        (req,) = self.world.calls("/groq/")
        roles = [m["role"] for m in req.json["messages"]]
        self.assertEqual(roles, ["system", "user"])
        self.assertIn("étalonnage salle 204", req.json["messages"][1]["content"])
        self.assertIn("Tu lis le texte envoyé", req.json["messages"][0]["content"])
        self.assertEqual(self.world.calls("/gemini/"), [])


class TestLLMWithImages(WorldCase):
    def chain(self, *pairs, extra=None):
        return [{"fournisseur": p, "modele": m, **({"extra": extra} if extra else {})} for p, m in pairs]

    def test_request_shape_for_groq(self):
        jpeg, png = fake_jpeg("un"), fake_png("deux")
        self.world.vision_reading = {"ok": True}
        text, label = llm.generate("Lis ça.", system="Tu lis des images.", chain=self.chain(("groq", "qwen/x")), keys=self.keys,
                                   json_mode=True, images=[("image/jpeg", jpeg), ("image/png", png)])
        self.assertEqual(label, "groq/qwen/x")
        self.assertEqual(json.loads(text), {"ok": True})
        (req,) = self.world.calls("/groq/")
        (message,) = req.json["messages"]                                               # un seul message : tout est dans « user »
        self.assertEqual(message["role"], "user")
        kinds = [part["type"] for part in message["content"]]
        self.assertEqual(kinds, ["text", "image_url", "image_url"])
        self.assertIn("Tu lis des images.", message["content"][0]["text"])
        self.assertIn("Lis ça.", message["content"][0]["text"])
        self.assertEqual(message["content"][1]["image_url"]["url"], "data:image/jpeg;base64," + base64.b64encode(jpeg).decode())
        self.assertEqual(message["content"][2]["image_url"]["url"], "data:image/png;base64," + base64.b64encode(png).decode())

    def test_without_images_the_request_keeps_its_system_message(self):
        self.world.groq_fn = lambda req, model, body: (200, "ok")
        llm.generate("p", system="s", chain=self.chain(("groq", "x")), keys=self.keys)
        (req,) = self.world.calls("/groq/")
        self.assertEqual([m["role"] for m in req.json["messages"]], ["system", "user"])

    def test_gemini_entries_are_skipped_with_an_explanation(self):
        self.world.vision_reading = {"ok": True}
        chain = self.chain(("gemini", "g1"), ("gemini", "g2"), ("groq", "x"))
        _text, label = llm.generate("p", system="s", chain=chain, keys=self.keys, images=[("image/jpeg", fake_jpeg())])
        self.assertEqual(label, "groq/x")
        self.assertEqual(self.world.calls("/gemini/"), [])
        with self.assertRaises(llm.LLMError) as ctx:
            llm.generate("p", system="s", chain=self.chain(("gemini", "g1")), keys=self.keys, images=[("image/jpeg", fake_jpeg())])
        self.assertIn("images ne partent que vers Groq", str(ctx.exception))

    def test_the_same_chain_still_serves_gemini_when_there_is_no_image(self):
        self.world.gemini_fn = lambda req, model, body: (200, "texte de gemini")
        text, label = llm.generate("p", system="s", chain=self.chain(("gemini", "g1"), ("groq", "x")), keys=self.keys)
        self.assertEqual((text, label), ("texte de gemini", "gemini/g1"))

    def test_a_setting_the_model_refuses_is_dropped_and_the_call_retried(self):
        def fn(req, model, body, text, images):
            if "reasoning_format" in body:
                return 400, json.dumps({"error": {"message": "`reasoning_format` is not supported with this model"}})
            return 200, '{"ok": true}'

        self.world.vision_fn = lambda req, model, body, text, images: fn(req, model, body, text, images)
        chain = self.chain(("groq", "qwen/x"), extra={"reasoning_format": "hidden", "max_completion_tokens": 500})
        text, _label = llm.generate("p", system="s", chain=chain, keys=self.keys, images=[("image/jpeg", fake_jpeg())])
        self.assertEqual(text, '{"ok": true}')
        first, second = self.world.calls("/groq/")
        self.assertIn("reasoning_format", first.json)
        self.assertNotIn("reasoning_format", second.json)
        self.assertNotIn("max_completion_tokens", second.json)                          # tous les réglages propres au modèle sautent
        self.assertEqual(len(second.json["messages"][0]["content"]), 2)                # …mais l'image est bien renvoyée

    def test_thinking_blocks_are_removed_from_groq_text(self):
        self.world.groq_fn = lambda req, model, body: (200, "<think>réflexion</think>Voici.")
        text, _label = llm.generate("p", system="s", chain=self.chain(("groq", "x")), keys=self.keys)
        self.assertEqual(text, "Voici.")

    def test_a_reply_that_is_only_an_unfinished_thought_counts_as_empty(self):
        self.world.groq_fn = lambda req, model, body: (200, "<think>réflexion interminable qui n'est jamais fermée")
        with self.assertRaises(llm.LLMError) as ctx:                                   # rien à lire : on passe au modèle suivant
            llm.generate("p", system="s", chain=self.chain(("groq", "x")), keys=self.keys)
        self.assertIn("réponse vide", str(ctx.exception))

    def test_data_url(self):
        self.assertEqual(llm.data_url("image/png", b"\x89PNG"), "data:image/png;base64,iVBORw==")


if __name__ == "__main__":
    unittest.main()
