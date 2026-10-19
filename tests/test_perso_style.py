"""Tests hors ligne de perso_style : tes consignes sur la façon de dire tes cours, anniversaires et rappels.

Lancer :  python -m unittest discover -s tests -t . -v
Le « modèle » est simulé : ce qu'on vérifie, c'est le CONTRÔLE (rien d'inventé, rien de changé) et le repli sur le texte du programme.
"""
from __future__ import annotations

import json
import unittest
from unittest import mock

from src import llm, perso_style as P

ORIGINAL = ("Aujourd'hui, c'est l'anniversaire de Karim : pense à lui écrire un petit mot. "
            "Côté cours, aujourd'hui : Étalonnage de 9 h à 12 h 30, salle 204, puis Histoire du cinéma de 14 h à 17 h.")
ANCHORS = [("nom", "Karim"), ("cours", "Étalonnage"), ("cours", "Histoire du cinéma")]
CFG = {"llm": {"prive": [{"fournisseur": "groq", "modele": "m1"}, {"fournisseur": "gemini", "modele": "g1"}]}}
KEYS = {"groq": "q", "gemini": "g"}
GOOD = ("Hé, c'est l'anniversaire de Karim aujourd'hui, écris-lui un petit mot ! Ce matin t'as Étalonnage de 9 h à 12 h 30 "
        "en salle 204, et cet après-midi Histoire du cinéma de 14 h à 17 h.")


def reply(text: str) -> tuple[str, str]:
    return json.dumps({"texte": text}, ensure_ascii=False), "groq/m1"


class TestNumbers(unittest.TestCase):
    def test_digits_and_spoken_numbers_are_both_understood(self):
        self.assertEqual(P.numbers_in("9h30 et 14 h"), {9, 30, 14})
        self.assertEqual(P.numbers_in("à neuf heures trente"), {9, 30})
        self.assertEqual(P.numbers_in("vingt et un, soixante et onze, quatre vingt dix sept, dix huit, deux cent quatre"),
                         {21, 71, 97, 18, 204})

    def test_a_comma_separates_two_numbers(self):
        self.assertEqual(P.numbers_in("neuf heures trente, vingt"), {9, 30, 20})

    def test_un_and_une_alone_are_articles_not_numbers(self):
        self.assertEqual(P.numbers_in("un petit mot pour une amie"), set())
        self.assertEqual(P.numbers_in("vingt et un"), {21})


