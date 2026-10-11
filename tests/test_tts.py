"""Tests hors ligne de la synthèse vocale : Gemini TTS (faux serveur local), voix de secours Edge, essai des voix.

Lancer :  python -m unittest tests.test_tts -v
Les tests qui fabriquent de vrais mp3 demandent ffmpeg (ils sont sautés sans lui).
"""
from __future__ import annotations

import base64
import io
import json
import os
import shutil
import unittest
import wave
from pathlib import Path
from unittest import mock

from src import audio, reglages, telegram, tts
from tests.test_podcast import WorldCase

needs_ffmpeg = unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg absent")
KEY = "AIza-cle-secrete-de-test"


class TtsCase(WorldCase):
    def setUp(self):
        super().setUp()
        for target, attr, value in [
            (tts, "INTERACTIONS_URL", self.world.base + "/gemini-tts/interactions"),
            (tts, "LEGACY_URL", self.world.base + "/gemini-tts/legacy/{model}:generateContent"),
            (telegram, "API_ROOT", self.world.base + "/telegram"),
        ]:
            patcher = mock.patch.object(target, attr, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        env = mock.patch.dict(os.environ, {"GEMINI_API_KEY": KEY}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        self.out = self.tmp / "voix.mp3"
        self.edge_calls: list[tuple] = []
        edge = mock.patch.object(tts, "_edge", side_effect=self.fake_edge)
        edge.start()
        self.addCleanup(edge.stop)
        self.cfg_voix = {"moteur": "gemini", "edge_voix": "fr-FR-HenriNeural", "vitesse": "+0%",
                         "gemini": {"voix": "Sulafat", "delai_max_s": 60}}

    def fake_edge(self, text, cfg, out_path, retries=3):
        self.edge_calls.append((text, dict(cfg)))
        Path(out_path).write_bytes(b"ID3" + b"\0" * 6000)
        return cfg.get("edge_voix", "fr-FR-HenriNeural")

    def tts_requests(self):
        return self.world.calls("/gemini-tts/")

    def duration(self, path=None) -> float:
        return audio.probe_duration(Path(path or self.out)) or 0.0


@needs_ffmpeg
class TestGemini(TtsCase):
    def test_request_has_the_documented_shape(self):
        voice = tts.synthesize("Bonjour, ceci est un test.", self.cfg_voix, self.out)
        self.assertEqual(voice, "gemini:Sulafat")
        self.assertEqual(self.edge_calls, [])
        (req,) = self.tts_requests()
        self.assertEqual(req.headers.get("x-goog-api-key") or req.headers.get("X-Goog-Api-Key"), KEY)
        body = req.json
        self.assertEqual(body["model"], "gemini-3.8-flash-tts")
        item = body["input"][0]["content"][0]
        self.assertEqual((body["input"][0]["type"], item["type"], item["text"]), ("user_input", "text", "Bonjour, ceci est un test."))
        self.assertEqual(item["annotations"][0]["type"], "speech_metadata")
        self.assertIn("warm", item["annotations"][0]["style"])
        self.assertEqual(body["response_format"], {"type": "audio"})
        self.assertEqual(body["generation_config"], {"speech_config": [{"voice": "Sulafat"}]})
        self.assertNotIn(KEY, req.text)                                       # la clé voyage dans l'en-tête, jamais dans le corps
        self.assertEqual(tts.last_note(), "")

    def test_the_mp3_is_real_and_has_no_leftovers(self):
        tts.synthesize("Bonjour.", self.cfg_voix, self.out)
        self.assertAlmostEqual(self.duration(), 1.0, delta=0.25)
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir() if p.name.startswith("voix")), ["voix.mp3"])

    def test_chosen_voice_style_and_models_come_from_the_config(self):
        cfg = {**self.cfg_voix, "gemini": {"voix": "Achird", "style": "slow and serious", "modeles": ["gemini-3.8-flash-lite-tts"]}}
        self.assertEqual(tts.synthesize("Salut.", cfg, self.out), "gemini:Achird")
        body = self.tts_requests()[0].json
        self.assertEqual(body["model"], "gemini-3.8-flash-lite-tts")
        self.assertEqual(body["generation_config"]["speech_config"], [{"voix": "x"}][0:0] or [{"voice": "Achird"}])
        self.assertEqual(body["input"][0]["content"][0]["annotations"][0]["style"], "slow and serious")

    def test_an_empty_style_sends_no_annotation(self):
        cfg = {**self.cfg_voix, "gemini": {"voix": "Kore", "style": ""}}
        tts.synthesize("Salut.", cfg, self.out)
        self.assertNotIn("annotations", self.tts_requests()[0].json["input"][0]["content"][0])

    def test_custom_voice_id_is_sent_as_is(self):
        cfg = {**self.cfg_voix, "gemini": {"voix": "voice_ab12-xy"}}
        self.assertEqual(tts.synthesize("Salut.", cfg, self.out), "gemini:voice_ab12-xy")
        self.assertEqual(self.tts_requests()[0].json["generation_config"]["speech_config"], [{"voice": "voice_ab12-xy"}])

    def test_speed_changes_the_length(self):
        tts.synthesize("Un.", self.cfg_voix, self.out)
        normal = self.duration()
        fast = self.tmp / "rapide.mp3"
        tts.synthesize("Un.", {**self.cfg_voix, "vitesse": "+25%"}, fast)
        slow = self.tmp / "lent.mp3"
        tts.synthesize("Un.", {**self.cfg_voix, "vitesse": "-20%"}, slow)
        self.assertLess(self.duration(fast), normal * 0.9)
        self.assertGreater(self.duration(slow), normal * 1.15)

    def test_long_text_is_said_in_several_pieces_and_glued(self):
        sentence = "Voici une phrase de test assez longue pour remplir le morceau. "
        text = sentence * 12
        cfg = {**self.cfg_voix, "gemini": {**self.cfg_voix["gemini"], "taille_morceau": 300}}
        tts.synthesize(text, cfg, self.out)
        sent = [r.json["input"][0]["content"][0]["text"] for r in self.tts_requests()]
        self.assertGreater(len(sent), 2)
        self.assertTrue(all(len(t) <= 300 for t in sent))
        self.assertEqual(" ".join(sent), " ".join(text.split()))              # rien perdu, rien ajouté
        self.assertAlmostEqual(self.duration(), len(sent) * 1.0, delta=0.6)

    def test_rate_limit_then_success_retries_the_same_model(self):
        answers = [(429, "quota"), (503, "surcharge")]
        smart = self.world.smart_tts
        self.world.tts_fn = lambda req, kind, body: answers.pop(0) if answers else smart(req, kind, body)
        self.assertEqual(tts.synthesize("Salut.", self.cfg_voix, self.out), "gemini:Sulafat")
        models = [r.json["model"] for r in self.tts_requests()]
        self.assertEqual(models, ["gemini-3.8-flash-tts"] * 3)
        self.assertEqual(self.edge_calls, [])

    def test_when_the_first_model_keeps_failing_the_second_one_is_tried(self):
        smart = self.world.smart_tts
        self.world.tts_fn = lambda req, kind, body: (503, "surcharge") if body.get("model") == "gemini-3.8-flash-tts" else smart(req, kind, body)
        cfg = {**self.cfg_voix, "gemini": {**self.cfg_voix["gemini"], "delai_max_s": 8}}
        self.assertEqual(tts.synthesize("Salut.", cfg, self.out), "gemini:Sulafat")
        self.assertEqual(self.tts_requests()[-1].json["model"], "gemini-3.8-flash-lite-tts")

    def test_a_dead_gemini_falls_back_to_edge_and_says_why(self):
        self.world.tts_fn = lambda req, kind, body: (429, "You exceeded your current quota")
        cfg = {**self.cfg_voix, "gemini": {**self.cfg_voix["gemini"], "delai_max_s": 5}}
        self.assertEqual(tts.synthesize("Salut.", cfg, self.out), "fr-FR-HenriNeural")
        self.assertEqual(len(self.edge_calls), 1)
        self.assertIn("quota", tts.last_note())
        self.assertIn("voix Edge", tts.last_note())
        self.assertNotIn(KEY, tts.last_note())
        self.assertTrue(self.out.exists())

    def test_a_refused_key_is_not_retried_and_falls_back(self):
        self.world.tts_fn = lambda req, kind, body: (403, "API key not valid")
        self.assertEqual(tts.synthesize("Salut.", self.cfg_voix, self.out), "fr-FR-HenriNeural")
        self.assertEqual(len(self.tts_requests()), 1)
        self.assertIn("403", tts.last_note())

    def test_a_reply_without_audio_falls_back(self):
        self.world.tts_fn = lambda req, kind, body: (200, {"steps": [{"type": "model_output", "content": [{"type": "text", "text": "non"}]}]})
        cfg = {**self.cfg_voix, "gemini": {**self.cfg_voix["gemini"], "delai_max_s": 5}}
        self.assertEqual(tts.synthesize("Salut.", cfg, self.out), "fr-FR-HenriNeural")
        self.assertIn("sans audio", tts.last_note())

    def test_corrupt_audio_falls_back(self):
        bad = {"steps": [{"type": "model_output", "content": [{"type": "audio", "mime_type": "audio/wav", "data": "%%%pas-du-base64%%%"}]}]}
        self.world.tts_fn = lambda req, kind, body: (200, bad)
        self.assertEqual(tts.synthesize("Salut.", self.cfg_voix, self.out), "fr-FR-HenriNeural")
        self.assertIn("illisible", tts.last_note())

    def test_when_the_interactions_api_is_refused_the_old_form_is_tried_once(self):
        smart = self.world.smart_tts
        self.world.tts_fn = lambda req, kind, body: (404, "not found") if kind == "interactions" else smart(req, kind, body)
        self.assertEqual(tts.synthesize("Salut.", self.cfg_voix, self.out), "gemini:Sulafat")
        kinds = [("legacy" if "/legacy/" in r.path else "interactions") for r in self.tts_requests()]
        self.assertEqual(kinds, ["interactions", "legacy"])
        legacy = self.tts_requests()[1].json
        self.assertEqual(legacy["generationConfig"]["responseModalities"], ["AUDIO"])
        self.assertEqual(legacy["generationConfig"]["speechConfig"]["voiceConfig"]["prebuiltVoiceConfig"]["voiceName"], "Sulafat")
        self.assertIn("Salut.", legacy["contents"][0]["parts"][0]["text"])
        self.assertAlmostEqual(self.duration(), 1.0, delta=0.25)             # le PCM brut a été enveloppé en WAV

    def test_a_bad_voice_name_does_not_trigger_the_old_form(self):
        self.world.tts_fn = lambda req, kind, body: (400, "Invalid voice name")
        cfg = {**self.cfg_voix, "gemini": {**self.cfg_voix["gemini"], "voix": "voice_nexiste_pas"}}
        self.assertEqual(tts.synthesize("Salut.", cfg, self.out), "fr-FR-HenriNeural")
        self.assertTrue(all("/legacy/" not in r.path for r in self.tts_requests()))
        self.assertIn("voice", tts.last_note().lower())


