"""Tests hors ligne du podcast : actus, IA, tri, infos perso, texte, bout en bout.

Lancer :  python -m unittest discover -s tests -t . -v
Aucun accès réseau : un faux serveur local imite les flux RSS, Gemini et Groq.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

import yaml

from src import llm, main, news, perso, script_writer, triage, tts
from tests.fakes import World

ROOT = Path(__file__).resolve().parent.parent
PARIS = ZoneInfo("Europe/Paris")


class WorldCase(unittest.TestCase):
    """Base : un faux internet local + un dossier de projet temporaire."""

    def setUp(self):
        self.world = World().start()
        self.addCleanup(self.world.stop)
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        for target, attr, value in [
            (llm, "GEMINI_URL", self.world.base + "/gemini/{model}:generateContent"),
            (llm, "GROQ_URL", self.world.base + "/groq/chat/completions"),
        ]:
            patcher = mock.patch.object(target, attr, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        sleeper = mock.patch("time.sleep")
        sleeper.start()
        self.addCleanup(sleeper.stop)
        self.keys = {"gemini": "g-key", "groq": "q-key"}
        self.now = datetime(2026, 10, 4, 5, 30, tzinfo=PARIS)
        self.cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))

    # ----- aides
    def use_feeds(self, feeds: dict[str, tuple[str, list[dict] | int]]) -> None:
        self.cfg["actu"]["flux"] = []
        for name, (categorie, data) in feeds.items():
            self.world.feeds[name] = data
            self.cfg["actu"]["flux"].append({"nom": name, "categorie": categorie, "url": self.world.feed_url(name)})

    def project(self) -> Path:
        racine = self.tmp / "projet"
        (racine / "memoire").mkdir(parents=True)
        shutil.copy(ROOT / "moi.yaml", racine / "moi.yaml")
        return racine


# =========================================================================== actus
class TestNews(WorldCase):
    def test_fetch_filters_old_dedupes_numbers_and_reports(self):
        self.world.feeds["a"] = [
            {"title": "Alpha un", "age_h": 1}, {"title": "Alpha deux", "age_h": 2},
            {"title": "Vieux", "age_h": 100}, {"title": "Alpha trois", "age_h": 3},
        ]
        self.world.feeds["b"] = [{"title": "ALPHA UN", "age_h": 1}, {"title": "Beta deux", "age_h": 2}]
        self.world.feeds["c"] = 500
        cfg = {
            "age_max_heures": 36, "max_par_flux": 3, "max_total": 10,
            "flux": [{"nom": n, "categorie": "tech", "url": self.world.feed_url(n)} for n in "abc"],
        }
        items, statuses = news.fetch(cfg)
        self.assertEqual([i.title for i in items], ["Alpha un", "Alpha deux", "Beta deux", "Alpha trois"])
        self.assertEqual([i.id for i in items], [1, 2, 3, 4])
        by_name = {s.nom: s for s in statuses}
        self.assertTrue(by_name["a"].ok)
        self.assertEqual((by_name["a"].recents, by_name["a"].total), (3, 4))
        self.assertFalse(by_name["c"].ok)
        self.assertIn("HTTP 500", by_name["c"].line())

    def test_summary_html_is_cleaned_but_comparisons_survive(self):
        self.world.feeds["a"] = [
            {"title": "Titre un", "summary": "&lt;p&gt;Salut &amp;amp; bienvenue&lt;/p&gt;"},
            {"title": "Titre deux", "summary": "<p>Le prix : a < b pour tous</p>"},
        ]
        items, _ = news.fetch({"flux": [{"nom": "a", "url": self.world.feed_url("a")}]})
        summaries = {i.title: i.summary for i in items}
        self.assertEqual(summaries["Titre un"], "Salut & bienvenue")
        self.assertEqual(summaries["Titre deux"], "Le prix : a < b pour tous")

    def test_no_feed_configured(self):
        self.assertEqual(news.fetch({"flux": []}), ([], []))


# =========================================================================== IA
class TestLLM(WorldCase):
    def chain(self, *pairs):
        return [{"fournisseur": p, "modele": m} for p, m in pairs]

    def test_falls_back_to_next_model_on_server_error(self):
        self.world.gemini_fn = lambda req, model, body: (503, "overloaded") if model == "m1" else (200, "bonjour")
        text, label = llm.generate("p", system="s", chain=self.chain(("gemini", "m1"), ("gemini", "m2")), keys=self.keys)
        self.assertEqual((text, label), ("bonjour", "gemini/m2"))
        self.assertEqual(len(self.world.calls("/gemini/m1")), 3)

    def test_provider_without_key_is_skipped(self):
        self.world.groq_fn = lambda req, model, body: (200, "depuis groq")
        keys = {"gemini": "", "groq": "q-key"}
        text, label = llm.generate("p", system="s", chain=self.chain(("gemini", "m1"), ("groq", "x")), keys=keys)
        self.assertEqual((text, label), ("depuis groq", "groq/x"))
        self.assertEqual(self.world.calls("/gemini/"), [])

    def test_refused_key_abandons_the_provider(self):
        self.world.gemini_fn = lambda req, model, body: (400, "API key not valid. Please pass a valid API key.")
        self.world.groq_fn = lambda req, model, body: (200, "ok groq")
        chain = self.chain(("gemini", "m1"), ("gemini", "m2"), ("groq", "x"))
        text, label = llm.generate("p", system="s", chain=chain, keys=self.keys)
        self.assertEqual(label, "groq/x")
        self.assertEqual(len(self.world.calls("/gemini/")), 1)  # m2 n'a même pas été tenté

    def test_rate_limit_waits_then_retries_same_model(self):
        calls = []

        def fn(req, model, body):
            calls.append(model)
            return (429, "slow down") if len(calls) == 1 else (200, "ça passe")

        self.world.gemini_fn = fn
        text, label = llm.generate("p", system="s", chain=self.chain(("gemini", "m1")), keys=self.keys)
        self.assertEqual((text, label, calls), ("ça passe", "gemini/m1", ["m1", "m1"]))

    def test_zero_quota_goes_straight_to_next_model(self):
        self.world.gemini_fn = lambda req, model, body: (
            (429, "Quota exceeded, limit: 0") if model == "m1" else (200, "ok")
        )
        _, label = llm.generate("p", system="s", chain=self.chain(("gemini", "m1"), ("gemini", "m2")), keys=self.keys)
        self.assertEqual(label, "gemini/m2")
        self.assertEqual(len(self.world.calls("/gemini/m1")), 1)

    def test_everything_fails_raises_with_every_reason(self):
        self.world.gemini_fn = lambda req, model, body: (500, "boum")
        with self.assertRaises(llm.LLMError) as ctx:
            llm.generate("p", system="s", chain=self.chain(("gemini", "m1"), ("groq", "x")), keys={"gemini": "k", "groq": ""})
        message = str(ctx.exception)
        self.assertIn("gemini/m1", message)
        self.assertIn("GROQ_API_KEY", message)

    def test_request_shapes(self):
        llm.generate("p", system="s", chain=self.chain(("gemini", "g1")), keys=self.keys, json_mode=True)
        req = self.world.calls("/gemini/g1")[0]
        self.assertEqual(req.headers.get("x-goog-api-key"), "g-key")
        self.assertEqual(req.json["generationConfig"]["responseMimeType"], "application/json")

        chain = [{"fournisseur": "groq", "modele": "openai/gpt-oss-120b",
                  "extra": {"reasoning_effort": "low", "max_completion_tokens": 123}}]
        llm.generate("p", system="s", chain=chain, keys=self.keys, json_mode=True)
        req = self.world.calls("/groq/")[0]
        self.assertEqual(req.headers.get("Authorization"), "Bearer q-key")
        self.assertEqual(req.json["response_format"], {"type": "json_object"})
        self.assertEqual(req.json["reasoning_effort"], "low")
        self.assertEqual(req.json["max_completion_tokens"], 123)
        self.assertIn("JSON", req.json["messages"][0]["content"])  # Groq exige le mot « JSON »

    def test_a_model_that_refuses_json_mode_is_retried_without_it(self):
        def fn(req, model, body):
            if "response_format" in body:
                return 400, json.dumps({"error": {"message": "response_format json_object is not supported with this model"}})
            return 200, '{"ok": true}'

        self.world.groq_fn = fn
        chain = [{"fournisseur": "groq", "modele": "openai/gpt-oss-120b"}]
        text, label = llm.generate("p", system="s", chain=chain, keys=self.keys, json_mode=True)
        self.assertEqual((text, label), ('{"ok": true}', "groq/openai/gpt-oss-120b"))
        first, second = self.world.calls("/groq/")
        self.assertIn("response_format", first.json)
        self.assertNotIn("response_format", second.json)
        self.assertIn("JSON", second.json["messages"][0]["content"])        # le prompt demande toujours du JSON
        # sans mode JSON demandé, une erreur 400 qui parle de json ne déclenche rien de spécial
        self.world.requests.clear()
        self.world.groq_fn = lambda req, model, body: (400, "bad json in request")
        with self.assertRaises(llm.LLMError):
            llm.generate("p", system="s", chain=chain, keys=self.keys)
        self.assertEqual(len(self.world.calls("/groq/")), 1)

    def test_keys_from_env(self):
        keys = llm.keys_from_env({"GEMINI_API_KEY": " abc ", "GROQ_API_KEY": None})
        self.assertEqual(keys, {"gemini": "abc", "groq": ""})


# =========================================================================== tri
def _score(i, imp, inter, ton="neutre", groupe=None, ignorer=False):
    return {"id": i, "importance": imp, "interet": inter, "ton": ton, "groupe": groupe or i, "ignorer": ignorer}


class TestTriage(WorldCase):
    def items(self, n, categorie="tech"):
        return [news.Item(f"S{i}", categorie, f"Titre {i}", "", None, id=i) for i in range(1, n + 1)]

    def test_parse_scores_variants(self):
        base = [_score(1, 6, 8, "positif"), _score(2, 3, 2)]
        good = json.dumps({"articles": base})
        for text in (good, f"```json\n{good}\n```", json.dumps(base), "Voici : " + good + " voilà"):
            scores = triage.parse_scores(text, 2)
            self.assertEqual(scores[1].interet, 8)
            self.assertEqual(scores[1].ton, "positif")
        syn = '{"articles":[{"id":"1","importance":"7,5","intérêt":8,"ton":"Négatif","groupe":"a","ignorer":"oui"}]}'
        sc = triage.parse_scores(syn, 1)[1]
        self.assertEqual((sc.importance, sc.interet, sc.ton, sc.ignorer), (7.5, 8.0, "difficile", True))

    def test_parse_scores_salvages_truncated_json(self):
        truncated = '{"articles":[{"id":1,"importance":6,"interet":8,"ton":"neutre","groupe":1,"ignorer":false},{"id":2,"importance":3,"interet'
        self.assertEqual(set(triage.parse_scores(truncated, 2)), {1})

    def test_parse_scores_rejects_incomplete_answer(self):
        with self.assertRaises(ValueError):
            triage.parse_scores(json.dumps({"articles": [_score(1, 5, 5)]}), 10)
        with self.assertRaises(ValueError):
            triage.parse_scores("n'importe quoi", 3)

    def test_select_orders_positive_first_and_limits_hard_subjects(self):
        items = self.items(6)
        scores = {
            1: triage.Score(9, 3, "difficile", "1", False),
            2: triage.Score(8, 2, "difficile", "2", False),
            3: triage.Score(4, 9, "positif", "3", False),
            4: triage.Score(7, 3, "neutre", "4", False),
            5: triage.Score(2, 1, "neutre", "5", False),
            6: triage.Score(8, 8, "neutre", "6", True),  # ignoré malgré de bonnes notes
        }
        chosen, rejected, calme = triage.select(items, scores, self.cfg["tri"])
        titles = [c.item.title for c in chosen]
        self.assertEqual(titles, ["Titre 3", "Titre 1", "Titre 4"])  # positif d'abord, difficile au milieu
        self.assertNotEqual(chosen[-1].ton, "difficile")
        reasons = {r.item.title: r.reason for r in rejected}
        self.assertIn("difficiles", reasons["Titre 2"])
        self.assertIn("ignoré", reasons["Titre 6"])
        self.assertIn("seuils", reasons["Titre 5"])
        self.assertFalse(calme)

    def test_select_collapses_duplicates_and_prefers_wider_coverage(self):
        items = self.items(3)
        scores = {
            1: triage.Score(8, 5, "neutre", "g", False),
            2: triage.Score(8, 5, "neutre", "g", False),
            3: triage.Score(8, 5, "neutre", "h", False),
        }
        chosen, rejected, _ = triage.select(items, scores, self.cfg["tri"])
        self.assertEqual(len(chosen), 2)
        self.assertEqual(sum(1 for r in rejected if "doublon" in r.reason), 1)

    def test_calm_day_relaxes_thresholds(self):
        items = self.items(5)
        scores = {i: triage.Score(4, 3, "neutre", str(i), False) for i in range(1, 6)}
        chosen, _, calme = triage.select(items, scores, self.cfg["tri"])
        self.assertTrue(calme)
        self.assertEqual(len(chosen), self.cfg["tri"]["minimum"])

    def test_category_cap_is_relaxed_to_reach_the_minimum_without_calling_it_a_calm_day(self):
        items = self.items(4, categorie="vr")
        scores = {i: triage.Score(8, 8, "neutre", str(i), False) for i in range(1, 5)}
        chosen, rejected, calme = triage.select(items, scores, self.cfg["tri"])
        self.assertEqual(len(chosen), self.cfg["tri"]["minimum"])  # 2 par catégorie + 1 pour atteindre le minimum
        self.assertFalse(calme)                                   # il y avait pourtant assez de bons sujets
        self.assertTrue(any("catégorie" in r.reason for r in rejected))

    def test_run_through_the_fake_ai(self):
        items = [
            news.Item("Numerama", "tech", "Houdini 21 arrive avec de nouveaux outils", "", None, id=1),
            news.Item("franceinfo", "general", "Grève des transports en Île-de-France", "", None, id=2),
            news.Item("franceinfo", "general", "Un attentat fait des victimes", "", None, id=3),
            news.Item("Promo", "tech", "Bon plan : -50% sur tout", "", None, id=4),
            news.Item("Blog", "tech", "Un billet sans intérêt", "", None, id=5),
        ]
        profile = triage.load_profile(self.project())
        res = triage.run(items, profile, self.cfg, self.keys, self.now)
        self.assertTrue(res.ok)
        titles = [c.item.title for c in res.choices]
        self.assertEqual(titles[0], "Houdini 21 arrive avec de nouveaux outils")
        self.assertNotIn("Bon plan : -50% sur tout", titles)
        self.assertTrue(res.modele.startswith("gemini/"))
        report = triage.report(res, self.now, profile=profile)
        self.assertIn("RETENUES", report)
        self.assertIn("PROVISOIRE", report)

    def test_run_falls_back_when_ai_is_down(self):
        self.world.gemini_fn = lambda req, model, body: (500, "down")
        self.world.groq_fn = lambda req, model, body: (500, "down")
        items = self.items(4)
        res = triage.run(items, triage.load_profile(self.project()), self.cfg, self.keys, self.now)
        self.assertFalse(res.ok)
        self.assertTrue(res.libre)
        self.assertEqual(len(res.choices), 4)
        self.assertIn("Tri indisponible", triage.report(res, self.now))

    def test_profile_switches_from_provisional_to_memory(self):
        racine = self.project()
        provisional = triage.load_profile(racine)
        self.assertEqual(provisional["source"], "provisoire")
        self.assertTrue(any("Houdini" in p["sujet"] for p in provisional["priorites"]))

        interets = racine / "memoire" / "interets.yaml"
        interets.write_text(yaml.safe_dump({"aime": [{"sujet": "montage vidéo", "poids": 3}] * 2}), encoding="utf-8")
        self.assertEqual(triage.load_profile(racine)["source"], "provisoire")  # 2 sujets appris : pas assez

        interets.write_text(
            yaml.safe_dump({
                "aime": [{"sujet": "montage vidéo", "poids": 3}, {"sujet": "pêche", "poids": 2}, {"sujet": "Maroc", "poids": 1}],
                "evite": ["football"],
            }),
            encoding="utf-8",
        )
        learned = triage.load_profile(racine)
        self.assertEqual(learned["source"], "memoire")
        self.assertEqual([p["sujet"] for p in learned["priorites"]], ["montage vidéo", "pêche", "Maroc"])
        self.assertIn("football", learned["a_ignorer"])
        self.assertFalse(any("Houdini" in p["sujet"] for p in learned["priorites"]))

    def test_pinned_topics_always_stay(self):
        racine = self.project()
        moi = yaml.safe_load((racine / "moi.yaml").read_text(encoding="utf-8"))
        moi["epingles"] = [{"sujet": "cinéma d'animation", "poids": 3}]
        (racine / "moi.yaml").write_text(yaml.safe_dump(moi, allow_unicode=True), encoding="utf-8")
        (racine / "memoire" / "interets.yaml").write_text(
            yaml.safe_dump({"aime": [{"sujet": s, "poids": 2} for s in ("a", "b", "c")]}), encoding="utf-8"
        )
        profile = triage.load_profile(racine)
        self.assertEqual(profile["priorites"][0]["sujet"], "cinéma d'animation")
        self.assertEqual(len(profile["priorites"]), 4)


# =========================================================================== infos perso
class TestPerso(unittest.TestCase):
    today = date(2026, 10, 4)

    def test_birthday_windows_and_age(self):
        entries = [
            {"nom": "Karim", "jour": 11, "mois": 10, "annee": 2005},
            {"nom": "Lina", "jour": 5, "mois": 10, "annee": 2000},
            {"nom": "Sam", "jour": 4, "mois": 10},
            {"nom": "Zoé", "jour": 6, "mois": 10},          # dans 2 jours : pas annoncé
            {"nom": "Cassé", "jour": 31, "mois": 2},        # date impossible : ignorée sans planter
        ]
        events = perso.birthday_events(entries, self.today, {7, 1, 0})
        texts = {e.delta: e.text for e in events}
        self.assertEqual(set(texts), {7, 1, 0})
        self.assertIn("Karim", texts[7])
        self.assertIn("Lina", texts[1])
        self.assertIn("26 ans", texts[1])
        self.assertIn("Sam", texts[0])

    def test_leap_day_birthday_in_regular_year(self):
        events = perso.birthday_events([{"nom": "Léa", "jour": 29, "mois": 2}], date(2027, 2, 27), {1})
        self.assertEqual(len(events), 1)
        self.assertIn("Léa", events[0].text)

    def test_reminders(self):
        entries = [
            {"texte": "rendre le dossier.", "date": date(2026, 10, 7)},      # dans 3 jours
            {"texte": "appeler la banque", "date": "2026-10-05"},            # demain
            {"texte": "ancien rappel", "date": date(2026, 10, 1)},           # passé
            {"texte": "billet de train", "date": date(2026, 10, 9), "prevenir": [5]},
            {"texte": "pas de date"},
        ]
        events = perso.reminder_events(entries, self.today, {3, 1, 0})
        texts = sorted(e.text for e in events)
        self.assertEqual(texts, ["Dans 3 jours : rendre le dossier.", "Dans 5 jours : billet de train.", "Demain : appeler la banque."])

    def test_manual_files_win_over_learned_ones(self):
        manual = [{"nom": "Karim", "jour": 1, "mois": 1}]
        auto = [{"nom": "karim", "jour": 2, "mois": 2}, {"nom": "Éloïse", "jour": 3, "mois": 3}]
        merged = perso.merge_birthdays(manual, auto)
        self.assertEqual([(e["nom"], e["jour"]) for e in merged], [("Karim", 1), ("Éloïse", 3)])
        rem = perso.merge_reminders([{"texte": "A", "date": "2026-01-01"}], [{"texte": "a", "date": "2026-01-01"}, {"texte": "B", "date": "2026-01-02"}])
        self.assertEqual(len(rem), 2)

    def test_build_segment_reads_files_and_caps_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            mem = Path(tmp) / "memoire"
            mem.mkdir()
            (mem / "anniversaires.yaml").write_text(yaml.safe_dump([{"nom": "Sam", "jour": 4, "mois": 10}]), encoding="utf-8")
            (mem / "anniversaires_auto.yaml").write_text(yaml.safe_dump([{"nom": "Lina", "jour": 5, "mois": 10}]), encoding="utf-8")
            (mem / "rappels_auto.yaml").write_text(yaml.safe_dump([{"texte": "rendre le dossier", "date": "2026-10-04"}]), encoding="utf-8")
            text, n = perso.build_segment(Path(tmp), {"max_lignes": 4}, self.today)
            self.assertEqual(n, 3)
            self.assertLess(text.index("Sam"), text.index("rendre le dossier"))  # anniversaire avant rappel, même jour
            self.assertLess(text.index("rendre le dossier"), text.index("Lina"))  # aujourd'hui avant demain
            _, n2 = perso.build_segment(Path(tmp), {"max_lignes": 2}, self.today)
            self.assertEqual(n2, 2)

    def test_missing_or_broken_files_are_harmless(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(perso.build_segment(Path(tmp), None, self.today), ("", 0))
            mem = Path(tmp) / "memoire"
            mem.mkdir()
            (mem / "anniversaires.yaml").write_text("- nom: [pas fermé", encoding="utf-8")
            self.assertEqual(perso.build_segment(Path(tmp), None, self.today), ("", 0))


# =========================================================================== texte
class TestScriptWriter(WorldCase):
    def choices(self):
        item = news.Item("Numerama", "tech", "Houdini 21 sort", "Nouveaux outils.", None, id=1)
        return [triage.Choice(item, 5, 9, "positif", 8.0)]

    def test_insert_perso_at_marker_after_greeting_or_after_first_sentence(self):
        seg = "Demain, Lina fête ses 26 ans."
        self.assertEqual(
            script_writer.insert_perso("Bonjour.\n\n[[PERSO]]\n\nSuite.", seg), f"Bonjour.\n\n{seg}\n\nSuite."
        )
        self.assertEqual(
            script_writer.insert_perso("Bonjour Léo, dimanche.\n\nPremier sujet.", seg),
            f"Bonjour Léo, dimanche.\n\n{seg}\n\nPremier sujet.",
        )
        one_block = script_writer.insert_perso("Bonjour à toi. Voici les actus. Fin.", seg)
        self.assertTrue(one_block.startswith("Bonjour à toi.\n\n" + seg))
        self.assertNotIn("[[", script_writer.insert_perso("Salut [[AUTRE]] toi [[PERSO]] fin", ""))

    def test_clean_script_removes_markdown_and_links(self):
        dirty = "# Titre\n\n**Bonjour** `toi`\n- point un\nvoir https://exemple.fr/x ici"
        clean = script_writer.clean_script(dirty)
        for bad in ("#", "*", "`", "http", "- "):
            self.assertNotIn(bad, clean)

    def test_perso_text_goes_in_after_generation_and_never_to_the_ai(self):
        res = script_writer.write_script(
            self.choices(), self.cfg, self.keys, self.now, prenom="Léo", perso_text="Demain, Karim fête ses 21 ans.",
        )
        text, model = res
        self.assertIn("Demain, Karim fête ses 21 ans.", text)
        self.assertNotIn("[[", text)
        self.assertNotIn("Karim", self.world.bodies("/gemini/") + self.world.bodies("/groq/"))
        self.assertTrue(model.startswith("gemini/"))

    def test_dramatic_words_trigger_one_rewrite(self):
        calls = []

        def fn(req, model, body):
            calls.append(body["contents"][0]["parts"][0]["text"])
            if len(calls) == 1:
                return 200, "Bonjour.\n\nUn événement tragique, une vraie catastrophe."
            return 200, "Bonjour.\n\nUn événement sérieux, la situation est suivie."

        self.world.gemini_fn = fn
        text, _ = script_writer.write_script(self.choices(), self.cfg, self.keys, self.now)
        self.assertEqual(len(calls), 2)
        self.assertEqual(script_writer.dramatic_hits(text), [])
        self.assertIn("tragique", calls[1])  # la réécriture reçoit les mots à remplacer

    def test_overlong_text_is_shortened_once(self):
        calls = []
        long_text = "Bonjour. " + "mot " * 600
        short_text = "Bonjour. " + "mot " * 200

        def fn(req, model, body):
            calls.append(1)
            return 200, long_text if len(calls) == 1 else short_text

        self.world.gemini_fn = fn
        text, _ = script_writer.write_script(self.choices(), self.cfg, self.keys, self.now)
        self.assertEqual(len(calls), 2)
        self.assertLess(len(text.split()), 250)

    def test_ai_failure_raises(self):
        self.world.gemini_fn = lambda req, model, body: (500, "down")
        self.world.groq_fn = lambda req, model, body: (500, "down")
        with self.assertRaises(llm.LLMError):
            script_writer.write_script(self.choices(), self.cfg, self.keys, self.now)


# =========================================================================== bout en bout
class TestPipeline(WorldCase):
    def setUp(self):
        super().setUp()
        self.use_feeds({
            "numerama": ("tech", [
                {"title": "Houdini 21 arrive avec de nouveaux outils", "summary": "Du procédural partout."},
                {"title": "Un billet sans intérêt"},
            ]),
            "info": ("general", [
                {"title": "Grève des transports en Île-de-France demain"},
                {"title": "Un attentat fait des victimes"},
                {"title": "Bon plan : -50% sur tout"},
            ]),
            "positif": ("positif", [{"title": "Bonne nouvelle : une découverte contre la pollution"}]),
        })
        self.racine = self.project()
        (self.racine / "memoire" / "anniversaires.yaml").write_text(
            yaml.safe_dump([{"nom": "Karim", "jour": 5, "mois": 10, "annee": 2005}]), encoding="utf-8"
        )
        self.out = self.tmp / "out"

    def run_pipeline(self, **kw):
        return main.generate(self.racine, self.cfg, self.out, keys=self.keys, now=self.now, no_audio=True, **kw)

    def test_full_run_writes_everything_and_keeps_private_data_away_from_ai(self):
        code = self.run_pipeline()
        self.assertEqual(code, main.EXIT_OK)
        script = (self.out / "latest.txt").read_text(encoding="utf-8")
        self.assertIn("Karim", script)                      # l'anniversaire est bien dit…
        self.assertIn("Houdini", script)
        self.assertNotIn("attentat", script.lower().split("belle journée")[-1])
        meta = json.loads((self.out / "meta.json").read_text(encoding="utf-8"))
        self.assertTrue(meta["tri"]["ok"])
        self.assertEqual(meta["tri"]["profil"], "provisoire")
        self.assertEqual(meta["perso"]["lignes"], 1)
        self.assertFalse(meta["audio"])
        choix = (self.out / "choix.txt").read_text(encoding="utf-8")
        self.assertIn("RETENUES", choix)
        self.assertIn("SOURCES", choix)
        # … mais ni Gemini ni Groq n'ont jamais vu son nom
        self.assertNotIn("Karim", self.world.bodies("/gemini/") + self.world.bodies("/groq/"))

    def test_audio_failure_keeps_the_text_and_returns_code_3(self):
        with mock.patch.object(tts, "synthesize", side_effect=RuntimeError("edge en panne")):
            code = main.generate(self.racine, self.cfg, self.out, keys=self.keys, now=self.now, no_audio=False)
        self.assertEqual(code, main.EXIT_NO_AUDIO)
        self.assertTrue((self.out / "latest.txt").exists())
        self.assertFalse((self.out / "latest.mp3").exists())
        self.assertFalse(json.loads((self.out / "meta.json").read_text(encoding="utf-8"))["audio"])

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg absent")
    def test_audio_success_records_duration_and_sends_to_telegram_when_configured(self):
        def fake_tts(text, cfg, out_path):
            subprocess.run(
                ["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
                 "-q:a", "9", str(out_path)], check=True,
            )
            return "fr-FR-TestNeural"

        with mock.patch.object(tts, "synthesize", side_effect=fake_tts):
            code = main.generate(
                self.racine, self.cfg, self.out, keys=self.keys, now=self.now, no_audio=False, send_telegram=False
            )
        self.assertEqual(code, main.EXIT_OK)
        meta = json.loads((self.out / "meta.json").read_text(encoding="utf-8"))
        self.assertTrue(meta["audio"])
        self.assertEqual(meta["voix"], "fr-FR-TestNeural")
        self.assertIn(meta["duree_s"], (3, 4))
        self.assertNotIn("telegram", meta)

    def test_no_news_at_all_publishes_a_fallback_podcast_and_removes_stale_files(self):
        self.out.mkdir()
        for stale in ("latest.mp3", "latest.txt", "meta.json", "choix.txt"):
            (self.out / stale).write_text("périmé", encoding="utf-8")
        for name in list(self.world.feeds):
            self.world.feeds[name] = 500
        code = self.run_pipeline()
        self.assertEqual(code, main.EXIT_SECOURS)           # le job le signale (mail GitHub)…
        script = (self.out / "latest.txt").read_text(encoding="utf-8")
        self.assertNotIn("périmé", script)
        self.assertIn("Bonjour", script)
        self.assertIn("Karim", script)                      # …mais le réveil garde ses infos perso
        self.assertIn("je réessaie demain", script)
        meta = json.loads((self.out / "meta.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["mode"], "secours")
        self.assertIn("Aucune actu", meta["raison_secours"])
        self.assertFalse(meta["tri"]["ok"])
        self.assertTrue((self.out / "choix.txt").read_text(encoding="utf-8").startswith("PODCAST DE SECOURS"))
        self.assertEqual(self.world.calls("/gemini/") + self.world.calls("/groq/"), [])  # aucune IA sollicitée

    def test_all_ais_down_publishes_a_fallback_that_still_explains_the_choice(self):
        self.world.gemini_fn = lambda req, model, body: (500, "down")
        self.world.groq_fn = lambda req, model, body: (500, "down")
        self.assertEqual(self.run_pipeline(), main.EXIT_SECOURS)
        script = (self.out / "latest.txt").read_text(encoding="utf-8")
        self.assertIn("Karim", script)
        self.assertNotIn("Houdini", script)                 # pas d'actus : on ne bricole pas un faux résumé
        meta = json.loads((self.out / "meta.json").read_text(encoding="utf-8"))
        self.assertEqual((meta["mode"], meta["modele"]), ("secours", "secours (sans IA)"))
        self.assertIn("HTTP 500", meta["raison_secours"])   # la raison réelle, pas seulement « Aucun modèle… »
        choix = (self.out / "choix.txt").read_text(encoding="utf-8")
        self.assertTrue(choix.startswith("PODCAST DE SECOURS"))
        self.assertIn("SOURCES", choix)                     # le rapport de tri est conservé sous l'en-tête

    def test_fallback_script_is_calm_and_handles_missing_parts(self):
        now = datetime(2026, 10, 5, 5, 30, tzinfo=PARIS)
        text = main.fallback_script("Léo", "Demain, c'est l'anniversaire de Karim.", now)
        self.assertTrue(text.startswith("Bonjour Léo ! Nous sommes le lundi 5 octobre 2026."))
        self.assertIn("anniversaire de Karim", text)
        bare = main.fallback_script("", "", now)
        self.assertTrue(bare.startswith("Bonjour ! Nous sommes"))
        self.assertEqual(bare.count("\n\n"), 1)
        self.assertEqual(script_writer.dramatic_hits(text), [])

    def test_ai_triage_down_still_produces_a_podcast(self):
        original = self.world.smart_gemini

        def fn(req, model, body):
            if "rédacteur en chef" in body["systemInstruction"]["parts"][0]["text"]:
                return 500, "tri en panne"
            return original(req, model, body)

        self.world.gemini_fn = fn
        self.world.groq_fn = lambda req, model, body: (500, "down")
        self.assertEqual(self.run_pipeline(), main.EXIT_OK)
        meta = json.loads((self.out / "meta.json").read_text(encoding="utf-8"))
        self.assertFalse(meta["tri"]["ok"])
        self.assertIn("Tri indisponible", (self.out / "choix.txt").read_text(encoding="utf-8"))

    def test_broken_personal_files_never_block_the_podcast(self):
        (self.racine / "memoire" / "anniversaires.yaml").write_text("- nom: [pas fermé", encoding="utf-8")
        self.assertEqual(self.run_pipeline(), main.EXIT_OK)

    def test_cli_entry_point_uses_env_keys_and_no_audio(self):
        cfg_path = self.tmp / "cfg.yaml"
        cfg_path.write_text(yaml.safe_dump(self.cfg, allow_unicode=True), encoding="utf-8")
        env = {"GEMINI_API_KEY": "g-key", "GROQ_API_KEY": "q-key"}
        with mock.patch.dict("os.environ", env), mock.patch("builtins.print"):
            code = main.main(["--racine", str(self.racine), "--config", str(cfg_path), "--out", str(self.out), "--no-audio"])
        self.assertEqual(code, main.EXIT_OK)
        self.assertTrue((self.out / "latest.txt").exists())


class TestShippedFiles(unittest.TestCase):
    def test_config_and_templates_are_valid(self):
        cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
        for chain in ("tri", "ecriture", "prive"):
            self.assertTrue(cfg["llm"][chain], chain)
        self.assertTrue(all(e["fournisseur"] == "groq" for e in cfg["llm"]["prive"]))  # vie privée : Groq seulement
        self.assertTrue(cfg["llm"]["vision"])
        self.assertTrue(all(e["fournisseur"] == "groq" for e in cfg["llm"]["vision"]))  # tes images ne partent qu'à Groq
        self.assertNotIn("llama-4-scout", json.dumps(cfg["llm"]))                       # retiré par Groq en juillet 2026
        for feed in cfg["actu"]["flux"]:
            self.assertTrue(feed["url"].startswith("https://") and feed.get("nom") and feed.get("categorie"))
        self.assertEqual(yaml.safe_load((ROOT / "memoire" / "anniversaires.yaml").read_text(encoding="utf-8")), [])
        self.assertEqual(yaml.safe_load((ROOT / "memoire" / "rappels.yaml").read_text(encoding="utf-8")), [])
        profile = triage.load_profile(ROOT)
        self.assertEqual(profile["source"], "provisoire")
        self.assertGreaterEqual(len(profile["priorites"]), 5)


if __name__ == "__main__":
    unittest.main()
