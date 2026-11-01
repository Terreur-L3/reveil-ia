"""Tests hors ligne de l'emploi du temps : lire les cours, les ranger, les dire dans le podcast, les afficher dans /cours.

Lancer :  python -m unittest discover -s tests -t . -v
"""
from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

import yaml

from src import edt, perso

TODAY = date(2026, 10, 7)          # mercredi
MON, TUE, WED, THU, FRI, SAT, SUN = (date(2026, 10, 5 + i) for i in range(7))
NEXT_MON = date(2026, 10, 12)

COURSES = [
    {"jour": "lun", "debut": "09:00", "fin": "12:30", "intitule": "Étalonnage", "salle": "204", "enseignant": "M. Durand"},
    {"jour": "lun", "debut": "14:00", "fin": "17:00", "intitule": "Prise de vue", "salle": "Studio B", "enseignant": None},
    {"jour": "mar", "debut": "10:00", "fin": "12:00", "intitule": "Histoire du cinéma", "salle": None, "enseignant": None},
    {"jour": "jeu", "debut": "09:30", "fin": "13:00", "intitule": "Montage", "salle": "Salle 12", "enseignant": None},
]
SEEN = ["lun", "mar", "mer", "jeu", "ven"]


def filled() -> dict:
    data = edt.empty()
    edt.merge(data, COURSES, SEEN, week=None, recurring=True, today=TODAY)
    return data


class TestNormalisation(unittest.TestCase):
    def test_day_names(self):
        cases = {"Lundi 05/10": "lun", "lun.": "lun", "Monday": "lun", "MERCREDI": "mer", "jeudi": "jeu", "dim": "dim",
                 "samedi matin": "sam", "mar 6": "mar", "Fri": "ven"}
        for text, short in cases.items():
            with self.subTest(text):
                self.assertEqual(edt.norm_day(text), short)
        for text in ("xyz", None, "", 5):
            with self.subTest(repr(text)):
                self.assertIsNone(edt.norm_day(text))

    def test_times(self):
        cases = {"9h": "09:00", "9h30": "09:30", "09:00": "09:00", "9 h 30": "09:30", "9.30": "09:30", "14H": "14:00",
                 "12h45": "12:45", "de 9h à 12h": "09:00"}
        for text, hhmm in cases.items():
            with self.subTest(text):
                self.assertEqual(edt.norm_time(text), hhmm)
        for text in ("25h", "9", None, "", "8:5", "midi"):
            with self.subTest(repr(text)):
                self.assertIsNone(edt.norm_time(text))

    def test_a_course_is_cleaned(self):
        self.assertEqual(
            edt.clean_course({"jour": "Lundi", "debut": "9h", "fin": "12h30", "intitule": " Étalonnage ", "salle": "204",
                              "enseignant": "M. Durand"}),
            ("lun", {"debut": "09:00", "cours": "Étalonnage", "fin": "12:30", "salle": "204", "prof": "M. Durand"}),
        )
        # d'autres noms de clés sont acceptés, les champs facultatifs peuvent manquer
        self.assertEqual(edt.clean_course({"jour": "mar", "debut": "10h", "matiere": "Lumière"}), ("mar", {"debut": "10:00", "cours": "Lumière"}))

    def test_unusable_courses_are_dropped(self):
        for raw in ("pas un dict", None, {}, {"jour": "foo", "debut": "9h", "intitule": "Cours"},
                    {"jour": "lun", "debut": "n'importe", "intitule": "Cours"}, {"jour": "lun", "debut": "9h"},
                    {"jour": "lun", "debut": "9h", "intitule": "X"}):
            with self.subTest(repr(raw)):
                self.assertIsNone(edt.clean_course(raw))

    def test_an_end_before_the_start_is_ignored_but_the_course_stays(self):
        self.assertEqual(edt.clean_course({"jour": "lun", "debut": "14h", "fin": "9h", "intitule": "Montage"})[1],
                         {"debut": "14:00", "cours": "Montage"})

    def test_links_and_template_markers_are_removed_from_text(self):
        """Le texte d'un cours est dit dans le podcast : ni lien, ni marqueur [[PERSO]] ne doit s'y glisser."""
        _day, course = edt.clean_course({"jour": "mar", "debut": "9h", "intitule": "Cours https://evil.example/x [[PERSO]] ok",
                                         "salle": "[[PERSO]]"})
        self.assertEqual(course["cours"], "Cours PERSO ok")        # les crochets disparaissent : ce n'est plus un marqueur
        self.assertEqual(course["salle"], "PERSO")
        self.assertNotIn("[[", course["cours"] + course["salle"])
        self.assertNotIn("http", course["cours"])

    def test_long_text_is_cut(self):
        _day, course = edt.clean_course({"jour": "mar", "debut": "9h", "intitule": "x" * 500, "salle": "y" * 500, "enseignant": "z" * 500})
        self.assertEqual((len(course["cours"]), len(course["salle"]), len(course["prof"])), (80, 24, 40))


