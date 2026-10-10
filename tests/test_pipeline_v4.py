"""Tests hors ligne de la chaîne podcast v4 : réglages venus de Telegram, consignes, sonnerie, reveil.mp3, origine.

Lancer :  python -m unittest discover -s tests -t . -v
Les tests qui fabriquent de vrais fichiers audio demandent ffmpeg (ils sont sautés sans lui).
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from src import jingle, main, telegram, tts
from src.memoire import Memoire
from tests.test_podcast import WorldCase

needs_ffmpeg = unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg absent")


def make_mp3(path, seconds: float = 3.0) -> None:
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
         "-ac", "1", "-c:a", "libmp3lame", "-b:a", "48k", str(path)], check=True,
    )


class PipelineCase(WorldCase):
    def setUp(self):
        super().setUp()
        self.use_feeds({
            "numerama": ("tech", [{"title": "Houdini 21 arrive avec de nouveaux outils", "summary": "Du procédural partout."}]),
            "positif": ("positif", [{"title": "Bonne nouvelle : une découverte contre la pollution"}]),
        })
        self.racine = self.project()
        self.mem = Memoire(self.racine)
        self.out = self.tmp / "out"
        self.voice_cfgs: list[dict] = []

    def fake_tts(self, text, voice_cfg, out_path):
        self.voice_cfgs.append(dict(voice_cfg))
        make_mp3(out_path, 3)
        return "fr-FR-TestNeural"

    def generate(self, *, audio=False, **kw):
        kw.setdefault("send_telegram", False)
        with mock.patch.object(tts, "synthesize", side_effect=self.fake_tts):
            return main.generate(self.racine, self.cfg, self.out, keys=self.keys, now=self.now, no_audio=not audio, **kw)

    def meta(self) -> dict:
        return json.loads((self.out / "meta.json").read_text(encoding="utf-8"))

    def writer_systems(self) -> list[str]:
        """Les consignes système envoyées à Gemini pour ÉCRIRE le podcast (pas pour trier)."""
        systems = []
        for req in self.world.calls("/gemini/"):
            system = (req.json or {})["systemInstruction"]["parts"][0]["text"]
            if "voix d'un mini-podcast" in system:
                systems.append(system)
        return systems

    def target_words(self) -> int:
        (system, *_) = self.writer_systems()
        return int(re.search(r"Environ (\d+) mots", system).group(1))

    def settings(self, **podcast) -> None:
        data = self.mem.load_reglages()
        data["podcast"].update(podcast)
        self.mem.save_reglages(data)


# =========================================================================== durée, voix, vitesse
class TestSettingsReachThePodcast(PipelineCase):
    def test_the_length_chosen_in_telegram_sets_the_target(self):
        self.generate()
        default = self.target_words()
        self.world.requests.clear()
        self.settings(minutes=3)
        self.generate()
        self.assertGreater(self.target_words(), default + 100)
        self.assertLessEqual(self.target_words(), 450)                     # 3 min × 150 mots, moins ce que dit la partie perso

    def test_a_shorter_podcast_asks_for_fewer_words(self):
        self.generate()
        default = self.target_words()
        self.world.requests.clear()
        self.settings(minutes=1)
        self.generate()
        self.assertLess(self.target_words(), default)
        self.assertGreaterEqual(self.target_words(), 120)                  # jamais en dessous du plancher

    @needs_ffmpeg
    def test_voice_and_speed_reach_the_synthesis_without_touching_the_config(self):
        before = json.dumps(self.cfg["voix"], sort_keys=True)
        self.settings(voix="fr-FR-DeniseNeural", vitesse="-10%")
        self.generate(audio=True)
        (voice_cfg,) = self.voice_cfgs
        self.assertEqual((voice_cfg["edge_voix"], voice_cfg["vitesse"]), ("fr-FR-DeniseNeural", "-10%"))
        self.assertEqual(json.dumps(self.cfg["voix"], sort_keys=True), before)      # la config en mémoire n'est pas modifiée

    @needs_ffmpeg
    def test_without_settings_the_config_file_decides(self):
        self.generate(audio=True)
        (voice_cfg,) = self.voice_cfgs
        self.assertEqual(voice_cfg, self.cfg["voix"])

    def test_a_broken_settings_file_changes_nothing(self):
        (self.racine / "memoire" / "reglages.json").write_text("{pas du json", encoding="utf-8")
        self.assertEqual(self.generate(), main.EXIT_OK)
        self.assertEqual(self.mem.load_reglages()["podcast"], {"minutes": None, "voix": None, "vitesse": None})


# =========================================================================== consignes de style
class TestRulesInThePodcast(PipelineCase):
    def test_style_rules_reach_the_writer_and_are_counted(self):
        self.mem.add_consigne("parle-moi de façon décontractée", "tout", "2026-10-04", "telegram")
        self.mem.add_consigne("dans le podcast, commence par une petite blague", "podcast", "2026-10-04", "telegram")
        self.generate()
        (system, *_) = self.writer_systems()
        self.assertIn("Consignes personnelles de l'auditeur", system)
        self.assertIn("- parle-moi de façon décontractée", system)
        self.assertIn("- dans le podcast, commence par une petite blague", system)
        self.assertEqual(self.meta()["consignes"], 2)

    def test_private_rules_never_leave_for_gemini_or_groq(self):
        self.mem.add_consigne("fais des phrases courtes", "tout", "2026-10-04", "telegram")
        self.mem.add_consigne("ne me parle jamais de mon argent", "podcast", "2026-10-04", "telegram")           # sujet sensible
        self.mem.add_consigne("évite de me rappeler ce que j'ai dit sur Léa", "podcast", "2026-10-04", "telegram", prive=True)
        self.generate()
        sent = self.world.bodies("/gemini/") + self.world.bodies("/groq/")
        self.assertIn("fais des phrases courtes", sent)
        for private in ("mon argent", "sur Léa"):
            self.assertNotIn(private, sent)
        self.assertEqual(self.meta()["consignes"], 1)

    def test_rules_about_the_questions_are_not_for_the_podcast(self):
        self.mem.add_consigne("pose-moi des questions plus précises", "questions", "2026-10-04", "telegram")
        self.generate()
        self.assertNotIn("Consignes personnelles", self.writer_systems()[0])
        self.assertEqual(self.meta()["consignes"], 0)

    def test_rules_can_be_switched_off_in_the_config(self):
        self.mem.add_consigne("parle-moi de façon décontractée", "tout", "2026-10-04", "telegram")
        self.cfg["perso"]["consignes_dans_podcast"] = False
        self.generate()
        self.assertNotIn("Consignes personnelles", self.writer_systems()[0])
        self.assertEqual(self.meta()["consignes"], 0)

    def test_a_broken_rules_file_never_blocks_the_podcast(self):
        (self.racine / "memoire" / "consignes.yaml").write_text("- texte: [pas fermé\n  portee: tout: ?\n", encoding="utf-8")
        self.assertEqual(self.generate(), main.EXIT_OK)
        self.assertNotIn("Consignes personnelles", self.writer_systems()[0])
        self.assertEqual(self.meta()["consignes"], 0)


# =========================================================================== la sonnerie et reveil.mp3
@needs_ffmpeg
class TestAlarmFiles(PipelineCase):
    def duration(self, name: str) -> float:
        return float(main.audio_duration(self.out / name))

    def test_the_fallback_ring_is_always_built(self):
        self.generate()
        self.assertAlmostEqual(self.duration("sonnerie.mp3"), 45, delta=1.5)
        self.assertFalse((self.out / "reveil.mp3").exists())              # sans audio du podcast : pas de reveil.mp3

    def test_the_ring_survives_a_total_failure_of_the_ais(self):
        self.world.gemini_fn = lambda req, model, body: (500, "down")
        self.world.groq_fn = lambda req, model, body: (500, "down")
        self.assertEqual(self.generate(audio=True), main.EXIT_SECOURS)
        self.assertAlmostEqual(self.duration("sonnerie.mp3"), 45, delta=1.5)
        self.assertEqual(self.meta()["mode"], "secours")
        self.assertTrue(self.meta()["reveil_mp3"])                         # le réveil sonne quand même, avec le podcast de secours

    def test_reveil_is_the_ring_then_the_podcast(self):
        self.assertEqual(self.generate(audio=True), main.EXIT_OK)
        self.assertAlmostEqual(self.duration("latest.mp3"), 3, delta=1)
        self.assertAlmostEqual(self.duration("reveil.mp3"), 13, delta=1.5)  # 10 s de sonnerie + 3 s de podcast
        meta = self.meta()
        self.assertTrue(meta["reveil_mp3"])
        self.assertTrue(meta["audio"])

    def test_the_length_of_the_ring_comes_from_the_config(self):
        self.cfg["reveil"]["sonnerie_s"] = 6
        self.cfg["reveil"]["sonnerie_secours_s"] = 20
        self.generate(audio=True)
        self.assertAlmostEqual(self.duration("reveil.mp3"), 9, delta=1.5)
        self.assertAlmostEqual(self.duration("sonnerie.mp3"), 20, delta=1.5)

    def test_a_ring_that_cannot_be_built_leaves_the_plain_podcast(self):
        with mock.patch.object(jingle, "build", side_effect=jingle.JingleError("ffmpeg est absent")):
            self.assertEqual(self.generate(audio=True), main.EXIT_OK)
        self.assertTrue((self.out / "latest.mp3").exists())
        self.assertFalse((self.out / "reveil.mp3").exists())
        self.assertNotIn("reveil_mp3", self.meta())

    def test_a_fallback_ring_that_fails_does_not_stop_the_podcast(self):
        with mock.patch.object(jingle, "build_alarm", side_effect=jingle.JingleError("boum")):
            self.assertEqual(self.generate(audio=True), main.EXIT_OK)
        self.assertFalse((self.out / "sonnerie.mp3").exists())
        self.assertTrue((self.out / "reveil.mp3").exists())

    def test_ring_off_in_the_config_builds_neither_file(self):
        self.cfg["reveil"]["sonnerie"] = False
        self.generate(audio=True)
        self.assertFalse((self.out / "sonnerie.mp3").exists())
        self.assertFalse((self.out / "reveil.mp3").exists())
        self.assertTrue((self.out / "latest.mp3").exists())
        self.assertNotIn("reveil_mp3", self.meta())

    def test_stale_files_from_yesterday_are_removed(self):
        self.out.mkdir()
        for stale in ("reveil.mp3", "latest.mp3", "latest.txt", "meta.json", "choix.txt"):
            (self.out / stale).write_text("périmé", encoding="utf-8")
        with mock.patch.object(tts, "synthesize", side_effect=RuntimeError("edge en panne")):
            code = main.generate(self.racine, self.cfg, self.out, keys=self.keys, now=self.now, no_audio=False, send_telegram=False)
        self.assertEqual(code, main.EXIT_NO_AUDIO)
        self.assertFalse((self.out / "reveil.mp3").exists())              # jamais le réveil d'hier à la place de celui d'aujourd'hui
        self.assertFalse((self.out / "latest.mp3").exists())
        self.assertNotIn("périmé", (self.out / "latest.txt").read_text(encoding="utf-8"))

    def test_no_temporary_files_are_left_behind(self):
        self.generate(audio=True)
        self.assertEqual(sorted(p.name for p in self.out.iterdir()),
                         ["choix.txt", "latest.mp3", "latest.txt", "meta.json", "reveil.mp3", "sonnerie.mp3"])


# =========================================================================== qui a lancé le podcast, et en silence ?
class TestOriginAndSilence(PipelineCase):
    def setUp(self):
        super().setUp()
        self.cfg["reveil"]["sonnerie"] = False                              # ici on teste Telegram, pas la sonnerie
        patcher = mock.patch.object(telegram, "API_ROOT", self.world.base + "/telegram")
        patcher.start()
        self.addCleanup(patcher.stop)
        env = mock.patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": self.world.tg_token, "TELEGRAM_CHAT_ID": str(self.world.tg_chat)})
        env.start()
        self.addCleanup(env.stop)

    def test_the_meta_file_says_who_started_it(self):
        self.generate()
        self.assertEqual(self.meta()["origine"], "local")
        self.generate(origine="sonnette")
        self.assertEqual(self.meta()["origine"], "sonnette")

    @needs_ffmpeg
    def test_a_normal_podcast_arrives_with_a_notification(self):
        self.generate(audio=True, send_telegram=True)
        (sent,) = self.world.sent("sendVoice")
        self.assertFalse(sent["silent"])
        self.assertEqual(self.meta()["telegram"], True)

    @needs_ffmpeg
    def test_the_silent_flag_sends_without_a_notification(self):
        self.generate(audio=True, send_telegram=True, silent=True)
        (sent,) = self.world.sent("sendVoice")
        self.assertTrue(sent["silent"])

    @needs_ffmpeg
    def test_a_telegram_failure_never_fails_the_podcast(self):
        self.world.tg_fail["sendVoice"] = 500
        self.assertEqual(self.generate(audio=True, send_telegram=True), main.EXIT_OK)
        self.assertFalse(self.meta()["telegram"])
        self.assertTrue((self.out / "latest.mp3").exists())

    def test_the_command_line_reads_silence_and_origin_from_the_environment(self):
        cfg_path = self.tmp / "cfg.yaml"
        import yaml

        cfg_path.write_text(yaml.safe_dump(self.cfg, allow_unicode=True), encoding="utf-8")
        argv = ["--racine", str(self.racine), "--config", str(cfg_path), "--out", str(self.out)]
        cases = [
            ({}, False, ""),
            ({"PODCAST_SILENT": "1", "PODCAST_ORIGINE": "sonnette"}, True, "sonnette"),
            ({"PODCAST_SILENT": "true", "PODCAST_ORIGINE": "telegram"}, True, "telegram"),
            ({"PODCAST_SILENT": "OUI"}, True, ""),
            ({"PODCAST_SILENT": "0", "PODCAST_ORIGINE": "x" * 50}, False, "x" * 20),
            ({"PODCAST_SILENT": "", "PODCAST_ORIGINE": "  manuel  "}, False, "manuel"),
        ]
        for env, silent, origin in cases:
            with self.subTest(env), mock.patch.dict("os.environ", {"GEMINI_API_KEY": "g", "GROQ_API_KEY": "q", **env}), \
                    mock.patch.object(main, "generate", return_value=0) as generate:
                self.assertEqual(main.main(argv), 0)
            self.assertEqual((generate.call_args.kwargs["silent"], generate.call_args.kwargs["origine"]), (silent, origin))


if __name__ == "__main__":
    unittest.main()
