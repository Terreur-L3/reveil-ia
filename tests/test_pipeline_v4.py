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
from datetime import datetime
from pathlib import Path
from unittest import mock

from src import edt, jingle, main, perso_style, reglages, telegram, tts
from src.memoire import Memoire
from tests.test_podcast import PARIS, WorldCase

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

    def fake_tts(self, text, voice_cfg, out_path, **kw):
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

    @needs_ffmpeg
    def test_a_gemini_choice_made_in_telegram_reaches_the_synthesis(self):
        self.settings(voix="gemini:Kore")
        self.generate(audio=True)
        (voice_cfg,) = self.voice_cfgs
        self.assertEqual((voice_cfg["moteur"], voice_cfg["gemini"]["voix"]), ("gemini", "Kore"))

    @needs_ffmpeg
    def test_a_voice_fallback_is_written_in_meta_and_sent_with_the_podcast(self):
        note = "voix Gemini indisponible (quota) : voix Edge à la place"
        with mock.patch.object(tts, "last_note", return_value=note):
            self.generate(audio=True)
        self.assertEqual(self.meta()["voix_note"], note)
        bot = mock.Mock()
        with mock.patch.object(telegram.Bot, "from_env", return_value=bot):
            main._send_to_telegram(self.out / "latest.mp3", self.cfg, self.now, 3, voix_note=note)
        self.assertIn(note, bot.send_voice.call_args.kwargs["caption"])
        with mock.patch.object(telegram.Bot, "from_env", return_value=bot):
            main._send_to_telegram(self.out / "latest.mp3", self.cfg, self.now, 3)
        self.assertNotIn("voix", bot.send_voice.call_args.kwargs["caption"].lower())

    @needs_ffmpeg
    def test_no_note_when_everything_went_normally(self):
        self.generate(audio=True)
        self.assertNotIn("voix_note", self.meta())

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


# =========================================================================== un réglage par jour
WEEK, WEEKEND = (0, 1, 2, 3, 4), (5, 6)
WEDNESDAY = datetime(2026, 10, 7, 5, 30, tzinfo=PARIS)
SATURDAY = datetime(2026, 10, 10, 5, 30, tzinfo=PARIS)


