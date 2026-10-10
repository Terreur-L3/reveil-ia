"""Tests hors ligne du « garde » du podcast, de l'appel GitHub et des fichiers de workflow livrés.

Lancer :  python -m unittest discover -s tests -t . -v
"""
from __future__ import annotations

import contextlib
import io
import json
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

import yaml

from src import garde, github
from tests.test_podcast import ROOT, WorldCase

TODAY = date(2026, 10, 7)
EARLY = "17 3 * * *"
NET = "47 6 * * *"


def meta(day: str = "2026-10-07", *, mode="normal", audio=True, origine="cron") -> dict:
    return {"date": day, "mode": mode, "audio": audio, "origine": origine}


# =========================================================================== faut-il générer maintenant ?
class TestDecide(unittest.TestCase):
    CASES = [
        # (événement, cron, meta, sauter ?, un morceau de la raison)
        ("workflow_dispatch", "", None, False, "volontaire"),
        ("workflow_dispatch", "", meta(), False, "volontaire"),                        # « Run workflow », sonnette, /podcast : jamais sauté
        ("push", "", meta(), False, "volontaire"),
        ("", "", meta(), False, "volontaire"),
        ("schedule", EARLY, None, False, "pas de podcast"),
        ("schedule", EARLY, {}, False, "pas de podcast"),
        ("schedule", EARLY, meta(), True, "existe déjà"),
        ("schedule", NET, meta(), True, "existe déjà"),
        ("schedule", EARLY, meta(mode="secours"), False, "pas de podcast"),             # un podcast de secours ne compte pas…
        ("schedule", NET, meta(mode="secours"), False, "filet"),                         # …et le filet le remplace par un vrai
        ("schedule", NET, meta(audio=False), False, "filet"),                            # sans audio : pas complet
        ("schedule", NET, None, False, "filet"),
        ("schedule", NET, meta("2026-10-06"), False, "filet"),
        ("schedule", EARLY, meta("2026-10-06"), False, "pas de podcast"),                # celui d'hier fait par le cron : on refait
        ("schedule", EARLY, meta("2026-10-06", origine="sonnette"), True, "sonnette"),   # la sonnette fait celui d'aujourd'hui
        ("schedule", EARLY, meta("2026-10-07", mode="secours", origine="sonnette"), True, "sonnette"),
        ("schedule", EARLY, meta("2026-10-05", origine="sonnette"), False, "pas de podcast"),   # sonnette absente depuis 2 jours
        ("schedule", EARLY, meta("2026-10-06", origine="telegram"), False, "pas de podcast"),
        ("schedule", NET, meta("2026-10-06", origine="sonnette"), False, "filet"),        # le filet passe, lui, même si la sonnette marche
        ("schedule", EARLY, {"date": "pas une date", "origine": "sonnette"}, False, "pas de podcast"),
        ("schedule", EARLY, {"date": "2026-10-08", "origine": "sonnette"}, False, "pas de podcast"),  # date dans le futur : on ne s'y fie pas
        ("schedule", f" {NET} ", meta(), True, "existe déjà"),                           # espaces autour du cron
        ("schedule", f" {NET} ", None, False, "filet"),
    ]

    def test_table(self):
        for event, cron, data, skip, reason in self.CASES:
            with self.subTest(event=event, cron=cron, meta=data):
                got_skip, got_reason = garde.decide(event, cron, data, TODAY)
                self.assertEqual(got_skip, skip, got_reason)
                self.assertIn(reason, got_reason)

    def test_the_net_cron_mark_is_the_second_schedule_of_the_workflow(self):
        self.assertEqual(garde.NET_CRON_MARK, NET)