class TestEngineChoice(TtsCase):
    def test_private_text_never_reaches_gemini(self):
        self.assertEqual(tts.synthesize("Question tirée de ton journal.", self.cfg_voix, self.out, private=True), "fr-FR-HenriNeural")
        self.assertEqual(self.tts_requests(), [])
        self.assertEqual(tts.last_note(), "")

    def test_no_key_means_edge_with_a_note(self):
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "  "}):
            self.assertEqual(tts.synthesize("Salut.", self.cfg_voix, self.out), "fr-FR-HenriNeural")
        self.assertEqual(self.tts_requests(), [])
        self.assertIn("GEMINI_API_KEY", tts.last_note())

    def test_edge_engine_never_calls_gemini_and_has_no_note(self):
        self.assertEqual(tts.synthesize("Salut.", {**self.cfg_voix, "moteur": "edge"}, self.out), "fr-FR-HenriNeural")
        self.assertEqual(self.tts_requests(), [])
        self.assertEqual(tts.last_note(), "")

    def test_default_engine_is_edge_and_unknown_engine_is_refused(self):
        self.assertEqual(tts.synthesize("Salut.", {"edge_voix": "fr-FR-DeniseNeural"}, self.out), "fr-FR-DeniseNeural")
        with self.assertRaises(ValueError):
            tts.synthesize("Salut.", {"moteur": "xtts"}, self.out)

    def test_the_note_is_reset_on_every_call(self):
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": ""}):
            tts.synthesize("Salut.", self.cfg_voix, self.out)
        self.assertTrue(tts.last_note())
        tts.synthesize("Salut.", {"moteur": "edge"}, self.out)
        self.assertEqual(tts.last_note(), "")