class TestPerDaySettingsInThePodcast(PipelineCase):
    """« 2 minutes en semaine, 5 le week-end » : ce que tu demandes par jour change vraiment le podcast de ce jour-là."""

    def set_for(self, key, value, days=None, rest=False) -> None:
        data = self.mem.load_reglages()
        reglages.set_podcast(data, key, value, days=days, rest=rest)
        self.mem.save_reglages(data)

    def words_on(self, when: datetime) -> int:
        self.now = when
        self.world.requests.clear()
        self.generate()
        return self.target_words()

    def test_a_longer_podcast_on_weekends_and_a_shorter_one_on_weekdays(self):
        self.set_for("minutes", 1.5, WEEK)
        self.set_for("minutes", 5, WEEKEND)
        weekday, weekend = self.words_on(WEDNESDAY), self.words_on(SATURDAY)
        self.assertLess(weekday, 260)
        self.assertGreater(weekend, 560)
        self.assertGreater(weekend, weekday + 300)

    def test_a_day_without_its_own_setting_keeps_the_usual_length(self):
        usual = self.words_on(WEDNESDAY)
        self.set_for("minutes", 5, WEEKEND)
        self.assertEqual(self.words_on(WEDNESDAY), usual)                  # le mercredi ne bouge pas
        self.assertGreater(self.words_on(SATURDAY), usual + 200)

    def test_the_other_days_rule(self):
        self.set_for("minutes", 5, WEEKEND)
        self.set_for("minutes", 2, rest=True)
        weekday, weekend = self.words_on(WEDNESDAY), self.words_on(SATURDAY)
        self.assertGreater(weekend, weekday + 300)

    def test_a_single_length_for_all_days_still_works(self):
        self.set_for("minutes", 3)
        self.assertEqual(self.words_on(WEDNESDAY), self.words_on(SATURDAY))

    @needs_ffmpeg
    def test_voice_and_speed_follow_the_day(self):
        self.set_for("voix", "fr-FR-DeniseNeural", WEEK)
        self.set_for("voix", "fr-FR-HenriNeural", WEEKEND)
        self.set_for("vitesse", "-10%", WEEK)
        self.set_for("vitesse", "+10%", WEEKEND)
        for when, voice, speed in ((WEDNESDAY, "fr-FR-DeniseNeural", "-10%"), (SATURDAY, "fr-FR-HenriNeural", "+10%")):
            with self.subTest(when.strftime("%A")):
                self.now = when
                self.voice_cfgs.clear()
                self.generate(audio=True)
                (voice_cfg,) = self.voice_cfgs
                self.assertEqual((voice_cfg["edge_voix"], voice_cfg["vitesse"]), (voice, speed))

    @needs_ffmpeg
    def test_a_gemini_voice_for_the_weekend_only(self):
        self.set_for("voix", "gemini:Kore", WEEKEND)
        self.now = SATURDAY
        self.generate(audio=True)
        (voice_cfg,) = self.voice_cfgs
        self.assertEqual((voice_cfg["moteur"], voice_cfg["gemini"]["voix"]), ("gemini", "Kore"))
        self.voice_cfgs.clear()
        self.now = WEDNESDAY
        self.generate(audio=True)
        (voice_cfg,) = self.voice_cfgs
        self.assertEqual(voice_cfg, self.cfg["voix"])                       # en semaine : la voix de config.yaml

    def test_the_config_in_memory_is_not_modified_by_the_day_settings(self):
        before = json.dumps(self.cfg, sort_keys=True, default=str)
        self.set_for("minutes", 5, WEEKEND)
        self.now = SATURDAY
        self.generate()
        self.assertEqual(json.dumps(self.cfg, sort_keys=True, default=str), before)

    def test_a_damaged_day_entry_is_ignored(self):
        data = json.loads((self.racine / "memoire" / "reglages.json").read_text(encoding="utf-8")) if (self.racine / "memoire" / "reglages.json").exists() else {}
        data.setdefault("podcast", {})["jours"] = {"sam": {"minutes": "beaucoup"}, "dimanche": {"minutes": 4}, "lun": "x"}
        (self.racine / "memoire" / "reglages.json").write_text(json.dumps(data), encoding="utf-8")
        self.now = SATURDAY
        self.assertEqual(self.generate(), main.EXIT_OK)
        self.assertEqual(self.mem.load_reglages()["podcast"], {"minutes": None, "voix": None, "vitesse": None})


class TestRulesByDayInThePodcast(PipelineCase):
    def weekend_rule(self, **kw):
        self.mem.add_consigne("sois plus décontracté et parle-moi comme à un copain", "podcast", "2026-10-04", "telegram",
                              jours=["sam", "dim"], **kw)

    def test_a_weekend_rule_reaches_the_weekend_podcast_only(self):
        self.weekend_rule()
        for when, expected in ((SATURDAY, 1), (WEDNESDAY, 0), (self.now, 1)):                  # self.now est un dimanche
            with self.subTest(when.strftime("%A %d")):
                self.now = when
                self.world.requests.clear()
                self.generate()
                (system, *_) = self.writer_systems()
                self.assertEqual("- sois plus décontracté et parle-moi comme à un copain" in system, bool(expected))
                self.assertEqual(self.meta()["consignes"], expected)

    def test_rules_for_different_days_each_find_their_day(self):
        self.mem.add_consigne("va droit au but, sans blague", "podcast", "2026-10-04", "telegram", jours=["lun", "mar", "mer", "jeu", "ven"])
        self.weekend_rule()
        self.mem.add_consigne("fais des phrases courtes", "tout", "2026-10-04", "telegram")
        for when, expected in ((WEDNESDAY, ["va droit au but, sans blague", "fais des phrases courtes"]),
                               (SATURDAY, ["sois plus décontracté et parle-moi comme à un copain", "fais des phrases courtes"])):
            with self.subTest(when.strftime("%A")):
                self.now = when
                self.world.requests.clear()
                self.generate()
                (system, *_) = self.writer_systems()
                found = re.findall(r"^- (.+)$", system.split("Consignes personnelles", 1)[1], re.MULTILINE)
                self.assertEqual(found, expected)

    def test_a_private_weekend_rule_still_never_reaches_gemini(self):
        self.weekend_rule(prive=True)
        self.now = SATURDAY
        self.generate()
        self.assertNotIn("copain", self.world.bodies("/gemini/") + self.world.bodies("/groq/"))
        self.assertEqual(self.meta()["consignes"], 0)