class TestCli(unittest.TestCase):
    def run_cli(self, *argv: str, now=datetime(2026, 10, 7, 5, 17, tzinfo=ZoneInfo("Europe/Paris"))):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), mock.patch.object(garde, "datetime") as fake:
            fake.now.return_value = now
            self.fake_now = fake.now
            code = garde.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def write(self, directory, data) -> str:
        path = Path(directory) / "meta.json"
        path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
        return str(path)

    def test_writes_skip_for_the_workflow(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            done = self.write(tmp, meta())
            code, out, err = self.run_cli("--event", "schedule", "--cron", EARLY, "--meta", done)
            self.assertEqual((code, out), (0, "skip=true\n"))
            self.assertIn("existe déjà", err)
            code, out, _ = self.run_cli("--event", "workflow_dispatch", "--meta", done)
            self.assertEqual(out, "skip=false\n")                                       # lancement volontaire : jamais sauté

    def test_unreadable_or_missing_meta_means_generate(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            for bad in ("{pas du json", "[1, 2]", "", '"texte"'):
                with self.subTest(bad):
                    code, out, _ = self.run_cli("--event", "schedule", "--cron", EARLY, "--meta", self.write(tmp, bad))
                    self.assertEqual((code, out), (0, "skip=false\n"))
            code, out, _ = self.run_cli("--event", "schedule", "--cron", NET, "--meta", str(Path(tmp) / "absent.json"))
            self.assertEqual(out, "skip=false\n")
            code, out, _ = self.run_cli("--event", "schedule", "--cron", NET)
            self.assertEqual(out, "skip=false\n")

    def test_the_event_comes_from_the_github_environment_by_default(self):
        with mock.patch.dict("os.environ", {"GITHUB_EVENT_NAME": "workflow_dispatch"}):
            code, out, err = self.run_cli()
        self.assertEqual(out, "skip=false\n")
        self.assertIn("volontaire", err)

    def test_the_day_is_read_in_the_configured_time_zone(self):
        self.run_cli("--event", "schedule")
        self.fake_now.assert_called_once_with(ZoneInfo("Europe/Paris"))
        self.run_cli("--event", "schedule", "--tz", "Pacific/Auckland")
        self.fake_now.assert_called_with(ZoneInfo("Pacific/Auckland"))


# =========================================================================== lancer un workflow GitHub
class TestGitHubDispatch(WorldCase):
    ENV = {"GITHUB_TOKEN": "ghs_TESTSECRET", "GITHUB_REPOSITORY": "Terreur-L3/reveil-ia", "GITHUB_REF_NAME": "main"}

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(github, "API_ROOT", self.world.base + "/github")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_success_sends_the_right_request(self):
        github.dispatch("podcast.yml", {"origine": "telegram", "silencieux": True, "n": 3}, self.ENV)
        self.assertEqual(self.world.gh_dispatches, [{
            "repo": "Terreur-L3/reveil-ia", "workflow": "podcast.yml", "ref": "main",
            "inputs": {"origine": "telegram", "silencieux": "True", "n": "3"},       # GitHub veut des textes
            "auth": "Bearer ghs_TESTSECRET"}])
        headers = self.world.calls("/github/")[0].headers
        self.assertEqual(headers.get("Accept"), "application/vnd.github+json")
        self.assertEqual(headers.get("X-GitHub-Api-Version"), "2022-11-28")

    def test_any_success_code_means_started(self):
        # 204 = réponse habituelle ; 200 = réponse avec le détail du lancement (nouveauté GitHub 2026) ; 202 par prudence
        for status in (204, 200, 201, 202):
            with self.subTest(status):
                self.world.gh_ok_status = status
                before = len(self.world.gh_dispatches)
                github.dispatch("podcast.yml", {"origine": "telegram"}, self.ENV)            # ne lève rien
                self.assertEqual(len(self.world.gh_dispatches), before + 1)

    def test_a_redirect_or_an_error_is_still_a_failure(self):
        for status in (301, 400, 404, 500):
            with self.subTest(status):
                response = mock.Mock(status_code=status)
                response.json.return_value = {"message": "pas bon"}
                with mock.patch.object(github.requests, "post", return_value=response), self.assertRaises(github.GitHubError):
                    github.dispatch("podcast.yml", None, self.ENV)

    def test_the_branch_defaults_to_main_and_inputs_are_optional(self):
        env = {k: v for k, v in self.ENV.items() if k != "GITHUB_REF_NAME"}
        github.dispatch("journal.yml", None, env)
        (call,) = self.world.gh_dispatches
        self.assertEqual((call["ref"], call["inputs"], call["workflow"]), ("main", {}, "journal.yml"))
        github.dispatch("journal.yml", {}, {**env, "GITHUB_REF_NAME": "  dev  "})
        self.assertEqual(self.world.gh_dispatches[-1]["ref"], "dev")

    def test_nothing_configured_is_reported_as_missing_and_sends_nothing(self):
        for env in ({}, {"GITHUB_TOKEN": "t"}, {"GITHUB_REPOSITORY": "a/b"}, {"GITHUB_TOKEN": " ", "GITHUB_REPOSITORY": "a/b"},
                    {"GITHUB_TOKEN": "t", "GITHUB_REPOSITORY": "pas-un-depot"}):
            with self.subTest(env), self.assertRaises(github.GitHubError) as ctx:
                github.dispatch("podcast.yml", None, env)
            self.assertTrue(ctx.exception.missing)
        self.assertEqual(self.world.requests, [])

    def test_errors_come_with_a_hint(self):
        hints = {401: "actions: write", 403: "actions: write", 404: "introuvable", 422: "podcast.yml", 500: ""}
        for status, hint in hints.items():
            with self.subTest(status):
                self.world.gh_fail = status
                with self.assertRaises(github.GitHubError) as ctx:
                    github.dispatch("podcast.yml", None, self.ENV)
                self.assertFalse(ctx.exception.missing)
                self.assertTrue(str(ctx.exception).startswith(f"HTTP {status} Resource not accessible"))
                self.assertIn(hint, str(ctx.exception))
                self.assertNotIn("ghs_TESTSECRET", str(ctx.exception))
        self.assertEqual(self.world.gh_dispatches, [])

    def test_an_unreadable_error_body_is_survivable(self):
        response = mock.Mock(status_code=502)
        response.json.side_effect = ValueError("pas du json")
        with mock.patch.object(github.requests, "post", return_value=response), self.assertRaises(github.GitHubError) as ctx:
            github.dispatch("podcast.yml", None, self.ENV)
        self.assertEqual(str(ctx.exception).strip(), "HTTP 502")

    def test_a_network_failure_never_shows_the_address_or_the_token(self):
        with mock.patch.object(github, "API_ROOT", "http://127.0.0.1:1"), self.assertRaises(github.GitHubError) as ctx:
            github.dispatch("podcast.yml", None, self.ENV)
        message = str(ctx.exception)
        self.assertTrue(message.startswith("réseau ("))
        for secret in ("ghs_TESTSECRET", "127.0.0.1", "Terreur-L3"):
            self.assertNotIn(secret, message)


# =========================================================================== les fichiers de workflow livrés
class TestShippedWorkflows(unittest.TestCase):
    @staticmethod
    def load(name: str) -> dict:
        doc = yaml.safe_load((ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8"))
        doc["triggers"] = doc.pop("on", None) or doc.pop(True, None)          # YAML 1.1 lit « on » comme le booléen vrai
        return doc

    def steps(self, doc: dict, job: str) -> dict:
        return {s.get("id") or s.get("name"): s for s in doc["jobs"][job]["steps"]}

    def test_podcast_workflow(self):
        doc = self.load("podcast.yml")
        crons = [s["cron"] for s in doc["triggers"]["schedule"]]
        self.assertEqual(crons, [EARLY, garde.NET_CRON_MARK])
        inputs = doc["triggers"]["workflow_dispatch"]["inputs"]
        self.assertEqual(sorted(inputs), ["origine", "silencieux"])
        self.assertEqual(doc["concurrency"], {"group": "podcast", "cancel-in-progress": False})
        self.assertEqual(doc["permissions"], {"contents": "write"})
        steps = self.steps(doc, "build")
        self.assertIn("python3 -m src.garde", steps["garde"]["run"])
        self.assertEqual(steps["garde"]["env"]["CRON"], "${{ github.event.schedule }}")
        for name in ("Installer les dépendances", "gen", "Publier sur la branche « podcast » (sans accumuler d'historique)"):
            self.assertEqual(steps[name]["if"], "steps.garde.outputs.skip != 'true'", name)
        env = steps["gen"]["env"]
        self.assertEqual(sorted(k for k in env if k.startswith("PODCAST_")), ["PODCAST_ORIGINE", "PODCAST_SILENT"])
        self.assertIn("inputs.silencieux", env["PODCAST_SILENT"])
        self.assertIn("inputs.origine", env["PODCAST_ORIGINE"])
        for secret in ("GEMINI_API_KEY", "GROQ_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
            self.assertEqual(env[secret], "${{ secrets." + secret + " }}")

    def test_journal_workflow(self):
        doc = self.load("journal.yml")
        self.assertEqual([s["cron"] for s in doc["triggers"]["schedule"]], ["7,37 5-21 * * *"])
        self.assertEqual(sorted(doc["triggers"]["workflow_dispatch"]["inputs"]), ["inspi", "origine"])
        self.assertEqual(doc["permissions"], {"contents": "write", "actions": "write"})   # actions: write = la commande /podcast
        self.assertEqual(doc["concurrency"], {"group": "journal", "cancel-in-progress": False})
        env = self.steps(doc, "journal")["Lire Telegram, transcrire, ranger, poser la question"]["env"]
        self.assertEqual(env["GITHUB_TOKEN"], "${{ github.token }}")
        self.assertIn("inputs.origine", env["JOURNAL_ORIGINE"])
        self.assertNotIn("GEMINI_API_KEY", env)                                         # le journal ne parle jamais à Gemini

    def test_the_workflows_ask_for_nothing_beyond_what_they_use(self):
        for name in ("podcast.yml", "journal.yml"):
            text = (ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8")
            self.assertNotIn("secrets.GITHUB_TOKEN", text)
            self.assertNotIn("pull_request", text)                                      # le dépôt est privé, mais on ne s'ouvre à rien d'autre
            self.assertNotIn("pull_request_target", text)


if __name__ == "__main__":
    unittest.main()
