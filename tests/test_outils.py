"""Tests hors ligne de l'outil de mise à jour de config.yaml (voix réaliste)."""
from __future__ import annotations

import importlib.util
import shutil
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("maj_config_voix", ROOT / "outils" / "maj_config_voix.py")
maj = importlib.util.module_from_spec(spec)
spec.loader.exec_module(maj)
spec_perso = importlib.util.spec_from_file_location("maj_config_perso", ROOT / "outils" / "maj_config_perso.py")
maj_perso = importlib.util.module_from_spec(spec_perso)
spec_perso.loader.exec_module(maj_perso)

OLD = '''prenom: Marius
fuseau: Europe/Paris

# ----- Voix du podcast -----
voix:
  moteur: edge
  # Autres voix à tester : fr-FR-DeniseNeural
  edge_voix: fr-FR-DeniseNeural
  vitesse: "+10%"         # plus vite

# ----- Telegram : ta conversation avec ton IA -----
telegram:
  envoyer_podcast: true
'''


class TestMajConfigVoix(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = self.tmp / "config.yaml"

    def test_only_the_voice_block_changes_and_the_user_choices_are_kept(self):
        new, message = maj.migrate(OLD)
        data = yaml.safe_load(new)
        self.assertEqual(data["voix"]["moteur"], "gemini")
        self.assertEqual(data["voix"]["gemini"]["voix"], "Sulafat")
        self.assertEqual((data["voix"]["edge_voix"], data["voix"]["vitesse"]), ("fr-FR-DeniseNeural", "+10%"))
        self.assertEqual((data["prenom"], data["telegram"]), ("Marius", {"envoyer_podcast": True}))   # le reste est intact
        self.assertTrue(new.startswith(OLD.split("# ----- Voix")[0]))
        self.assertTrue(new.endswith("# ----- Telegram : ta conversation avec ton IA -----\ntelegram:\n  envoyer_podcast: true\n"))
        self.assertIn("mise à jour", message)

    def test_idempotent(self):
        once, _ = maj.migrate(OLD)
        twice, message = maj.migrate(once)
        self.assertEqual(once, twice)
        self.assertIn("déjà à jour", message)

    def test_voice_block_at_the_end_of_the_file(self):
        text = OLD.split("# ----- Telegram")[0]
        new, _ = maj.migrate(text)
        self.assertEqual(yaml.safe_load(new)["voix"]["moteur"], "gemini")

    def test_missing_block_changes_nothing(self):
        new, message = maj.migrate("prenom: Marius\n")
        self.assertEqual(new, "prenom: Marius\n")
        self.assertTrue(message.startswith("ERREUR"))

    def test_defaults_when_edge_voice_and_speed_are_absent(self):
        text = OLD.replace('  edge_voix: fr-FR-DeniseNeural\n  vitesse: "+10%"         # plus vite\n', "")
        data = yaml.safe_load(maj.migrate(text)[0])
        self.assertEqual((data["voix"]["edge_voix"], data["voix"]["vitesse"]), ("fr-FR-HenriNeural", "+0%"))

    def test_cli_writes_a_backup_and_verifier_changes_nothing(self):
        self.path.write_text(OLD, encoding="utf-8")
        self.assertEqual(maj.main(["--config", str(self.path), "--verifier"]), 0)
        self.assertEqual(self.path.read_text(encoding="utf-8"), OLD)
        self.assertEqual(maj.main(["--config", str(self.path)]), 0)
        self.assertEqual(self.path.with_name("config.yaml.avant-voix").read_text(encoding="utf-8"), OLD)
        self.assertEqual(yaml.safe_load(self.path.read_text(encoding="utf-8"))["voix"]["moteur"], "gemini")
        self.assertEqual(maj.main(["--config", str(self.path)]), 0)                      # relancer ne casse rien

    def test_the_shipped_config_is_already_up_to_date_and_matches_the_tool(self):
        shipped = (ROOT / "config.yaml").read_text(encoding="utf-8")
        self.assertEqual(maj.migrate(shipped)[0], shipped)
        # le bloc de l'outil et celui de config.yaml disent la même chose (mêmes clés ; edge_voix et vitesse sont à l'utilisateur)
        a = yaml.safe_load(maj.BLOCK.format(edge_voix="x", vitesse="+0%"))["voix"]
        b = yaml.safe_load(shipped)["voix"]
        a["edge_voix"] = b["edge_voix"] = a["vitesse"] = b["vitesse"] = None
        self.assertEqual(a, b)


PERSO_OLD = '''prenom: Marius

# ----- Infos perso dites par le programme (jamais envoyées à une IA) -----
perso:
  anniversaires_jours_avant: [7, 1, 0]
  max_lignes: 6              # mes réglages
  cours: false
  consignes_dans_podcast: true   # tes consignes

# ----- Réveil : le téléphone joue reveil.mp3 -----
reveil:
  sonnerie: true
'''


class TestMajConfigPerso(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = self.tmp / "config.yaml"

    def test_only_the_comment_and_one_key_change_and_the_user_choices_are_kept(self):
        new, message = maj_perso.migrate(PERSO_OLD)
        before, after = yaml.safe_load(PERSO_OLD), yaml.safe_load(new)
        self.assertIs(after["perso"].pop("style_ia"), True)
        self.assertEqual(after, before)                                                  # rien d'autre n'a bougé (max_lignes: 6, cours: false…)
        self.assertNotIn("jamais envoyées à une IA", new)
        self.assertIn("Groq (jamais Gemini)", new)
        self.assertIn("# mes réglages", new)                                             # les commentaires de l'utilisateur restent
        self.assertIn("mise à jour", message)
        self.assertLess(new.index("style_ia"), new.index("# ----- Réveil"))               # la clé est bien DANS la section perso

    def test_idempotent(self):
        once, _ = maj_perso.migrate(PERSO_OLD)
        twice, message = maj_perso.migrate(once)
        self.assertEqual(once, twice)
        self.assertIn("déjà à jour", message)

    def test_a_choice_already_made_is_never_overwritten(self):
        text = PERSO_OLD.replace("  cours: false\n", "  cours: false\n  style_ia: false\n")
        new, _ = maj_perso.migrate(text)
        self.assertIs(yaml.safe_load(new)["perso"]["style_ia"], False)
        self.assertEqual(new.count("style_ia"), 1)

    def test_section_at_the_end_of_the_file_without_a_final_newline(self):
        text = "prenom: M\nperso:\n  cours: true"
        new, _ = maj_perso.migrate(text)
        self.assertEqual(yaml.safe_load(new)["perso"], {"cours": True, "style_ia": True})

    def test_an_empty_section_still_gets_the_key(self):
        new, _ = maj_perso.migrate("perso:\n\nreveil:\n  sonnerie: true\n")
        data = yaml.safe_load(new)
        self.assertEqual((data["perso"], data["reveil"]), ({"style_ia": True}, {"sonnerie": True}))

    def test_missing_section_changes_nothing_and_is_not_an_error(self):
        new, message = maj_perso.migrate("prenom: Marius\n")
        self.assertEqual(new, "prenom: Marius\n")
        self.assertFalse(message.startswith("ERREUR"))

    def test_only_the_comment_when_there_is_no_perso_section(self):
        new, message = maj_perso.migrate("# ----- Infos perso dites par le programme (jamais envoyées à une IA) -----\nprenom: M\n")
        self.assertNotIn("jamais envoyées à une IA", new)
        self.assertIn("Commentaire corrigé", message)

    def test_cli_writes_a_backup_and_verifier_changes_nothing(self):
        self.path.write_text(PERSO_OLD, encoding="utf-8")
        self.assertEqual(maj_perso.main(["--config", str(self.path), "--verifier"]), 0)
        self.assertEqual(self.path.read_text(encoding="utf-8"), PERSO_OLD)
        self.assertEqual(maj_perso.main(["--config", str(self.path)]), 0)
        self.assertEqual(self.path.with_name("config.yaml.avant-perso").read_text(encoding="utf-8"), PERSO_OLD)
        self.assertIs(yaml.safe_load(self.path.read_text(encoding="utf-8"))["perso"]["style_ia"], True)
        self.assertEqual(maj_perso.main(["--config", str(self.path)]), 0)                  # relancer ne casse rien

    def test_the_shipped_config_is_already_up_to_date_and_matches_the_tool(self):
        shipped = (ROOT / "config.yaml").read_text(encoding="utf-8")
        self.assertEqual(maj_perso.migrate(shipped)[0], shipped)
        self.assertIn(maj_perso.NEW_HEADER, shipped)
        self.assertIn(maj_perso.STYLE_LINE, shipped)

    def test_the_tool_also_works_on_the_config_as_the_user_has_it_today(self):
        """Le config.yaml livré avant cette mise à jour (sans style_ia, ancien commentaire) est migré sans autre changement."""
        shipped = (ROOT / "config.yaml").read_text(encoding="utf-8")
        today = shipped.replace(maj_perso.NEW_HEADER, maj_perso.OLD_HEADER).replace(maj_perso.STYLE_LINE + "\n", "")
        self.assertNotIn("style_ia", today)
        new, _ = maj_perso.migrate(today)
        self.assertEqual(new, shipped)


if __name__ == "__main__":
    unittest.main()