class TestFile(unittest.TestCase):
    def test_roundtrip_is_sorted_and_commented(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "memoire" / "emploi_du_temps.yaml"
            data = filled()
            data["semaine_type"]["lun"].reverse()               # désordre : l'écriture remet dans l'ordre
            edt.save(path, data)
            text = path.read_text(encoding="utf-8")
            self.assertTrue(text.startswith("# Géré par ton journal vocal"))
            self.assertIn("Étalonnage", text)                    # accents conservés (pas d'échappement É)
            self.assertEqual([c["debut"] for c in edt.load(path)["semaine_type"]["lun"]], ["09:00", "14:00"])
            self.assertEqual([p.name for p in path.parent.iterdir()], ["emploi_du_temps.yaml"])        # pas de « .part » oublié

    def test_missing_or_broken_files_give_an_empty_timetable(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(edt.load(Path(tmp) / "nope.yaml"), edt.empty())
            bad = Path(tmp) / "bad.yaml"
            bad.write_text("semaine_type: [pas fermé", encoding="utf-8")
            self.assertEqual(edt.load(bad), edt.empty())

    def test_junk_inside_a_valid_file_is_dropped(self):
        for junk in (None, 3, "x", [], {"semaine_type": 3, "dates": "x"}):
            with self.subTest(repr(junk)):
                self.assertEqual(edt.normalize(junk)["semaine_type"], {})
        data = edt.normalize({
            "mis_a_jour": "2026-10-07",
            "semaine_type": {"lun": [{"debut": "9h", "cours": "Montage"}, {"debut": "??", "cours": "Cassé"}, "texte", {"debut": "9h"}],
                             "xyz": [{"debut": "9h", "cours": "Jour inconnu"}], "mar": "oups"},
            "dates": {"2026-10-12": [{"debut": "10h", "cours": "Exception"}], "pas-une-date": [], "2026-02-30": []},
        })
        self.assertEqual(data["semaine_type"], {"lun": [{"debut": "09:00", "cours": "Montage"}], "mar": []})
        self.assertEqual(list(data["dates"]), ["2026-10-12"])

    def test_the_file_written_by_hand_is_understood(self):
        text = ("mis_a_jour: '2026-10-06'\nsemaine_type:\n  lun:\n  - {debut: '09:00', fin: '12:00', cours: Étalonnage, salle: '204', prof: M. Martin}\n"
                "  mar: []\ndates: {}\n")
        data = edt.normalize(yaml.safe_load(text))
        self.assertEqual(edt.courses_for(data, MON)[0]["prof"], "M. Martin")
        self.assertEqual(edt.courses_for(data, TUE), [])
        self.assertIsNone(edt.courses_for(data, WED))


class TestMerge(unittest.TestCase):
    def test_a_week_image_fills_the_recurring_week(self):
        data = edt.empty()
        report = edt.merge(data, COURSES, SEEN, week=None, recurring=True, today=TODAY)
        self.assertEqual(report, {"cours": 4, "jours": ["lun", "mar", "mer", "jeu", "ven"], "semaine": None, "recurrent": True})
        self.assertEqual(sorted(data["semaine_type"]), ["jeu", "lun", "mar", "mer", "ven"])
        self.assertEqual((data["semaine_type"]["mer"], data["semaine_type"]["ven"]), ([], []))       # vus, sans cours
        self.assertNotIn("sam", data["semaine_type"])                                                  # pas vu : inconnu
        self.assertEqual((data["dates"], data["mis_a_jour"]), ({}, "2026-10-07"))
        self.assertEqual(edt.count(data), 4)

    def test_days_seen_again_replace_the_old_ones_and_other_days_stay(self):
        data = filled()
        edt.merge(data, [{"jour": "lun", "debut": "10:00", "fin": "11:00", "intitule": "Nouveau cours"}], ["lun"],
                  week=None, recurring=True, today=TODAY)
        self.assertEqual([c["cours"] for c in data["semaine_type"]["lun"]], ["Nouveau cours"])        # remplacé
        self.assertEqual([c["cours"] for c in data["semaine_type"]["jeu"]], ["Montage"])              # intact

    def test_a_day_seen_empty_clears_it(self):
        data = filled()
        edt.merge(data, [], ["jeu"], week=None, recurring=True, today=TODAY)
        self.assertEqual(data["semaine_type"]["jeu"], [])

    def test_this_week_only_goes_to_dates_and_leaves_the_recurring_week_alone(self):
        data = filled()
        report = edt.merge(data, [{"jour": "lun", "debut": "10:00", "fin": "11:00", "intitule": "Exceptionnel"}], ["lun"],
                           week=NEXT_MON, recurring=False, today=TODAY)
        self.assertEqual((report["recurrent"], report["semaine"]), (False, NEXT_MON))
        self.assertEqual([c["cours"] for c in edt.courses_for(data, NEXT_MON)], ["Exceptionnel"])
        self.assertEqual([c["cours"] for c in edt.courses_for(data, MON)], ["Étalonnage", "Prise de vue"])  # la semaine type est intacte
        self.assertEqual(list(data["dates"]), ["2026-10-12"])

    def test_recurring_with_a_week_writes_both(self):
        data = edt.empty()
        edt.merge(data, COURSES, SEEN, week=MON, recurring=True, today=TODAY)
        self.assertEqual(len(data["dates"]), 5)
        self.assertEqual(edt.count(data), 4)
        self.assertEqual(edt.courses_for(data, THU)[0]["cours"], "Montage")

    def test_a_dated_day_wins_over_the_recurring_week(self):
        data = filled()
        data["dates"]["2026-10-08"] = []                       # jeudi 8 : pas cours cette semaine-là
        self.assertEqual(edt.courses_for(data, THU), [])
        self.assertEqual(edt.courses_for(data, date(2026, 10, 15))[0]["cours"], "Montage")             # la semaine suivante : normal

    def test_garbage_courses_are_skipped_and_counted_correctly(self):
        data = edt.empty()
        report = edt.merge(data, COURSES[:1] + [{"jour": "?", "debut": "9h", "intitule": "Cassé"}, "x", None], ["lun"],
                           week=None, recurring=True, today=TODAY)
        self.assertEqual(report["cours"], 1)

    def test_nothing_usable_reports_zero_courses(self):
        data = edt.empty()
        report = edt.merge(data, [{"jour": "?", "debut": "9h", "intitule": "Cassé"}], [], week=None, recurring=True, today=TODAY)
        self.assertEqual((report["cours"], report["jours"]), (0, []))

    def test_courses_are_capped(self):
        many = [{"jour": "lun", "debut": f"{6 + i // 6:02d}:{i % 6 * 10:02d}", "intitule": f"Cours numéro {i}"} for i in range(100)]
        data = edt.empty()
        report = edt.merge(data, many, ["lun"], week=None, recurring=True, today=TODAY)
        self.assertEqual(report["cours"], edt.MAX_COURSES)
        self.assertEqual(len(data["semaine_type"]["lun"]), edt.MAX_COURSES)

    def test_old_dated_weeks_are_forgotten(self):
        data = filled()
        data["dates"].update({"2026-08-01": [], "2026-09-28": [], "2026-10-12": []})
        edt.merge(data, [], [], week=None, recurring=True, today=TODAY)
        self.assertEqual(sorted(data["dates"]), ["2026-09-28", "2026-10-12"])                        # 14 jours de mémoire

    def test_known_count_and_clear(self):
        data = filled()
        self.assertTrue(edt.known(data))
        self.assertFalse(edt.known(edt.empty()))
        edt.clear(data)
        self.assertEqual((edt.known(data), edt.count(data), data["mis_a_jour"]), (False, 0, ""))


class TestWeekResolution(unittest.TestCase):
    def test_school_week_is_the_coming_week_on_sundays(self):
        self.assertEqual(edt.school_week(MON), MON)
        self.assertEqual(edt.school_week(SAT), MON)
        self.assertEqual(edt.school_week(SUN), NEXT_MON)                  # le dimanche, la semaine de cours est la suivante
        self.assertEqual(edt.monday_of(SUN), MON)                         # (le lundi « civil », lui, ne bouge pas)

    def test_dates_give_the_monday_of_their_week(self):
        for text, monday in (("2026-10-05", MON), ("2026-10-09", MON), ("05/10", MON), ("07/10/2026", MON), ("07/10/26", MON),
                             ("semaine du 12.10", NEXT_MON), ("12-10", NEXT_MON)):
            with self.subTest(text):
                self.assertEqual(edt.resolve_week(text, TODAY), monday)

    def test_far_or_impossible_dates_are_refused(self):
        for text in ("2025-01-01", "2030-01-01", "", None, "31/02", "bientôt", "99/99"):
            with self.subTest(repr(text)):
                self.assertIsNone(edt.resolve_week(text, TODAY))

    def test_year_end_picks_the_closest_year(self):
        self.assertEqual(edt.resolve_week("04/01", date(2026, 12, 30)), date(2027, 1, 4))
        self.assertEqual(edt.resolve_week("28/12", date(2027, 1, 2)), date(2026, 12, 28))


class TestSpoken(unittest.TestCase):
    """Ce que le podcast dit des cours du jour (texte fabriqué par des modèles de phrases, jamais par une IA)."""

    def test_several_courses(self):
        self.assertEqual(
            edt.spoken(filled(), MON),
            "Côté cours, aujourd'hui : Étalonnage de 9 h à 12 h 30, en salle 204, puis Prise de vue de 14 h à 17 h, en Studio B.",
        )

    def test_one_course(self):
        self.assertEqual(edt.spoken(filled(), TUE), "Côté cours, aujourd'hui : Histoire du cinéma de 10 h à 12 h.")
        self.assertEqual(edt.spoken(filled(), THU), "Côté cours, aujourd'hui : Montage de 9 h 30 à 13 h, en salle 12.")

    def test_a_course_without_an_end(self):
        data = edt.empty()
        edt.merge(data, [{"jour": "lun", "debut": "9h30", "intitule": "Rendez-vous production"}], ["lun"], week=None, recurring=True, today=TODAY)
        self.assertEqual(edt.spoken(data, MON), "Côté cours, aujourd'hui : Rendez-vous production à 9 h 30.")

    def test_a_busy_day_is_summarised_instead_of_listed(self):
        many = [{"jour": "lun", "debut": f"{h:02d}:00", "fin": f"{h:02d}:50", "intitule": f"Cours {h}", "salle": "A1" if h == 8 else None}
                for h in (8, 9, 10, 11, 14)]
        data = edt.empty()
        edt.merge(data, many, ["lun"], week=None, recurring=True, today=TODAY)
        self.assertEqual(edt.spoken(data, MON), "Aujourd'hui, tu as 5 cours, de 8 h à 14 h 50. Le premier : Cours 8 à 8 h, en salle A1.")

    def test_free_weekday_and_unknown_days(self):
        self.assertEqual(edt.spoken(filled(), WED), "Pas de cours aujourd'hui.")       # vu, sans cours
        self.assertEqual(edt.spoken(filled(), SAT), "")                                # pas vu : on ne dit rien
        self.assertEqual(edt.spoken(edt.empty(), MON), "")
        data = filled()
        data["semaine_type"]["sam"] = []
        self.assertEqual(edt.spoken(data, SAT), "")                                    # samedi vide : inutile de le dire

    def test_sunday_announces_monday(self):
        self.assertEqual(edt.spoken(filled(), SUN), "Demain, ça reprend : premier cours à 9 h, Étalonnage.")
        data = filled()
        data["semaine_type"]["lun"] = []
        self.assertEqual(edt.spoken(data, SUN), "")

    def test_a_dated_exception_is_what_gets_said(self):
        data = filled()
        edt.merge(data, [{"jour": "lun", "debut": "11:00", "fin": "12:00", "intitule": "Visite exceptionnelle"}], ["lun"],
                  week=NEXT_MON, recurring=False, today=TODAY)
        self.assertIn("Visite exceptionnelle", edt.spoken(data, NEXT_MON))
        self.assertIn("Étalonnage", edt.spoken(data, MON))

    def test_room_phrasing(self):
        cases = {"204": "en salle 204", "Salle 12": "en salle 12", "salle 204": "en salle 204", "S. 204": "en salle 204",
                 "S204": "en salle S204", "B12": "en salle B12", "Studio B": "en Studio B", "Amphi A": "en Amphi A",
                 "Salle de montage": "en Salle de montage", "Salle B3": "en salle B3", "": ""}
        for room, spoken in cases.items():
            with self.subTest(room):
                self.assertEqual(edt._room_spoken(room), spoken)


class TestDisplay(unittest.TestCase):
    """Ce que le bot répond à /cours."""

    def test_unknown_timetable_explains_how_to_fill_it(self):
        text = edt.week_text(edt.empty(), TODAY)
        self.assertIn("Je ne connais pas encore ton emploi du temps", text)
        self.assertIn("en tant que fichier", text)
        self.assertIn("/cours lundi", text)

    def test_the_week_is_shown_day_by_day(self):
        text = edt.week_text(filled(), TODAY)
        self.assertIn("semaine du 5 octobre (mis à jour le 07/10)", text)
        self.assertIn("lundi 5 : 9h–12h30 Étalonnage (204, M. Durand) · 14h–17h Prise de vue (Studio B)", text)
        self.assertIn("👉 mercredi 7 : pas de cours", text)                       # aujourd'hui est marqué
        self.assertIn("jeudi 8 : 9h30–13h Montage (Salle 12)", text)
        self.assertNotIn("samedi", text)                                           # week-end inconnu : pas affiché
        self.assertIn("/cours effacer", text)

    def test_on_sunday_the_coming_week_is_shown(self):
        text = edt.week_text(filled(), SUN)
        self.assertIn("semaine du 12 octobre", text)
        self.assertIn("lundi 12 : 9h–12h30 Étalonnage", text)
        self.assertNotIn("👉", text)

    def test_weekend_days_with_courses_are_shown(self):
        data = filled()
        edt.merge(data, [{"jour": "sam", "debut": "10:00", "fin": "12:00", "intitule": "Tournage"}], ["sam"], week=None, recurring=True, today=TODAY)
        self.assertIn("samedi 10 : 10h–12h Tournage", edt.week_text(data, TODAY))

    def test_a_known_timetable_with_nothing_for_this_week(self):
        data = edt.empty()
        edt.merge(data, [{"jour": "mar", "debut": "10h", "intitule": "Cours"}], ["mar"], week=date(2026, 9, 28), recurring=False, today=TODAY)
        self.assertTrue(edt.known(data))                     # une semaine passée encore en mémoire, rien pour celle-ci
        self.assertIn("rien de connu pour cette semaine", edt.week_text(data, TODAY))

    def test_day_text(self):
        data = filled()
        self.assertEqual(edt.day_text(data, TUE, TODAY), "mardi 6 : 10h–12h Histoire du cinéma")
        self.assertEqual(edt.day_text(data, WED, TODAY), "👉 mercredi 7 : pas de cours")
        self.assertIsNone(edt.day_text(data, SAT, TODAY))


class TestPodcastSegment(unittest.TestCase):
    """Les cours entrent dans le segment perso du podcast, dit par le programme."""

    def project(self, tmp: str, data: dict | None) -> Path:
        racine = Path(tmp)
        (racine / "memoire").mkdir()
        if data is not None:
            edt.save(racine / "memoire" / "emploi_du_temps.yaml", data)
        return racine

    def test_courses_follow_birthdays_and_reminders(self):
        with tempfile.TemporaryDirectory() as tmp:
            racine = self.project(tmp, filled())
            (racine / "memoire" / "anniversaires.yaml").write_text(
                yaml.safe_dump([{"nom": "Karim", "jour": 6, "mois": 10}]), encoding="utf-8")
            text, n = perso.build_segment(racine, {}, MON)                  # lundi 5 : l'anniversaire de Karim est demain
            self.assertEqual(n, 2)
            self.assertIn("Karim", text)
            self.assertLess(text.index("Karim"), text.index("Étalonnage"))

    def test_no_timetable_changes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(perso.build_segment(self.project(tmp, None), {}, MON), ("", 0))

    def test_courses_can_be_switched_off(self):
        with tempfile.TemporaryDirectory() as tmp:
            racine = self.project(tmp, filled())
            self.assertEqual(perso.build_segment(racine, {"cours": False}, MON), ("", 0))
            self.assertEqual(perso.build_segment(racine, {"max_lignes": 0}, MON), ("", 0))      # coupe tout le segment perso

    def test_a_broken_file_never_blocks_the_podcast(self):
        with tempfile.TemporaryDirectory() as tmp:
            racine = self.project(tmp, None)
            (racine / "memoire" / "emploi_du_temps.yaml").write_text("semaine_type: [pas fermé", encoding="utf-8")
            self.assertEqual(perso.build_segment(racine, {}, MON), ("", 0))


# =========================================================================== les redites : un cours dit une seule fois, la salle seulement si elle apprend quelque chose
def course(day, start, end, title, room=None):
    return {"jour": day, "debut": start, "fin": end, "intitule": title, "salle": room, "enseignant": None}


def timetable(courses, seen=("lun",)) -> dict:
    data = edt.empty()
    edt.merge(data, courses, list(seen), week=None, recurring=True, today=TODAY)
    return data


# Une semaine où presque tout se passe dans la même salle, sauf le vendredi.
HABITUAL_WEEK = [
    course("lun", "09:00", "12:00", "Étalonnage", "204"), course("mar", "09:00", "12:00", "Montage", "Salle 204"),
    course("mer", "09:00", "12:00", "Cadrage", "204"), course("jeu", "09:00", "12:00", "Histoire du cinéma", "204"),
    course("ven", "09:00", "12:00", "Prise de vue", "Studio B"),
]
WEEK_DAYS = ("lun", "mar", "mer", "jeu", "ven")


class TestTheSameCourseIsSaidOnce(unittest.TestCase):
    """« Lumière de 9 h à 12 h 30, puis Lumière de 14 h à 17 h 30 » : c'est le même cours, il est dit une seule fois."""

    def test_morning_and_afternoon_of_the_same_course(self):
        data = timetable([course("lun", "09:00", "12:30", "Lumière", "Ma classe"), course("lun", "14:00", "17:30", "Lumière", "Ma classe")])
        self.assertEqual(edt.spoken(data, MON), "Côté cours, aujourd'hui : Lumière de 9 h à 17 h 30.")

    def test_the_lunch_break_does_not_cut_a_course_but_a_long_gap_does(self):
        short = timetable([course("lun", "09:00", "12:00", "Lumière"), course("lun", "14:30", "17:00", "Lumière")])       # 2 h 30
        self.assertEqual(edt.spoken(short, MON), "Côté cours, aujourd'hui : Lumière de 9 h à 17 h.")
        long = timetable([course("lun", "09:00", "11:00", "Lumière"), course("lun", "15:00", "17:00", "Lumière")])        # 4 h
        self.assertEqual(edt.spoken(long, MON), "Côté cours, aujourd'hui : Lumière de 9 h à 11 h, puis de 15 h à 17 h.")

    def test_back_to_back_pieces_make_one_course(self):
        data = timetable([course("lun", "09:00", "10:30", "Lumière"), course("lun", "10:30", "12:00", "Lumière")])
        self.assertEqual(edt.spoken(data, MON), "Côté cours, aujourd'hui : Lumière de 9 h à 12 h.")

    def test_an_exact_duplicate_is_one_course(self):
        data = timetable([course("lun", "09:00", "12:00", "Lumière", "204"), course("lun", "09:00", "12:00", "Lumière", "204")])
        self.assertEqual(edt.spoken(data, MON), "Côté cours, aujourd'hui : Lumière de 9 h à 12 h, en salle 204.")

    def test_matin_and_apres_midi_written_in_the_title_do_not_split_the_course(self):
        for morning, afternoon in (("Lumière (matin)", "Lumière (après-midi)"), ("Matin : Lumière", "Après-midi : Lumière"),
                                   ("Lumière - matin", "Lumière - après-midi")):
            with self.subTest(morning):
                data = timetable([course("lun", "09:00", "12:00", morning), course("lun", "13:30", "17:00", afternoon)])
                self.assertEqual(edt.spoken(data, MON), "Côté cours, aujourd'hui : Lumière de 9 h à 17 h.")

    def test_without_end_times_the_title_is_still_said_once(self):
        data = timetable([course("lun", "09:00", None, "Lumière"), course("lun", "14:00", None, "Lumière")])
        self.assertEqual(edt.spoken(data, MON), "Côté cours, aujourd'hui : Lumière à 9 h, puis à 14 h.")

    def test_a_piece_without_an_end_is_never_merged_into_a_range(self):
        data = timetable([course("lun", "09:00", "12:00", "Lumière"), course("lun", "14:00", None, "Lumière")])
        self.assertEqual(edt.spoken(data, MON), "Côté cours, aujourd'hui : Lumière de 9 h à 12 h, puis à 14 h.")

    def test_different_courses_stay_different(self):
        data = timetable([course("lun", "09:00", "12:00", "Lumière"), course("lun", "14:00", "17:00", "Montage")])
        self.assertEqual(edt.spoken(data, MON), "Côté cours, aujourd'hui : Lumière de 9 h à 12 h, puis Montage de 14 h à 17 h.")

    def test_another_course_in_between_keeps_the_pieces_apart(self):
        data = timetable([course("lun", "09:00", "11:00", "Lumière"), course("lun", "11:00", "13:00", "Montage"),
                          course("lun", "14:00", "17:00", "Lumière")])
        self.assertEqual(edt.spoken(data, MON), "Côté cours, aujourd'hui : Lumière de 9 h à 11 h, puis Montage de 11 h à 13 h, "
                                                "puis Lumière de 14 h à 17 h.")

    def test_the_same_title_in_two_different_rooms_is_two_courses(self):
        data = timetable([course("lun", "09:00", "12:00", "Lumière", "204"), course("lun", "14:00", "17:00", "Lumière", "Studio B")])
        self.assertEqual(edt.spoken(data, MON),
                         "Côté cours, aujourd'hui : Lumière de 9 h à 12 h, en salle 204, puis Lumière de 14 h à 17 h, en Studio B.")

    def test_a_missing_or_generic_room_does_not_prevent_the_merge(self):
        data = timetable([course("lun", "09:00", "12:00", "Lumière", None), course("lun", "14:00", "17:00", "Lumière", "204")])
        self.assertEqual(edt.spoken(data, MON), "Côté cours, aujourd'hui : Lumière de 9 h à 17 h, en salle 204.")

    def test_the_busy_day_summary_counts_courses_not_slots(self):
        six_slots = []
        for title, (a, b) in (("Lumière", ("08:00", "10:00")), ("Montage", ("10:00", "12:00")), ("Cadrage", ("14:00", "16:00"))):
            six_slots += [course("lun", a, b, title), course("lun", "17:00" if title == "Cadrage" else a, "18:00" if title == "Cadrage" else b, title)]
        data = timetable(six_slots)
        text = edt.spoken(data, MON)
        self.assertTrue(text.startswith("Côté cours, aujourd'hui : "), text)             # 3 cours : listés, pas résumés
        self.assertNotIn("tu as", text)
        many = [course("lun", f"{h:02d}:00", f"{h:02d}:50", f"Cours {h}") for h in (8, 9, 10, 11, 14)]
        self.assertTrue(edt.spoken(timetable(many), MON).startswith("Aujourd'hui, tu as 5 cours"))

    def test_the_summary_uses_the_clean_title_and_one_room_rule(self):
        many = [course("lun", f"{h:02d}:00", f"{h:02d}:50", f"Cours {h} (matin)", "A1") for h in (8, 9, 10, 11, 14)]
        text = edt.spoken(timetable(many), MON, "toujours")
        self.assertEqual(text, "Aujourd'hui, tu as 5 cours, de 8 h à 14 h 50. Le premier : Cours 8 à 8 h, en salle A1.")

    def test_sunday_announces_the_clean_title(self):
        data = timetable([course("lun", "09:00", "12:00", "Lumière (matin)")])
        self.assertEqual(edt.spoken(data, SUN), "Demain, ça reprend : premier cours à 9 h, Lumière.")

    def test_the_titles_given_to_the_anchors_are_the_spoken_ones(self):
        data = timetable([course("lun", "09:00", "12:00", "Lumière (matin)"), course("lun", "13:30", "17:00", "Lumière (après-midi)"),
                          course("lun", "17:00", "18:00", "Montage")])
        text, titles = edt.spoken_detail(data, MON)
        self.assertEqual(titles, ["Lumière", "Montage"])
        self.assertIn("Lumière de 9 h à 17 h", text)

    def test_clean_title(self):
        cases = {"Lumière (matin)": "Lumière", "Lumière (le matin)": "Lumière", "Après-midi : Lumière": "Lumière",
                 "Matin - Lumière": "Lumière", "Montage – après-midi": "Montage", "Histoire du cinéma": "Histoire du cinéma",
                 "Matin": "Matin", "Réveil matin": "Réveil matin", "Atelier du matin": "Atelier du matin", "": ""}
        for raw, expected in cases.items():
            with self.subTest(raw):
                self.assertEqual(edt.clean_title(raw), expected)


class TestTheRoomIsSaidOnlyWhenItTellsYouSomething(unittest.TestCase):
    """« en ma classe » tous les jours n'apprend rien : la salle n'est dite que quand elle change."""

    def test_generic_labels_are_not_rooms(self):
        for room in ("Ma classe", "ma classe", "En classe", "Salle de classe", "Dans ma classe", "Classe", "Ta classe"):
            with self.subTest(room):
                self.assertTrue(edt.is_generic_room(room))
        for room in ("Classe 2B", "204", "Studio B", "Salle de montage", "Amphi A", "Visio", ""):
            with self.subTest(room):
                self.assertFalse(edt.is_generic_room(room))

    def test_my_class_is_never_said_in_any_mode(self):
        data = timetable([course("lun", "09:00", "12:00", "Lumière", "Ma classe"), course("lun", "14:00", "17:00", "Montage", "Ma classe")])
        for mode in ("auto", "toujours", "jamais"):
            with self.subTest(mode):
                self.assertEqual(edt.spoken(data, MON, mode),
                                 "Côté cours, aujourd'hui : Lumière de 9 h à 12 h, puis Montage de 14 h à 17 h.")

    def test_the_usual_room_is_not_repeated_but_a_different_one_is(self):
        data = timetable(HABITUAL_WEEK, WEEK_DAYS)
        self.assertEqual(edt.usual_room(data), "204")                          # « Salle 204 » et « 204 » sont la même salle
        self.assertEqual(edt.spoken(data, MON), "Côté cours, aujourd'hui : Étalonnage de 9 h à 12 h.")
        self.assertEqual(edt.spoken(data, TUE), "Côté cours, aujourd'hui : Montage de 9 h à 12 h.")
        self.assertEqual(edt.spoken(data, date(2026, 10, 9)), "Côté cours, aujourd'hui : Prise de vue de 9 h à 12 h, en Studio B.")

    def test_a_dated_exception_in_another_room_is_said(self):
        data = timetable(HABITUAL_WEEK, WEEK_DAYS)
        edt.merge(data, [course("lun", "10:00", "12:00", "Visite", "Amphi A")], ["lun"], week=NEXT_MON, recurring=False, today=TODAY)
        self.assertEqual(edt.spoken(data, NEXT_MON), "Côté cours, aujourd'hui : Visite de 10 h à 12 h, en Amphi A.")

    def test_rooms_that_vary_are_all_said(self):
        self.assertEqual(edt.usual_room(filled()), "")
        self.assertIn("en salle 204", edt.spoken(filled(), MON))
        self.assertIn("en Studio B", edt.spoken(filled(), MON))

    def test_too_few_courses_to_know_the_usual_room(self):
        data = timetable([course("lun", "09:00", "12:00", "Lumière", "204"), course("lun", "14:00", "17:00", "Montage", "204")])
        self.assertEqual(edt.usual_room(data), "")
        self.assertEqual(edt.spoken(data, MON),
                         "Côté cours, aujourd'hui : Lumière de 9 h à 12 h, en salle 204, puis Montage de 14 h à 17 h, en salle 204.")

    def test_a_room_that_is_only_a_bit_more_common_is_not_the_usual_one(self):
        data = timetable([course(d, "09:00", "12:00", f"Cours {i}", room) for i, (d, room) in
                          enumerate([("lun", "204"), ("mar", "204"), ("mer", "Studio B"), ("jeu", "Studio B"), ("ven", "12")])], WEEK_DAYS)
        self.assertEqual(edt.usual_room(data), "")                             # 2 sur 5 : pas « presque tous »

    def test_the_three_modes(self):
        data = timetable(HABITUAL_WEEK, WEEK_DAYS)
        self.assertEqual(edt.spoken(data, MON, "toujours"), "Côté cours, aujourd'hui : Étalonnage de 9 h à 12 h, en salle 204.")
        self.assertEqual(edt.spoken(data, date(2026, 10, 9), "toujours"), "Côté cours, aujourd'hui : Prise de vue de 9 h à 12 h, en Studio B.")
        self.assertEqual(edt.spoken(data, date(2026, 10, 9), "jamais"), "Côté cours, aujourd'hui : Prise de vue de 9 h à 12 h.")
        self.assertEqual(edt.spoken(data, MON, "n'importe quoi"), edt.spoken(data, MON, "auto"))
        self.assertEqual(edt.ROOM_MODES, ("auto", "toujours", "jamais"))

    def test_the_default_is_auto(self):
        data = timetable(HABITUAL_WEEK, WEEK_DAYS)
        self.assertEqual(edt.spoken(data, MON), edt.spoken(data, MON, "auto"))
        self.assertEqual(edt.spoken_detail(data, MON)[0], edt.spoken(data, MON))


class TestDaysWithoutCourses(unittest.TestCase):
    """« mardi pas de cours » : une phrase qui ne dit que ça est lue par des règles, sans IA."""

    def test_the_phrases_that_are_understood(self):
        cases = {
            "mardi pas de cours": ["mar"], "pas de cours le mardi": ["mar"], "le mercredi et le vendredi : libre": ["mer", "ven"],
            "pas de cours le week-end": ["sam", "dim"], "pas de cours le weekend": ["sam", "dim"], "mardis rien": ["mar"],
            "il n'y a pas de cours le jeudi": ["jeu"], "mardi pas de cours cette semaine seulement": ["mar"],
            "Lundi, mardi : aucun cours": ["lun", "mar"], "mon mardi est libre": ["mar"],
        }
        for text, days in cases.items():
            with self.subTest(text):
                self.assertEqual(edt.parse_free_days(text), days)

    def test_everything_else_is_a_timetable_to_read(self):
        for text in ("lundi 9h-12h étalonnage ; mardi pas de cours", "lundi atelier libre", "pas de cours", "libre", "",
                     "pas de cours car grève mardi", "tous les jours pas de cours", "mardi 9h pas de cours", "lundi salle 204 libre",
                     "bonjour tout le monde"):
            with self.subTest(text):
                self.assertIsNone(edt.parse_free_days(text))

    def test_a_free_day_is_said_as_such_and_an_unknown_day_stays_silent(self):
        data = filled()
        edt.merge(data, [], ["jeu"], week=None, recurring=True, today=TODAY)
        self.assertEqual(data["semaine_type"]["jeu"], [])
        self.assertEqual(edt.spoken(data, THU), "Pas de cours aujourd'hui.")
        self.assertIn("Étalonnage", edt.spoken(data, MON))                       # les autres jours ne bougent pas

    def test_the_help_line_lists_what_can_be_changed(self):
        text = edt.week_text(filled(), TODAY)
        for command in ("/cours lundi 9h-17h", "/cours mardi pas de cours", "/cours salle jamais", "/cours effacer"):
            self.assertIn(command, text)


class TestPersoUsesTheNewText(unittest.TestCase):
    def project(self, tmp, courses) -> Path:
        racine = Path(tmp)
        (racine / "memoire").mkdir()
        edt.save(racine / "memoire" / "emploi_du_temps.yaml", courses)
        return racine

    def test_the_podcast_says_the_course_once_and_without_my_class(self):
        data = timetable([course("lun", "09:00", "12:30", "Lumière", "Ma classe"), course("lun", "14:00", "17:30", "Lumière", "Ma classe")])
        with tempfile.TemporaryDirectory() as tmp:
            segment = perso.build_segment_detail(self.project(tmp, data), {}, MON)
        self.assertEqual(segment.text, "Côté cours, aujourd'hui : Lumière de 9 h à 17 h 30.")
        self.assertEqual(segment.anchors, [("cours", "Lumière")])               # un seul mot-clé à garder, pas deux

    def test_the_room_setting_of_the_config_reaches_the_text(self):
        data = timetable(HABITUAL_WEEK, WEEK_DAYS)
        friday = date(2026, 10, 9)
        with tempfile.TemporaryDirectory() as tmp:
            racine = self.project(tmp, data)
            self.assertEqual(perso.build_segment(racine, {}, friday)[0], "Côté cours, aujourd'hui : Prise de vue de 9 h à 12 h, en Studio B.")
            self.assertEqual(perso.build_segment(racine, {"salle": "jamais"}, friday)[0], "Côté cours, aujourd'hui : Prise de vue de 9 h à 12 h.")
            self.assertEqual(perso.build_segment(racine, {"salle": "toujours"}, MON)[0], "Côté cours, aujourd'hui : Étalonnage de 9 h à 12 h, en salle 204.")
            self.assertEqual(perso.build_segment(racine, {"salle": None}, MON)[0], "Côté cours, aujourd'hui : Étalonnage de 9 h à 12 h.")
            self.assertEqual(perso.build_segment(racine, {"salle": " JAMAIS "}, MON)[0], "Côté cours, aujourd'hui : Étalonnage de 9 h à 12 h.")


if __name__ == "__main__":
    unittest.main()
