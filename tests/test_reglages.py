"""Tests hors ligne des réglages : l'heure du réveil (ordres compris en français) et la façon dont le podcast est dit.

Lancer :  python -m unittest discover -s tests -t . -v
Le fichier tests/data/wake_cases.json est AUSSI lu par les tests de la sonnette (extras/sonnette/worker.test.js) :
les deux programmes doivent lire l'horaire exactement de la même façon.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from src import reglages as R

ROOT = Path(__file__).resolve().parent.parent
PARIS = ZoneInfo("Europe/Paris")
TODAY = date(2026, 10, 7)                                   # un mercredi
NOW = datetime(2026, 10, 7, 21, 30, tzinfo=PARIS)
WEEK = (0, 1, 2, 3, 4)
WEEKEND = (5, 6)


def req(action, time=None, days=None, on_date=None) -> R.WakeRequest:
    return R.WakeRequest(action, time, days, on_date)


def day(iso: str) -> date:
    return date.fromisoformat(iso)


def fresh() -> dict:
    return R.normalize(None)


# =========================================================================== cas partagés avec la sonnette
class TestSharedFixtures(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = json.loads((ROOT / "tests" / "data" / "wake_cases.json").read_text(encoding="utf-8"))

    def test_the_file_has_enough_cases(self):
        self.assertGreaterEqual(len(self.data["wake_for"]), 30)
        self.assertGreaterEqual(len(self.data["next_wake"]), 10)

    def test_wake_for(self):
        for case in self.data["wake_for"]:
            with self.subTest(case["nom"]):
                self.assertEqual(R.wake_for(R.normalize(case["reglages"]), day(case["jour"])), case["attendu"])

    def test_next_wake(self):
        for case in self.data["next_wake"]:
            with self.subTest(case["nom"]):
                got = R.next_wake(R.normalize(case["reglages"]), datetime.fromisoformat(case["maintenant"]).replace(tzinfo=PARIS))
                self.assertEqual({"jour": got[0].isoformat(), "heure": got[1]} if got else None, case["attendu"])


# =========================================================================== lecture / écriture du fichier
class TestFile(unittest.TestCase):
    def test_defaults_are_complete_and_independent(self):
        a, b = fresh(), fresh()
        self.assertEqual(list(a["reveil"]["jours"]), R.DAYS)
        self.assertFalse(a["reveil"]["actif"])
        self.assertEqual(a["journal_offset"], 0)
        a["reveil"]["jours"]["lun"] = "07:00"
        self.assertIsNone(b["reveil"]["jours"]["lun"])          # pas de dictionnaire partagé entre deux copies

    def test_junk_never_crashes(self):
        for junk in (None, 42, "x", [], [1, 2], {"reveil": None}, {"reveil": []}, {"reveil": {"jours": None, "ponctuel": 3}},
                     {"podcast": "oups"}, {"reveil": {"jours": {"lun": 630, "mar": "6h30", "mer": "24:00"}}}):
            with self.subTest(repr(junk)):
                s = R.normalize(junk)
                self.assertEqual(list(s["reveil"]["jours"]), R.DAYS)
                self.assertTrue(all(v is None for v in s["reveil"]["jours"].values()))

    def test_only_valid_values_survive(self):
        s = R.normalize({
            "reveil": {"actif": True, "jours": {"lun": "06:30", "mar": "6:30", "mer": "25:00", "jeu": "06:60"},
                       "ponctuel": {"2026-10-08": "05:45", "2026-10-09": "off", "2026-02-30": "07:00", "demain": "07:00",
                                    "2026-10-10": "tard"}},
            "podcast": {"minutes": 3, "voix": "fr-FR-DeniseNeural", "vitesse": "-10%"},
        })
        self.assertEqual(s["reveil"]["jours"]["lun"], "06:30")
        self.assertEqual([s["reveil"]["jours"][d] for d in ("mar", "mer", "jeu")], [None, None, None])
        self.assertEqual(s["reveil"]["ponctuel"], {"2026-10-08": "05:45", "2026-10-09": "off"})
        self.assertEqual(s["podcast"], {"minutes": 3, "voix": "fr-FR-DeniseNeural", "vitesse": "-10%"})

    def test_podcast_values_out_of_range_are_dropped(self):
        for pod in ({"minutes": 0}, {"minutes": 9}, {"minutes": True}, {"minutes": "3"}, {"voix": "robot"}, {"voix": 3},
                    {"vitesse": "vite"}, {"vitesse": "+200%"}, {"vitesse": 10}):
            with self.subTest(repr(pod)):
                self.assertEqual(R.normalize({"podcast": pod})["podcast"], {"minutes": None, "voix": None, "vitesse": None})

    def test_journal_offset_only_accepts_non_negative_integers(self):
        self.assertEqual(R.normalize({"journal_offset": 502})["journal_offset"], 502)
        for bad in (-1, True, "12", 1.5, None, [3]):
            with self.subTest(repr(bad)):
                self.assertEqual(R.normalize({"journal_offset": bad})["journal_offset"], 0)

    def test_save_then_load_roundtrip_and_no_leftover(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "memoire" / "reglages.json"
            s = fresh()
            R.apply_request(s, req("set", "06:30", WEEK), NOW)
            R.apply_request(s, req("set", "05:45", on_date=day("2026-10-08")), NOW)
            s["journal_offset"] = 77
            s["sonnette_vue"] = "2026-10-07T05:00:00+02:00"
            R.save(path, s)
            self.assertEqual(R.load(path), R.normalize(s))
            self.assertEqual([p.name for p in path.parent.iterdir()], ["reglages.json"])      # pas de « .part » oublié
            raw = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(raw["reveil"]["jours"]["lun"], "06:30")                           # format lu par la sonnette
            self.assertEqual(raw["reveil"]["ponctuel"], {"2026-10-08": "05:45"})
            self.assertEqual(raw["journal_offset"], 77)

    def test_missing_or_broken_file_gives_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(R.load(Path(tmp) / "nope.json"), fresh())
            broken = Path(tmp) / "reglages.json"
            broken.write_text("{ pas du json", encoding="utf-8")
            self.assertEqual(R.load(broken), fresh())

    def test_shipped_settings_file_is_valid_if_present(self):
        path = ROOT / "memoire" / "reglages.json"
        if path.exists():
            raw = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(R.normalize(raw)["reveil"]["jours"], raw["reveil"]["jours"])


# =========================================================================== l'horaire
class TestSchedule(unittest.TestCase):
    def test_french_time_format(self):
        self.assertEqual([R.fr_time(t) for t in ("06:30", "07:00", "00:05", "23:59", "12:00")],
                         ["6 h 30", "7 h", "0 h 05", "23 h 59", "12 h"])

    def test_day_labels(self):
        self.assertEqual(R.day_label(day("2026-10-07"), TODAY), "aujourd'hui (mercredi 7 octobre)")
        self.assertEqual(R.day_label(day("2026-10-08"), TODAY), "demain (jeudi 8 octobre)")
        self.assertEqual(R.day_label(day("2026-10-12"), TODAY), "lundi 12 octobre")
        self.assertEqual(R.day_label(day("2026-11-01"), TODAY), "dimanche 1er novembre")

    def test_week_summary_groups_days(self):
        s = fresh()
        R.apply_request(s, req("set", "06:30", WEEK), NOW)
        R.apply_request(s, req("set", "09:00", WEEKEND), NOW)
        self.assertEqual(R.week_summary(s["reveil"]["jours"]), "lun–ven 6 h 30 · sam–dim 9 h")
        R.apply_request(s, req("off", days=(6,)), NOW)
        self.assertEqual(R.week_summary(s["reveil"]["jours"]), "lun–ven 6 h 30 · sam 9 h · dim pas de réveil")
        R.apply_request(s, req("set", "07:00"), NOW)
        self.assertEqual(R.week_summary(s["reveil"]["jours"]), "tous les jours 7 h")
        self.assertEqual(R.week_summary(fresh()["reveil"]["jours"]), "aucune heure choisie")

    def test_describe(self):
        self.assertIn("Aucun réveil réglé", R.describe(fresh(), NOW))
        s = fresh()
        R.apply_request(s, req("set", "06:30", WEEK), NOW)
        text = R.describe(s, NOW)
        self.assertIn("⏰ Réveil : lun–ven 6 h 30 · sam–dim pas de réveil", text)
        self.assertIn("Prochain réveil : demain (jeudi 8 octobre) à 6 h 30.", text)
        R.apply_request(s, req("off"), NOW)
        self.assertIn("DÉSACTIVÉ", R.describe(s, NOW))
        self.assertIn("/reveil on", R.describe(s, NOW))
        self.assertNotIn("Prochain réveil", R.describe(s, NOW))

    def test_describe_lists_upcoming_exceptions_only(self):
        s = fresh()
        R.apply_request(s, req("set", "06:30", WEEK), NOW)
        s["reveil"]["ponctuel"].update({"2026-10-01": "05:00", "2026-10-08": "05:45", "2026-10-10": "off"})
        text = R.describe(s, NOW)
        self.assertIn("• demain (jeudi 8 octobre) : 5 h 45", text)
        self.assertIn("• samedi 10 octobre : pas de réveil", text)
        self.assertNotIn("1 octobre", text)

    def test_next_wake_today_if_not_passed(self):
        s = fresh()
        R.apply_request(s, req("set", "06:30"), NOW)
        early = datetime(2026, 10, 8, 5, 0, tzinfo=PARIS)
        self.assertEqual(R.next_wake(s, early), (day("2026-10-08"), "06:30"))
        late = datetime(2026, 10, 8, 6, 31, tzinfo=PARIS)
        self.assertEqual(R.next_wake(s, late), (day("2026-10-09"), "06:30"))

    def test_prune_forgets_past_exceptions_but_keeps_yesterday_and_future(self):
        s = fresh()
        s["reveil"]["ponctuel"].update({"2026-10-05": "05:00", "2026-10-06": "05:00", "2026-10-07": "off", "2026-10-20": "07:00"})
        R.prune(s, TODAY)
        self.assertEqual(sorted(s["reveil"]["ponctuel"]), ["2026-10-06", "2026-10-07", "2026-10-20"])


# =========================================================================== comprendre un ordre
ORDERS = [
    # ---- régler l'heure
    ("Mets mon réveil à 6h30", [req("set", "06:30")]),
    ("Mets mon réveil à 6 h 30 en semaine", [req("set", "06:30", WEEK)]),
    ("Réveille-moi à 7 heures", [req("set", "07:00")]),
    ("réveille moi à sept heures et demie demain", [req("set", "07:30", on_date=day("2026-10-08"))]),
    ("Mets mon réveil à six heures moins le quart", [req("set", "05:45")]),
    ("Change l'heure du podcast à 8h", [req("set", "08:00")]),
    ("Je veux que le podcast soit lu à 7h15", [req("set", "07:15")]),
    ("Réveil à 7h", [req("set", "07:00")]),
    ("Mets le réveil à 7h du soir", [req("set", "19:00")]),
    ("Mets le réveil à midi", [req("set", "12:00")]),
    ("Mets mon réveil à 6h45, j'ai bossé sur Houdini toute la journée.", [req("set", "06:45")]),
    ("J'ai bossé sur Houdini toute la journée, c'était génial. Mets mon réveil à 6h45.", [req("set", "06:45")]),
    # ---- quels jours
    ("Décale le réveil à 9h le week-end", [req("set", "09:00", WEEKEND)]),
    ("Mets le réveil à 9h tous les week-ends", [req("set", "09:00", WEEKEND)]),
    ("Mets le réveil à 7h samedi et dimanche", [req("set", "07:00", WEEKEND)]),
    ("Mets le réveil à 7h du lundi au vendredi", [req("set", "07:00", WEEK)]),
    ("Mets le réveil à 8h le lundi", [req("set", "08:00", (0,))]),
    ("Mets le réveil à 8h tous les lundis", [req("set", "08:00", (0,))]),
    ("Mets le réveil le lundi 8h", [req("set", "08:00", (0,))]),                      # même sens que « à 8h le lundi »
    ("Mets le réveil le lundi 8h30", [req("set", "08:30", (0,))]),
    ("Mets le réveil le lundi 10h", [req("set", "10:00", (0,))]),                     # « 10 » n'est pas un numéro de jour
    ("Mets le réveil le lundi 08:30", [req("set", "08:30", (0,))]),
    ("Mets le réveil à 8h le lundi 12", [req("set", "08:00", on_date=day("2026-10-12"))]),   # là, « 12 » est bien une date
    ("Mets le réveil à 7h les mardis et jeudis", [req("set", "07:00", (1, 3))]),
    ("Mets le réveil à 7h le lundi et le mercredi", [req("set", "07:00", (0, 2))]),
    ("Mets mon réveil à 7h tous les jours", [req("set", "07:00")]),
    ("Désormais mets mon réveil à 7h", [req("set", "07:00")]),
    ("À partir de demain mets mon réveil à 7h", [req("set", "07:00")]),
    ("À partir de maintenant mets mon réveil à 7h", [req("set", "07:00")]),
    ("À partir d'aujourd'hui réveille-moi à 6h45", [req("set", "06:45")]),
    ("À partir de demain mets le réveil à 9h le week-end", [req("set", "09:00", WEEKEND)]),
    # ---- une date précise
    ("Mets le réveil à 5h45 demain", [req("set", "05:45", on_date=day("2026-10-08"))]),
    ("Mets le réveil à 8h aujourd'hui", [req("set", "08:00", on_date=day("2026-10-07"))]),
    ("Mets le réveil à 8h lundi", [req("set", "08:00", on_date=day("2026-10-12"))]),
    ("Mets le réveil à 6h le 12 octobre", [req("set", "06:00", on_date=day("2026-10-12"))]),
    ("Mets le réveil à 8h le lundi 12 octobre", [req("set", "08:00", on_date=day("2026-10-12"))]),
    ("Mets le réveil à 9h30 ce week-end", [req("set", "09:30", on_date=day("2026-10-10")),
                                           req("set", "09:30", on_date=day("2026-10-11"))]),
    # ---- plusieurs ordres dans un message
    ("Mets le podcast à 7h30 en semaine et à 9h le week-end.", [req("set", "07:30", WEEK), req("set", "09:00", WEEKEND)]),
    ("Mets mon réveil à 8h. Je dors 8 heures, mets mon réveil à 7h", [req("set", "08:00"), req("set", "07:00")]),
    ("Mets mon réveil à 7h30 en semaine, et le week-end à 9h", [req("set", "07:30", WEEK), req("set", "09:00", WEEKEND)]),
    ("Mets mon réveil à 7h et le week-end à 9h", [req("set", "07:00"), req("set", "09:00", WEEKEND)]),   # 7h partout, puis 9h le week-end
    ("Mets le réveil à 7h demain et à 9h après-demain", [req("set", "07:00", on_date=day("2026-10-08")),
                                                         req("set", "09:00", on_date=day("2026-10-09"))]),
    # ---- la suite d'un ordre, dans la phrase d'après (sans le mot « réveil »)
    ("Mets mon réveil à 7h. Et le week-end à 9h.", [req("set", "07:00"), req("set", "09:00", WEEKEND)]),
    ("Mets mon réveil à 7h. Et le samedi et le dimanche à 10h.", [req("set", "07:00"), req("set", "10:00", WEEKEND)]),
    ("Mets mon réveil à 7h. Sinon le lundi 9h.", [req("set", "07:00"), req("set", "09:00", (0,))]),
    ("Mets mon réveil à 7h. Pour le week-end, mets-le à 9h.", [req("set", "07:00"), req("set", "09:00", WEEKEND)]),
    ("Mets le réveil à 7h. Du 12 au 15 octobre, mets-le à 5h.", [req("set", "07:00"), req("ask")]),
    ("Mets le réveil à 7h. Et à 5h du 12 au 15 octobre.", [req("set", "07:00"), req("ask")]),
    ("Mets le réveil à 7h. Et à 9h.", [req("set", "07:00"), req("unclear")]),                         # 9h quand ? on ne devine pas
    # ---- plusieurs heures dont les jours ne sont pas clairs : on le dit, on ne change rien
    ("Mets mon réveil à 7h et 9h", [req("unclear")]),
    ("Mets le réveil à 7h puis à 9h", [req("unclear")]),
    ("Mets le réveil à 9h le week-end et à 7h", [req("unclear")]),
    # ---- couper, remettre, demander
    ("Pas de réveil demain", [req("off", on_date=day("2026-10-08"))]),
    ("Annule le réveil de demain", [req("off", on_date=day("2026-10-08"))]),
    ("Pas de réveil le week-end", [req("off", days=WEEKEND)]),
    ("Désactive le réveil", [req("off")]),
    ("Réactive le réveil", [req("on")]),
    ("À quelle heure est mon réveil ?", [req("show")]),
    ("À demain, pas de réveil le week-end", [req("ask")]),                    # « demain » ET « le week-end » : lequel ?
    ("Mets le réveil à 7h demain et le week-end", [req("ask")]),
    ("Pas de réveil le samedi 10 octobre", [req("off", on_date=day("2026-10-10"))]),
    # ---- ce qu'on ne sait pas régler : on demande, on ne devine pas
    ("Mets le réveil à 5h du 12 au 15 octobre", [req("ask")]),
    ("À partir de lundi mets le réveil à 8h", [req("ask")]),
    ("A partir du 12 octobre mets le réveil à 8h", [req("ask")]),
    ("Mets le réveil à 7h du lundi au mercredi", [req("ask")]),
    ("Mets le réveil à 7h la semaine prochaine", [req("ask")]),
    ("Mets le réveil à 7h cette semaine", [req("ask")]),
    ("Mets le réveil à 7h jusqu'à vendredi", [req("ask")]),
    ("Mets le réveil à 7h tous les jours sauf le dimanche", [req("ask")]),
    ("Mets le réveil à 9h le week-end prochain", [req("ask")]),
    ("Mets le réveil à 7h ce matin", [req("ask")]),
    ("Pas de réveil pendant le tournage", [req("ask")]),
    ("Pas de réveil du 12 au 14 octobre", [req("ask")]),
]

NOT_ORDERS = [
    "Je me réveille à 7h tous les jours",
    "Demain je me lève à 6h pour le tournage.",
    "Mon réveil a sonné à 7h ce matin",
    "Mon réveil sonne à 6h30 chaque matin, c'est trop tôt",
    "Mets mon réveil dans 2 heures",
    "J'ai mis mon réveil à 6h hier",
    "Je dors 8 heures par nuit",
    "Il faut que je rende le dossier à 9h demain",
    "Le podcast sur Houdini dure 8 minutes",
    "Je voudrais un podcast sur le cinéma à 9h",
    "Le tournage commence à 6h, je dois me réveiller tôt.",
    "Mets le réveil",
    "Mets mon réveil à 25h",
    "Aujourd'hui j'ai monté la séquence d'ouverture de mon court-métrage avec Houdini.",
    "Et le week-end à 9h.",                              # pas de « réveil » et rien avant : on ne devine pas
    "Du 12 au 15 octobre, mets-le à 5h.",
    "",
]

# Un ordre suivi d'une phrase qui n'en est PAS la suite : seul le premier compte.
ONLY_THE_FIRST = [
    "Mets mon réveil à 7h. Et je finis à 18h le vendredi.",
    "Mets mon réveil à 7h. Mercredi 15h.",                               # pas de « Et » : on n'en fait pas un ordre
    "Mets mon réveil à 7h. Et demain le tournage commence à 6h.",
    "Mets mon réveil à 7h. Et le week-end, je fais le montage à 9h.",
]


class TestOrders(unittest.TestCase):
    def test_orders(self):
        for text, expected in ORDERS:
            with self.subTest(text):
                self.assertEqual(R.find_wake_requests(text, TODAY), expected)

    def test_a_sentence_that_is_not_the_continuation_of_an_order_is_ignored(self):
        for text in ONLY_THE_FIRST:
            with self.subTest(text):
                self.assertEqual(R.find_wake_requests(text, TODAY), [req("set", "07:00")])

    def test_things_that_are_not_orders_are_ignored(self):
        for text in NOT_ORDERS:
            with self.subTest(text):
                self.assertEqual(R.find_wake_requests(text, TODAY), [])

    def test_accents_case_and_punctuation_do_not_matter(self):
        for text in ("METS MON REVEIL A 6H30", "mets mon réveil à 6h30 !", "  Mets   mon réveil à 6:30  ", "mets mon reveil a 6 h 30"):
            with self.subTest(text):
                self.assertEqual(R.find_wake_requests(text, TODAY), [req("set", "06:30")])

    def test_spoken_times(self):
        cases = {
            "sept heures": "07:00", "sept heures et quart": "07:15", "sept heures et demie": "07:30",
            "sept heures moins vingt": "06:40", "huit heures quinze": "08:15", "six heures trente-cinq": "06:35",
            "minuit": "00:00", "dix-neuf heures": "19:00", "huit heures du soir": "20:00",
        }
        for spoken, hhmm in cases.items():
            with self.subTest(spoken):
                self.assertEqual(R.find_wake_requests(f"Mets mon réveil à {spoken}", TODAY), [req("set", hhmm)])

    def test_weekday_names_pick_the_next_occurrence(self):
        today_is_monday = date(2026, 10, 5)
        self.assertEqual(R.find_wake_requests("Mets le réveil à 8h lundi", today_is_monday), [req("set", "08:00", on_date=day("2026-10-12"))])
        self.assertEqual(R.find_wake_requests("Mets le réveil à 8h mardi", today_is_monday), [req("set", "08:00", on_date=day("2026-10-06"))])

    def test_this_weekend_when_it_is_already_the_weekend(self):
        saturday = date(2026, 10, 10)
        self.assertEqual(R.find_wake_requests("Mets le réveil à 9h ce week-end", saturday),
                         [req("set", "09:00", on_date=day("2026-10-10")), req("set", "09:00", on_date=day("2026-10-11"))])

    def test_date_in_the_past_rolls_to_next_year(self):
        self.assertEqual(R.find_wake_requests("Mets le réveil à 6h le 3 octobre", TODAY), [req("set", "06:00", on_date=day("2027-10-03"))])
        self.assertEqual(R.find_wake_requests("Mets le réveil à 6h le 31 février", TODAY), [req("set", "06:00")])  # date impossible : ignorée

    def test_command_arguments(self):
        cases = [
            ("", [req("show")]),
            ("6h30", [req("set", "06:30")]),
            ("6h30 semaine", [req("set", "06:30", WEEK)]),
            ("7h30 en semaine", [req("set", "07:30", WEEK)]),
            ("9h week-end", [req("set", "09:00", WEEKEND)]),
            ("demain 5h45", [req("set", "05:45", on_date=day("2026-10-08"))]),
            ("lundi 8h", [req("set", "08:00", on_date=day("2026-10-12"))]),
            ("8h30 samedi", [req("set", "08:30", on_date=day("2026-10-10"))]),
            ("7", [req("set", "07:00")]),
            ("6:15", [req("set", "06:15")]),
            ("off", [req("off")]),
            ("on", [req("on")]),
            ("pas demain", [req("off", on_date=day("2026-10-08"))]),
            ("off week-end", [req("off", days=WEEKEND)]),
            ("off lundi", [req("off", on_date=day("2026-10-12"))]),
            ("8 heures", [req("set", "08:00")]),
            ("huit heures et demie", [req("set", "08:30")]),
            ("7h30 en semaine et 9h le week-end", [req("set", "07:30", WEEK), req("set", "09:00", WEEKEND)]),
            ("6h30 semaine, 9h week-end", [req("set", "06:30", WEEK), req("set", "09:00", WEEKEND)]),
            ("7h et le week-end 9h", [req("set", "07:00"), req("set", "09:00", WEEKEND)]),
            ("7h le lundi et 9h le mardi", [req("set", "07:00", (0,)), req("set", "09:00", (1,))]),
            ("7h30 9h", [req("unclear")]),
            ("9h week-end et 7h", [req("unclear")]),
            ("7h du 12 au 15", [req("ask")]),
            ("7h cette semaine", [req("ask")]),
            ("off pendant le tournage", [req("ask")]),
            ("n'importe quoi", []),
            ("25h", []),
        ]
        for args, expected in cases:
            with self.subTest(args):
                self.assertEqual(R.parse_wake_args(args, TODAY), expected)


# =========================================================================== appliquer un ordre
class TestApply(unittest.TestCase):
    def apply(self, s, order, when=NOW, stamp=None):
        return R.apply_request(s, order, when, stamp)

    def test_set_every_day(self):
        s = fresh()
        self.assertEqual(self.apply(s, req("set", "06:30")), "⏰ Réveil tous les jours : 6 h 30.")
        self.assertTrue(s["reveil"]["actif"])
        self.assertEqual(set(s["reveil"]["jours"].values()), {"06:30"})
        self.assertEqual(s["reveil"]["maj"], "2026-10-07T21:30:00+02:00")

    def test_set_some_days_keeps_the_others(self):
        s = fresh()
        self.apply(s, req("set", "06:30"))
        self.assertEqual(self.apply(s, req("set", "09:00", WEEKEND)), "⏰ Réveil le week-end : 9 h.")
        self.assertEqual([s["reveil"]["jours"][d] for d in R.DAYS], ["06:30"] * 5 + ["09:00"] * 2)
        self.assertEqual(self.apply(s, req("set", "08:00", (0,))), "⏰ Réveil le lundi : 8 h.")
        self.assertEqual(s["reveil"]["jours"]["mar"], "06:30")
        self.assertEqual(self.apply(s, req("set", "07:00", (1, 3))), "⏰ Réveil le mardi et le jeudi : 7 h.")

    def test_one_off_leaves_the_week_alone_and_says_so(self):
        s = fresh()
        self.apply(s, req("set", "06:30"))
        message = self.apply(s, req("set", "05:45", on_date=day("2026-10-08")))
        self.assertEqual(message, "⏰ Réveil demain (jeudi 8 octobre) : 5 h 45 (les autres jours ne changent pas).")
        self.assertEqual(s["reveil"]["ponctuel"], {"2026-10-08": "05:45"})
        self.assertEqual(set(s["reveil"]["jours"].values()), {"06:30"})
        self.assertEqual(R.wake_for(s, day("2026-10-08")), "05:45")
        self.assertEqual(R.wake_for(s, day("2026-10-09")), "06:30")

    def test_one_off_works_even_when_the_alarm_is_off(self):
        s = fresh()
        self.apply(s, req("set", "06:30"))
        self.apply(s, req("off"))
        self.assertFalse(s["reveil"]["actif"])
        self.apply(s, req("set", "05:45", on_date=day("2026-10-08")))
        self.assertEqual(R.wake_for(s, day("2026-10-08")), "05:45")      # tu l'as demandé pour ce jour-là
        self.assertIsNone(R.wake_for(s, day("2026-10-09")))

    def test_set_turns_a_disabled_alarm_back_on(self):
        s = fresh()
        self.apply(s, req("set", "06:30"))
        self.apply(s, req("off"))
        self.apply(s, req("set", "07:00"))
        self.assertTrue(s["reveil"]["actif"])

    def test_off_everything_keeps_the_hours_for_later(self):
        s = fresh()
        self.apply(s, req("set", "06:30", WEEK))
        self.assertEqual(self.apply(s, req("off")), "😴 Réveil désactivé (pour le remettre : /reveil on).")
        self.assertFalse(s["reveil"]["actif"])
        self.assertEqual(s["reveil"]["jours"]["lun"], "06:30")
        self.assertEqual(self.apply(s, req("on")), "⏰ Réveil réactivé.")
        self.assertEqual(R.wake_for(s, day("2026-10-08")), "06:30")

    def test_off_some_days_and_off_one_date(self):
        s = fresh()
        self.apply(s, req("set", "06:30"))
        self.assertEqual(self.apply(s, req("off", days=WEEKEND)), "😴 Plus de réveil le week-end.")
        self.assertEqual([s["reveil"]["jours"][d] for d in ("sam", "dim")], [None, None])
        self.assertEqual(self.apply(s, req("off", on_date=day("2026-10-08"))), "😴 Pas de réveil demain (jeudi 8 octobre).")
        self.assertIsNone(R.wake_for(s, day("2026-10-08")))
        self.assertEqual(R.wake_for(s, day("2026-10-09")), "06:30")

    def test_on_without_any_hour_explains(self):
        s = fresh()
        self.assertIn("aucune heure n'est choisie", self.apply(s, req("on")))

    def test_show_changes_nothing(self):
        s = fresh()
        self.apply(s, req("set", "06:30", WEEK))
        before = json.dumps(s, sort_keys=True)
        self.assertIn("Réveil : lun–ven 6 h 30", self.apply(s, req("show")))
        self.assertEqual(json.dumps(s, sort_keys=True), before)

    def test_ask_changes_nothing(self):
        s = fresh()
        self.apply(s, req("set", "06:30", WEEK))
        before = json.dumps(s, sort_keys=True)
        self.assertEqual(self.apply(s, req("ask")), R.ASK_TEXT)
        self.assertEqual(json.dumps(s, sort_keys=True), before)
        self.assertIn("un jour à la fois", R.ASK_TEXT)

    def test_unclear_changes_nothing_either(self):
        s = fresh()
        self.apply(s, req("set", "06:30", WEEK))
        before = json.dumps(s, sort_keys=True)
        self.assertEqual(self.apply(s, req("unclear")), R.UNCLEAR_TEXT)
        self.assertEqual(json.dumps(s, sort_keys=True), before)
        self.assertIn("une heure à la fois", R.UNCLEAR_TEXT)

    def test_a_set_then_a_set_for_the_weekend_ends_up_with_two_schedules(self):
        s = fresh()
        for order in R.find_wake_requests("Mets mon réveil à 7h et le week-end à 9h", TODAY):
            self.apply(s, order)
        self.assertEqual(R.week_summary(s["reveil"]["jours"]), "lun–ven 7 h · sam–dim 9 h")

    def test_maj_is_the_moment_you_wrote_not_the_moment_it_was_read(self):
        s = fresh()
        wrote = datetime(2026, 10, 7, 19, 40, tzinfo=PARIS)
        read = datetime(2026, 10, 7, 21, 30, tzinfo=PARIS)
        self.apply(s, req("set", "06:30"), when=read, stamp=wrote)
        self.assertEqual(s["reveil"]["maj"], "2026-10-07T19:40:00+02:00")

    def test_every_change_is_a_valid_file_for_the_sonnette(self):
        s = fresh()
        for order in (req("set", "06:30"), req("set", "09:00", WEEKEND), req("set", "05:45", on_date=day("2026-10-08")),
                      req("off", days=(6,)), req("off", on_date=day("2026-10-09")), req("off"), req("on")):
            self.apply(s, order)
            self.assertEqual(R.normalize(s), s)      # rien d'invalide n'est jamais écrit

    def test_old_exceptions_are_forgotten_when_applying(self):
        s = fresh()
        s["reveil"]["ponctuel"]["2026-09-01"] = "05:00"
        self.apply(s, req("set", "06:30"))
        self.assertNotIn("2026-09-01", s["reveil"]["ponctuel"])


# =========================================================================== le podcast
class TestPodcastSettings(unittest.TestCase):
    def test_duration(self):
        cases = {"3": 3.0, "2 minutes": 2.0, "90 secondes": 1.5, "1,5": 1.5, "45 s": 1.0, "2,3 minutes": 2.5, "8": 8.0}
        for text, minutes in cases.items():
            with self.subTest(text):
                self.assertEqual(R.parse_duree(text), minutes)
        for text in ("0", "9", "10 minutes", "deux", "", "bientôt"):
            with self.subTest(text):
                self.assertIsNone(R.parse_duree(text))

    def test_voice(self):
        self.assertEqual(R.parse_voix("homme"), "fr-FR-HenriNeural")
        self.assertEqual(R.parse_voix("une voix de femme"), "fr-FR-DeniseNeural")
        self.assertEqual(R.parse_voix("Remy"), "fr-FR-RemyMultilingualNeural")
        self.assertEqual(R.parse_voix("vivienne"), "fr-FR-VivienneMultilingualNeural")
        self.assertEqual(R.parse_voix("fr-FR-EloiseNeural"), "fr-FR-EloiseNeural")
        self.assertIsNone(R.parse_voix("robot"))
        self.assertIsNone(R.parse_voix(""))

    def test_speed(self):
        cases = {"lent": "-10%", "très lent": "-20%", "tres lent": "-20%", "rapide": "+10%", "plus rapide": "+10%",
                 "très rapide": "+20%", "normal": "+0%", "+15%": "+15%", "15 %": "+15%", "0%": "+0%"}
        for text, value in cases.items():
            with self.subTest(text):
                self.assertEqual(R.parse_vitesse(text), value)
        self.assertIsNone(R.parse_vitesse("n'importe quoi"))

    def test_negative_speed_keeps_its_sign(self):
        """Régression : « - » était transformé en espace, donc « -15% » devenait « +15% »."""
        for text in ("-15%", "−15 %", "–15%"):
            with self.subTest(text):
                self.assertEqual(R.parse_vitesse(text), "-15%")
        self.assertEqual(R.parse_vitesse("-40%"), "-30%")        # plafonné
        self.assertEqual(R.parse_vitesse("+90%"), "+30%")

    def test_proposals_from_the_ai_are_validated(self):
        self.assertEqual(R.validate_setting("duree", "3"), ("minutes", 3.0))
        self.assertEqual(R.validate_setting("minutes", "2 minutes"), ("minutes", 2.0))
        self.assertEqual(R.validate_setting("vitesse", "lent"), ("vitesse", "-10%"))
        self.assertEqual(R.validate_setting("voix", "femme"), ("voix", "fr-FR-DeniseNeural"))
        for key, value in (("couleur", "bleu"), ("duree", "42"), ("voix", "robot"), ("vitesse", "vite"), ("", ""), (None, None)):
            with self.subTest((key, value)):
                self.assertIsNone(R.validate_setting(key, value))

    def test_set_podcast_messages(self):
        s = fresh()
        self.assertEqual(R.set_podcast(s, "minutes", 1.5), "durée du podcast : environ 1,5 minutes")
        self.assertEqual(R.set_podcast(s, "minutes", 1), "durée du podcast : environ 1 minute")
        self.assertEqual(R.set_podcast(s, "voix", "fr-FR-DeniseNeural"), "voix : femme")
        self.assertEqual(R.set_podcast(s, "voix", "fr-FR-RemyMultilingualNeural"), "voix : fr-FR-RemyMultilingualNeural")
        self.assertEqual(R.set_podcast(s, "vitesse", "-10%"), "vitesse de la voix : -10%")
        self.assertEqual(s["podcast"], {"minutes": 1, "voix": "fr-FR-RemyMultilingualNeural", "vitesse": "-10%"})

    def test_summary(self):
        s = fresh()
        self.assertEqual(R.podcast_summary(s), "🎙️ Podcast : environ 2 min · voix par défaut · vitesse normale")
        R.set_podcast(s, "minutes", 3)
        R.set_podcast(s, "voix", "fr-FR-DeniseNeural")
        R.set_podcast(s, "vitesse", "-10%")
        self.assertEqual(R.podcast_summary(s), "🎙️ Podcast : environ 3 min · voix femme · vitesse -10%")

    def test_settings_override_the_config_without_touching_it(self):
        cfg = {"mots_cibles": 300, "voix": {"edge_voix": "fr-FR-HenriNeural", "vitesse": "+0%"}, "autre": {"x": 1}}
        self.assertEqual(R.apply_to_cfg(cfg, fresh()), cfg)                      # rien de choisi : la config reste telle quelle
        s = fresh()
        R.set_podcast(s, "minutes", 3)
        R.set_podcast(s, "voix", "fr-FR-DeniseNeural")
        R.set_podcast(s, "vitesse", "-10%")
        out = R.apply_to_cfg(cfg, s)
        self.assertEqual((out["mots_cibles"], out["voix"]["edge_voix"], out["voix"]["vitesse"]), (450, "fr-FR-DeniseNeural", "-10%"))
        self.assertEqual(cfg["mots_cibles"], 300)                                # l'original n'a pas bougé
        self.assertEqual(cfg["voix"]["edge_voix"], "fr-FR-HenriNeural")
        self.assertEqual(out["autre"], {"x": 1})

    def test_config_without_voice_section_still_works(self):
        s = fresh()
        R.set_podcast(s, "voix", "fr-FR-DeniseNeural")
        self.assertEqual(R.apply_to_cfg({"mots_cibles": 300}, s)["voix"], {"edge_voix": "fr-FR-DeniseNeural"})


if __name__ == "__main__":
    unittest.main()