class TestTheGuard(unittest.TestCase):
    def check(self, output, rules=("parle de façon décontractée",), anchors=ANCHORS):
        return P.check(ORIGINAL, output, anchors, list(rules))

    def test_a_faithful_rewrite_passes(self):
        self.assertEqual(self.check(GOOD), [])

    def test_a_changed_hour_is_refused(self):
        problems = self.check(GOOD.replace("12 h 30", "12 h 45"))
        self.assertTrue(any("45" in p for p in problems), problems)

    def test_a_changed_room_is_refused(self):
        self.assertTrue(self.check(GOOD.replace("204", "205")))

    def test_a_changed_day_is_refused(self):
        self.assertTrue(any("demain" in p for p in self.check(GOOD.replace("aujourd'hui", "demain"))))

    def test_hours_in_words_are_accepted_when_they_match_the_digits(self):
        out = GOOD.replace("9 h à 12 h 30", "neuf heures à douze heures trente").replace("14 h à 17 h", "quatorze heures à dix sept heures")
        self.assertEqual(self.check(out, rules=["dis les heures en toutes lettres"]), [])

    def test_hours_in_words_that_do_not_match_are_refused(self):
        out = GOOD.replace("9 h", "dix heures")
        self.assertTrue(self.check(out))

    def test_counting_the_items_is_allowed(self):
        out = "Aujourd'hui tu as deux cours : Étalonnage et Histoire du cinéma, et c'est l'anniversaire de Karim."
        self.assertEqual(self.check(out, rules=["résume mes cours"]), [])

    def test_an_added_name_is_refused(self):
        self.assertTrue(any("Marie" in p for p in self.check(GOOD + " Pense aussi à embrasser Marie.")))

    def test_a_forgotten_course_or_name_is_refused_unless_the_rule_asks_for_it(self):
        short = "Hé, c'est l'anniversaire de Karim aujourd'hui. Ce matin t'as Étalonnage de 9 h à 12 h 30."
        self.assertTrue(any("Histoire du cinéma" in p for p in self.check(short)))
        self.assertEqual(self.check(short, rules=["dis seulement le premier cours"]), [])
        self.assertEqual(self.check(short, rules=["ne cite plus les cours de l'après-midi"]), [])

    def test_markup_and_links_are_refused(self):
        self.assertTrue(self.check(GOOD + " https://exemple.fr"))
        self.assertTrue(self.check("**" + GOOD + "**"))
        self.assertTrue(self.check(GOOD + " [[PERSO]]"))

    def test_a_much_longer_text_is_refused(self):
        self.assertTrue(any("trop long" in p for p in self.check(GOOD + " " + "Et voilà encore une phrase pour rien. " * 12)))

    def test_an_empty_text_is_refused_unless_the_rule_says_to_say_nothing(self):
        self.assertTrue(self.check(""))
        self.assertEqual(self.check("", rules=["ne me parle plus de mes cours"], anchors=ANCHORS[1:]), [])

    def test_which_rules_allow_leaving_things_out(self):
        for rule in ("dis seulement le nom du cours", "juste l'heure du premier cours", "ne cite plus mes cours",
                     "résume mes cours en une phrase", "sans les cours du matin", "ne cite plus mes cours de l'après-midi"):
            with self.subTest(rule):
                self.assertTrue(P.allows_omission([rule]))
        for rule in ("parle de façon décontractée", "dis-le avec plus de chaleur", "commence par l'heure", "fais des phrases courtes"):
            with self.subTest(rule):
                self.assertFalse(P.allows_omission([rule]))

    def test_dropping_only_a_detail_never_lets_a_course_disappear(self):
        short = "Hé, c'est l'anniversaire de Karim aujourd'hui. Ce matin t'as Étalonnage de 9 h à 12 h 30."
        for rule in ("cite mes cours sans la salle", "pas de salle", "enlève la salle et le prof", "ne dis plus la salle",
                     "évite de citer le prof", "oublie la salle du cours", "sans l'heure de fin"):
            with self.subTest(rule):
                self.assertFalse(P.allows_omission([rule]))
                self.assertTrue(any("Histoire du cinéma" in p for p in self.check(short, rules=[rule])))

    def test_a_detail_and_an_omission_together_still_allow_leaving_things_out(self):
        self.assertTrue(P.allows_omission(["cite mes cours sans la salle", "dis seulement le premier cours"]))

    def test_without_the_room_but_every_course_kept_passes(self):
        out = "Hé, c'est l'anniversaire de Karim aujourd'hui, écris-lui un petit mot ! Ce matin t'as Étalonnage de 9 h à 12 h 30, puis Histoire du cinéma de 14 h à 17 h."
        self.assertEqual(self.check(out, rules=["cite mes cours sans la salle"]), [])

    def test_a_reminder_may_be_reworded_but_not_lost(self):
        anchors = [("rappel", "rendre le dossier de montage")]
        original = "Demain : rendre le dossier de montage."
        self.assertEqual(P.check(original, "N'oublie pas de rendre ton dossier de montage demain.", anchors, ["plus chaleureux"]), [])
        self.assertTrue(P.check(original, "Demain, belle journée à toi.", anchors, ["plus chaleureux"]))


