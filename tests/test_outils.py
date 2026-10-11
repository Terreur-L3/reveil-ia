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


if __name__ == "__main__":
    unittest.main()