class TestHelpers(unittest.TestCase):
    def test_split_text(self):
        self.assertEqual(tts._split_text("Court.", 100), ["Court."])
        parts = tts._split_text("Une phrase. Une autre ! Et encore une ? Fin…", 20)
        self.assertEqual(" ".join(parts), "Une phrase. Une autre ! Et encore une ? Fin…")
        self.assertTrue(all(len(p) <= 20 for p in parts))
        monster = "mot " * 200
        self.assertTrue(all(len(p) <= 50 for p in tts._split_text(monster, 50)))
        self.assertEqual(" ".join(tts._split_text(monster, 50)), monster.strip())
        self.assertEqual(tts._split_text("x" * 130, 50), ["x" * 50, "x" * 50, "x" * 30])   # aucun espace : coupe sèche

    def test_speed_factor(self):
        self.assertEqual(tts._speed_factor("+0%"), 1.0)
        self.assertAlmostEqual(tts._speed_factor("+10%"), 1.10)
        self.assertAlmostEqual(tts._speed_factor("-15%"), 0.85)
        self.assertEqual(tts._speed_factor("n'importe quoi"), 1.0)
        self.assertEqual(tts._speed_factor("+99%"), 1.4)

    def test_raw_pcm_becomes_a_wav(self):
        raw = b"\x01\x00" * 2400
        wav = tts._wav_from_audio(raw, "audio/L16;codec=pcm;rate=16000")
        with wave.open(io.BytesIO(wav)) as w:
            self.assertEqual((w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()), (1, 2, 16000, 2400))
        self.assertEqual(tts._wav_from_audio(wav), wav)                          # déjà un WAV : inchangé

    def test_join_refuses_pieces_of_different_formats(self):
        def mk(rate):
            buf = io.BytesIO()
            with wave.open(buf, "wb") as w:
                w.setnchannels(1), w.setsampwidth(2), w.setframerate(rate)
                w.writeframes(b"\0\0" * 100)
            return buf.getvalue()

        with self.assertRaises(tts.GeminiTTSError):
            tts._join_wavs([mk(24000), mk(16000)])

    def test_audio_is_taken_from_the_last_audio_block_of_model_output(self):
        payload = {"steps": [
            {"type": "model_output", "content": [{"type": "audio", "data": "AAAA", "mime_type": "audio/wav"}]},
            {"type": "model_output", "content": [{"type": "audio", "data": "BBBB", "mime_type": "audio/wav"}]},
        ]}
        self.assertEqual(tts._find_audio_interactions(payload), ("BBBB", "audio/wav"))
        self.assertIsNone(tts._find_audio_interactions({"steps": []}))
        self.assertIsNone(tts._find_audio_interactions({}))