class TestRestyle(unittest.TestCase):
    def run_restyle(self, outputs, rules=("parle de façon décontractée",), cfg=CFG, text=ORIGINAL):
        replies = iter(outputs)

        def fake(prompt, **kw):
            self.calls.append({"prompt": prompt, **kw})
            item = next(replies)
            if isinstance(item, Exception):
                raise item
            return item if isinstance(item, tuple) else reply(item)

        self.calls: list[dict] = []
        with mock.patch.object(llm, "generate", side_effect=fake):
            return P.restyle(text, list(rules), cfg=cfg, keys=KEYS, prenom="Louis", anchors=ANCHORS)

    def test_a_good_rewrite_replaces_the_program_text(self):
        result = self.run_restyle([GOOD])
        self.assertEqual((result.text, result.status, result.rules, result.attempts), (GOOD, "applique", 1, 1))
        self.assertFalse(result.fallback)

    def test_the_prompt_carries_the_rules_the_text_and_the_name(self):
        self.run_restyle([GOOD], rules=["cite mes cours sans la salle", "parle de façon décontractée"])
        (call,) = self.calls
        self.assertIn("- cite mes cours sans la salle", call["prompt"])
        self.assertIn(ORIGINAL, call["prompt"])
        self.assertIn("Louis", call["system"])
        self.assertTrue(call["json_mode"])

    def test_only_groq_ever_receives_the_personal_lines(self):
        self.run_restyle([GOOD])
        (call,) = self.calls
        self.assertEqual([e["fournisseur"] for e in call["chain"]], ["groq"])           # l'entrée Gemini de la config est écartée

    def test_a_config_without_groq_leaves_the_text_alone(self):
        cfg = {"llm": {"prive": [{"fournisseur": "gemini", "modele": "g1"}]}}
        result = self.run_restyle([], cfg=cfg)
        self.assertEqual((result.text, result.fallback), (ORIGINAL, True))
        self.assertEqual(self.calls, [])

    def test_one_correction_is_allowed_and_the_problem_is_told_to_the_model(self):
        result = self.run_restyle([GOOD.replace("12 h 30", "12 h 45"), GOOD])
        self.assertEqual((result.text, result.status, result.attempts), (GOOD, "applique", 2))
        self.assertIn("45", self.calls[1]["prompt"])                                      # il sait pourquoi on l'a refusé
        self.assertIn("refusée", self.calls[1]["prompt"])

    def test_two_refusals_fall_back_to_the_program_text(self):
        bad = GOOD.replace("12 h 30", "12 h 45")
        result = self.run_restyle([bad, bad])
        self.assertEqual(result.text, ORIGINAL)
        self.assertTrue(result.fallback)
        self.assertIn("45", result.status)

    def test_an_unreadable_answer_falls_back(self):
        result = self.run_restyle([("pas du json du tout", "groq/m1"), ("toujours pas", "groq/m1")])
        self.assertEqual((result.text, result.fallback), (ORIGINAL, True))

    def test_an_answer_with_the_wrong_key_falls_back(self):
        wrong = (json.dumps({"phrase": GOOD}), "groq/m1")
        self.assertTrue(self.run_restyle([wrong, wrong]).fallback)

    def test_a_model_that_does_not_answer_falls_back_without_raising(self):
        result = self.run_restyle([llm.LLMError("quota épuisé")])
        self.assertEqual((result.text, result.fallback, result.attempts), (ORIGINAL, True, 1))
        self.assertIn("n'a pas répondu", result.status)

    def test_emoji_are_removed_before_they_are_spoken(self):
        result = self.run_restyle([GOOD + " 🎉"])
        self.assertEqual((result.status, result.text), ("applique", GOOD))

    def test_nothing_is_sent_without_rules_or_text(self):
        self.assertEqual(self.run_restyle([], rules=[]).status, "")
        self.assertEqual(self.run_restyle([], text="  ").status, "")
        self.assertEqual(self.calls, [])

    def test_the_pacer_is_consulted_when_given(self):
        pacer = mock.Mock()
        with mock.patch.object(llm, "generate", return_value=reply(GOOD)):
            P.restyle(ORIGINAL, ["x y z"], cfg=CFG, keys=KEYS, anchors=ANCHORS, pacer=pacer)
        pacer.wait.assert_called_once()


if __name__ == "__main__":
    unittest.main()