# =========================================================================== « cite mes cours d'une certaine manière »
BASE = "Côté cours, aujourd'hui : Étalonnage de 9 h à 12 h 30, en salle 204, puis Prise de vue de 14 h à 17 h, en Studio B."
SHORT = "Ce matin, Étalonnage de 9 h à 12 h 30, puis Prise de vue de 14 h à 17 h."
MONDAY = datetime(2026, 10, 5, 5, 30, tzinfo=PARIS)


class PersoCase(PipelineCase):
    def setUp(self):
        super().setUp()
        data = edt.empty()
        data["semaine_type"]["lun"] = [
            {"debut": "09:00", "fin": "12:30", "cours": "Étalonnage", "salle": "204"},
            {"debut": "14:00", "fin": "17:00", "cours": "Prise de vue", "salle": "Studio B"}]
        edt.save(self.mem.p_edt, data)
        self.now = MONDAY
        self.world.perso_fn = lambda prompt: SHORT

    def restyle_calls(self):
        return [r for r in self.world.calls("/groq/chat") if "Tu réécris un court passage" in r.plain]

    def spoken(self) -> str:
        return (self.out / "latest.txt").read_text(encoding="utf-8")

    def rule(self, text="cite mes cours sans la salle", scope="perso", **kw):
        self.assertTrue(self.mem.add_consigne(text, scope, "2026-10-04", "telegram", **kw))