@needs_ffmpeg
class TestEssai(TtsCase):
    def run_essai(self, voix="", env=None):
        args = mock.Mock(racine=str(Path(__file__).resolve().parents[1]), voix=voix)
        base = {"TELEGRAM_BOT_TOKEN": self.world.tg_token, "TELEGRAM_CHAT_ID": str(self.world.tg_chat), "ESSAI_VOIX": ""}
        with mock.patch.dict(os.environ, {**base, **(env or {})}):
            return tts._essai(args)

    def test_default_selection_sends_one_voice_message_per_voice_plus_a_summary(self):
        code = self.run_essai()
        self.assertEqual(code, 0)
        captions = [s["caption"] for s in self.world.sent("sendVoice")]
        self.assertEqual(len(self.tts_requests()), 6)
        gem = [c for c in captions if c.startswith("🎙️") and "/voix " in c]
        self.assertEqual(len(gem), 6)
        self.assertIn("/voix sulafat", gem[0])
        self.assertTrue(any("Edge" in c for c in captions))                   # la voix de comparaison
        self.assertIn("Essai des voix terminé", self.world.sent("sendMessage")[-1]["text"])

    def test_chosen_voices_come_from_the_input_and_unknown_names_are_ignored(self):
        self.run_essai(env={"ESSAI_VOIX": "puck  ZEPHYR n'importe-quoi ; puck"})
        voices = [r.json["generation_config"]["speech_config"][0]["voice"] for r in self.tts_requests()]
        self.assertEqual(voices, ["Puck", "Zephyr"])

    def test_no_key_tells_the_user_what_to_do(self):
        code = self.run_essai(env={"GEMINI_API_KEY": ""})
        self.assertEqual(code, 1)
        self.assertEqual(self.tts_requests(), [])
        texts = " ".join(s["text"] for s in self.world.sent("sendMessage"))
        self.assertIn("GEMINI_API_KEY", texts)
        self.assertNotIn(KEY, texts)

    def test_quota_exhausted_is_explained_without_leaking_anything(self):
        self.world.tts_fn = lambda req, kind, body: (429, "You exceeded your current quota")
        code = self.run_essai(voix="Sulafat Kore")
        self.assertEqual(code, 1)
        text = self.world.sent("sendMessage")[-1]["text"]
        self.assertIn("quota", text)
        self.assertIn("aucune voix Gemini", text)
        self.assertNotIn(KEY, text)

    def test_partial_success_is_reported(self):
        smart = self.world.smart_tts
        self.world.tts_fn = lambda req, kind, body: (400, "Invalid voice name") if body["generation_config"]["speech_config"][0]["voice"] == "Kore" else smart(req, kind, body)
        self.assertEqual(self.run_essai(voix="Sulafat Kore"), 0)
        text = self.world.sent("sendMessage")[-1]["text"]
        self.assertIn("✅ Sulafat", text)
        self.assertIn("❌ Kore", text)


class TestVoiceCatalog(unittest.TestCase):
    def test_thirty_voices_each_with_a_description_and_a_gender(self):
        self.assertEqual(len(reglages.GEMINI_VOICES), 30)
        for name, (desc, gender) in reglages.GEMINI_VOICES.items():
            self.assertTrue(desc and gender in ("f", "h"), name)
        self.assertEqual(len(reglages.GEMINI_BY_LOWER), 30)

    def test_default_essai_and_config_voices_exist(self):
        import yaml

        cfg = yaml.safe_load((Path(__file__).resolve().parents[1] / "config.yaml").read_text(encoding="utf-8"))
        for name in [cfg["voix"]["gemini"]["voix"], *cfg["voix"]["essai"], *tts.ESSAI_PAR_DEFAUT]:
            self.assertIn(name, reglages.GEMINI_VOICES)
        self.assertEqual(cfg["voix"]["moteur"], "gemini")
        self.assertEqual(cfg["voix"]["gemini"]["modeles"], tts.DEFAULT_GEMINI["modeles"])


if __name__ == "__main__":
    unittest.main()
