"""Ajoute à TON config.yaml la section « contexte » (réglages de ton document de contexte, /moi dans Telegram).

Ce que ça change, et seulement ça : un petit bloc `contexte:` avec trois réglages, placé après la section `perso`
(ou à la fin du fichier). Le reste de ton config.yaml n'est pas touché. L'ancien fichier est gardé dans
config.yaml.avant-contexte.

    python3 outils/maj_config_contexte.py            # applique
    python3 outils/maj_config_contexte.py --verifier # dit seulement ce qu'il ferait

Relancer l'outil ne fait rien de plus (il reconnaît un config.yaml déjà à jour). Il est facultatif : sans lui, le document
de contexte marche pareil, avec exactement les mêmes valeurs (elles sont écrites ici et reprises par le programme).
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

BLOCK = """# ----- Ton document de contexte : qui tu es, tes hobbies, tes goûts, comment l'IA doit te parler… -----
# C'est le fichier memoire/contexte.md, que tu lis et modifies depuis Telegram (/moi). Les sections marquées 🔒 ne partent JAMAIS
# vers Gemini : seul Groq les lit (questions « Inspi », rangement de tes phrases). Celles marquées 🌐 peuvent aider à écrire le
# podcast, sauf les éléments sensibles (santé, argent, politique…), qui restent privés. /moi apercu montre ce que chaque IA reçoit.
contexte:
  dans_podcast: true         # false = tes sections 🌐 « libres » (hobbies, goûts…) n'accompagnent pas l'écriture du podcast
                             # (tes consignes de style et tes sujets d'actus, eux, ont leurs propres réglages : perso / moi.yaml)
  budget_partage: 900        # caractères de tes sections 🌐 libres envoyés à l'IA qui écrit le podcast (Gemini)
  budget_prive: 3000         # caractères de ton document que Groq lit pour te poser de bonnes questions
"""
EXPECTED = {"dans_podcast": True, "budget_partage": 900, "budget_prive": 3000}

_SECTION_RE = re.compile(r"^contexte:[ \t]*(?:#.*)?$", re.M)
_PERSO_RE = re.compile(r"^perso:[ \t]*(?:#.*)?$", re.M)


def _end_of_perso(text: str) -> int | None:
    """Position (dans la liste des lignes) juste après la dernière ligne de la section perso ; None si elle n'existe pas."""
    found = _PERSO_RE.search(text)
    if found is None:
        return None
    lines = text.splitlines(keepends=True)
    last = text.count("\n", 0, found.start())               # numéro de la ligne « perso: »
    for i in range(last + 1, len(lines)):
        if lines[i].strip() == "":
            continue
        if not lines[i][0].isspace():                       # une clé ou un commentaire collé à gauche : fin de la section
            break
        last = i
    return last + 1


def migrate(text: str) -> tuple[str, str]:
    """Retourne (nouveau texte, message). Le texte est inchangé si rien à faire."""
    if _SECTION_RE.search(text):
        return text, "Ton config.yaml est déjà à jour (section contexte)."
    lines = text.splitlines(keepends=True)
    at = _end_of_perso(text)
    if at is None:
        if text and not text.endswith("\n"):
            text += "\n"
        return text + ("\n" if text.strip() else "") + BLOCK, "Section « contexte » ajoutée à la fin de ton config.yaml."
    if lines and not lines[at - 1].endswith("\n"):
        lines[at - 1] += "\n"
    lines.insert(at, "\n" + BLOCK)
    return "".join(lines), "Section « contexte » ajoutée après la section « perso »."


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default=str(Path(__file__).resolve().parents[1] / "config.yaml"))
    parser.add_argument("--verifier", action="store_true", help="ne modifie rien")
    args = parser.parse_args(argv)
    path = Path(args.config)
    text = path.read_text(encoding="utf-8")
    new, message = migrate(text)
    print(message)
    if new == text or args.verifier:
        return 0
    try:                                   # on ne remplace jamais le fichier par quelque chose d'illisible
        import yaml

        before, after = yaml.safe_load(text) or {}, yaml.safe_load(new)
        assert isinstance(after, dict) and after.get("contexte") == EXPECTED, "la section n'est pas lue comme prévu"
        assert {k: v for k, v in after.items() if k != "contexte"} == before, "autre chose que « contexte » a changé"
    except ImportError:
        pass
    except Exception as exc:
        print(f"ERREUR : le résultat n'est pas valide ({exc.__class__.__name__} : {exc}) : rien n'a été modifié.")
        return 1
    path.with_name(path.name + ".avant-contexte").write_text(text, encoding="utf-8")
    path.write_text(new, encoding="utf-8")
    print(f"Ancienne version gardée dans {path.name}.avant-contexte")
    return 0


if __name__ == "__main__":
    sys.exit(main())