class TestCoursesSpokenYourWay(PersoCase):
    def test_without_a_rule_the_program_text_is_spoken_and_no_ai_sees_it(self):
        self.generate()
        self.assertIn(BASE, self.spoken())
        self.assertEqual(self.restyle_calls(), [])
        self.assertNotIn("Étalonnage", self.world.bodies("/gemini/") + self.world.bodies("/groq/"))
        self.assertEqual(self.meta()["perso"], {"lignes": 1})

    def test_a_rule_about_the_courses_changes_how_they_are_spoken(self):
        self.rule()
        self.generate()
        spoken = self.spoken()
        self.assertIn(SHORT, spoken)
        self.assertNotIn("salle 204", spoken)
        self.assertNotIn(BASE, spoken)
        (call,) = self.restyle_calls()
        self.assertIn("- cite mes cours sans la salle", call.plain)
        self.assertIn(BASE, call.plain)
        self.assertEqual(self.meta()["perso"], {"lignes": 1, "consignes": 1, "style": "applique"})

    def test_the_courses_never_go_to_gemini_even_when_a_rule_applies(self):
        self.rule()
        self.generate()
        self.assertEqual(self.world.calls("/groq/chat") and True, True)
        gemini = self.world.bodies("/gemini/")
        for private in ("Étalonnage", "Prise de vue", "salle 204", "sans la salle", "Studio B"):
            self.assertNotIn(private, gemini)

    def test_a_private_rule_about_the_courses_goes_to_groq_and_never_to_gemini(self):
        self.rule("ne cite jamais mon cours de rattrapage en Studio B", prive=True)
        self.generate()
        (call,) = self.restyle_calls()
        self.assertIn("rattrapage", call.plain)
        self.assertNotIn("rattrapage", self.world.bodies("/gemini/"))

    def test_an_old_rule_filed_under_podcast_is_still_applied_to_the_courses(self):
        self.rule("cite mes cours en commençant par l'heure", scope="podcast")
        self.world.perso_fn = lambda prompt: "À 9 h, Étalonnage jusqu'à 12 h 30, puis à 14 h, Prise de vue jusqu'à 17 h."
        self.generate()
        self.assertIn("À 9 h, Étalonnage jusqu'à 12 h 30", self.spoken())
        self.assertEqual(len(self.restyle_calls()), 1)

    def test_a_rule_that_is_not_about_the_courses_does_not_trigger_the_rewrite(self):
        self.rule("fais des phrases courtes", scope="tout")
        self.rule("dans le podcast, commence par une petite blague", scope="podcast")
        self.generate()
        self.assertEqual(self.restyle_calls(), [])
        self.assertIn(BASE, self.spoken())
        self.assertNotIn("consignes", self.meta()["perso"])

    def test_a_rule_for_another_day_does_not_apply(self):
        self.rule("cite mes cours sans la salle", jours=["mar"])
        self.generate()
        self.assertEqual(self.restyle_calls(), [])
        self.assertIn(BASE, self.spoken())

    def test_a_rule_for_this_day_applies(self):
        self.rule("cite mes cours sans la salle", jours=["lun", "mer"])
        self.generate()
        self.assertEqual(len(self.restyle_calls()), 1)
        self.assertIn(SHORT, self.spoken())

    def test_nothing_is_sent_when_there_is_nothing_to_say_that_day(self):
        self.rule()
        self.now = SATURDAY                                                    # pas de cours, ni anniversaire, ni rappel
        self.generate()
        self.assertEqual(self.restyle_calls(), [])
        self.assertEqual(self.meta()["perso"], {"lignes": 0})

    def test_the_switch_in_the_config_turns_the_rewrite_off(self):
        self.rule()
        self.cfg["perso"]["style_ia"] = False
        self.generate()
        self.assertEqual(self.restyle_calls(), [])
        self.assertIn(BASE, self.spoken())
        self.assertEqual(self.meta()["perso"], {"lignes": 1})

    def test_birthdays_and_reminders_follow_the_same_rule(self):
        (self.racine / "memoire" / "anniversaires.yaml").write_text("- {nom: Karim, jour: 5, mois: 10}\n", encoding="utf-8")
        (self.racine / "memoire" / "rappels.yaml").write_text("- {texte: rendre le dossier de montage, date: '2026-10-05'}\n", encoding="utf-8")
        self.rule("souhaite les anniversaires avec chaleur et dis les rappels simplement", scope="perso")
        seen = {}
        original = ("Aujourd'hui, c'est l'anniversaire de Karim : pense à lui écrire un petit mot. "
                    "Pour aujourd'hui : rendre le dossier de montage. " + BASE)

        def fake(prompt):
            seen["prompt"] = prompt
            return ("Aujourd'hui, c'est l'anniversaire de Karim : écris-lui un gentil petit mot ! "
                    "Pour aujourd'hui, rends le dossier de montage. " + BASE.replace("Côté cours, aujourd'hui :", "Côté cours :"))

        self.world.perso_fn = fake
        self.generate()
        self.assertIn(original.split(" Côté cours")[0].split("Aujourd'hui")[1][:20], seen["prompt"])
        self.assertIn("écris-lui un gentil petit mot", self.spoken())
        self.assertEqual(self.meta()["perso"]["lignes"], 3)
        self.assertEqual(self.meta()["perso"]["style"], "applique")


class TestWhenTheRewriteGoesWrong(PersoCase):
    """Une consigne mal appliquée ne doit jamais fausser ton matin : le texte sûr du programme est dit à la place."""

    def setUp(self):
        super().setUp()
        self.rule()

    def test_a_changed_hour_is_caught_and_the_program_text_is_spoken(self):
        self.world.perso_fn = lambda prompt: SHORT.replace("9 h", "10 h")
        self.assertEqual(self.generate(), main.EXIT_OK)
        self.assertIn(BASE, self.spoken())
        self.assertNotIn("10 h", self.spoken())
        meta = self.meta()["perso"]
        self.assertTrue(meta["style"].startswith("repli"), meta)
        self.assertIn("10", meta["style"])
        self.assertEqual(len(self.restyle_calls()), 2)                          # une correction a été tentée

    def test_one_bad_answer_then_a_good_one_is_accepted(self):
        answers = iter([SHORT.replace("9 h", "10 h"), SHORT])
        self.world.perso_fn = lambda prompt: next(answers)
        self.generate()
        self.assertIn(SHORT, self.spoken())
        self.assertEqual(self.meta()["perso"]["style"], "applique")

    def test_an_invented_name_is_caught(self):
        self.world.perso_fn = lambda prompt: SHORT + " Dis bonjour à Marie de ma part."
        self.generate()
        self.assertIn(BASE, self.spoken())
        self.assertNotIn("Marie", self.spoken())

    def test_a_forgotten_course_is_caught_unless_the_rule_allows_it(self):
        self.world.perso_fn = lambda prompt: "Ce matin, Étalonnage de 9 h à 12 h 30."
        self.generate()
        self.assertIn(BASE, self.spoken())                                      # « sans la salle » n'autorise pas d'oublier un cours
        self.assertIn("Prise de vue", self.meta()["perso"]["style"])

    def test_groq_down_never_blocks_the_podcast(self):
        smart = self.world.groq_fn
        self.world.groq_fn = lambda req, model, body: (500, "down") if "Tu réécris" in json.dumps(body, ensure_ascii=False) else smart(req, model, body)
        self.assertEqual(self.generate(), main.EXIT_OK)
        self.assertIn(BASE, self.spoken())
        self.assertEqual(self.meta()["perso"]["style"], "repli : l'IA n'a pas répondu")

    def test_an_unreadable_answer_falls_back(self):
        smart = self.world.groq_fn
        self.world.groq_fn = lambda req, model, body: (200, "ceci n'est pas du json") if "Tu réécris" in json.dumps(body, ensure_ascii=False) else smart(req, model, body)
        self.assertEqual(self.generate(), main.EXIT_OK)
        self.assertIn(BASE, self.spoken())
        self.assertTrue(self.meta()["perso"]["style"].startswith("repli"))

    def test_a_config_without_groq_keeps_the_program_text(self):
        self.cfg["llm"]["prive"] = [e for e in self.cfg["llm"]["prive"] if e["fournisseur"] != "groq"]
        self.generate()
        self.assertEqual(self.restyle_calls(), [])
        self.assertIn(BASE, self.spoken())
        self.assertIn("Groq", self.meta()["perso"]["style"])

    def test_a_podcast_of_last_resort_also_speaks_your_courses_your_way(self):
        self.world.feeds.update({"numerama": 500, "positif": 500})
        self.assertEqual(self.generate(), main.EXIT_SECOURS)
        self.assertEqual(self.meta()["mode"], "secours")
        self.assertIn(SHORT, self.spoken())

    def test_a_bug_in_the_rewrite_step_never_removes_the_courses(self):
        with mock.patch.object(perso_style, "restyle", side_effect=RuntimeError("boum")):
            self.assertEqual(self.generate(), main.EXIT_OK)
        self.assertIn(BASE, self.spoken())
        self.assertEqual(self.meta()["perso"], {"lignes": 1})

    def test_the_note_explains_it_under_the_podcast(self):
        style = perso_style.Result(BASE, "repli : nombre ajouté ou changé : 10", 1, 2)
        self.assertEqual(main._style_note(style), "ℹ️ consigne sur tes infos perso non appliquée ce matin : nombre ajouté ou changé : 10")
        self.assertEqual(main._style_note(perso_style.Result(SHORT, "applique", 1, 1)), "")
        self.assertEqual(main._style_note(perso_style.Result(BASE)), "")


@needs_ffmpeg
class TestTheNoteInTelegram(PersoCase):
    def setUp(self):
        super().setUp()
        self.cfg["reveil"]["sonnerie"] = False
        patcher = mock.patch.object(telegram, "API_ROOT", self.world.base + "/telegram")
        patcher.start()
        self.addCleanup(patcher.stop)
        env = mock.patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": self.world.tg_token, "TELEGRAM_CHAT_ID": str(self.world.tg_chat)})
        env.start()
        self.addCleanup(env.stop)

    def caption(self) -> str:
        (sent,) = self.world.sent("sendVoice")
        return sent["caption"]

    def test_a_failed_rewrite_is_announced_with_the_podcast(self):
        self.rule()
        self.world.perso_fn = lambda prompt: SHORT.replace("14 h", "15 h")
        self.generate(audio=True, send_telegram=True)
        self.assertIn("consigne sur tes infos perso non appliquée", self.caption())

    def test_nothing_is_added_when_the_rewrite_worked_or_there_was_no_rule(self):
        self.generate(audio=True, send_telegram=True)
        self.assertNotIn("consigne", self.caption())
        self.world.tg_sent.clear()
        self.rule()
        self.generate(audio=True, send_telegram=True)
        self.assertNotIn("consigne", self.caption())


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
