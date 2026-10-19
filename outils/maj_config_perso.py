"""Met à jour la section « perso » de TON config.yaml pour les consignes sur tes cours, anniversaires et rappels.

Ce que ça change, et seulement ça :
  1. le commentaire « (jamais envoyées à une IA) » devient exact : si tu as une consigne sur la façon de dire tes
     cours, anniversaires ou rappels, ces phrases sont reformulées par Groq (jamais par Gemini) puis contrôlées ;
  2. la ligne `style_ia: true` est ajoutée dans la section `perso` (mets `false` pour que plus rien ne parte vers Groq).

Le reste de ton config.yaml n'est pas touché. L'ancien fichier est gardé dans config.yaml.avant-perso.

    python3 outils/maj_config_perso.py            # applique
    python3 outils/maj_config_perso.py --verifier # dit seulement ce qu'il ferait

Relancer l'outil ne fait rien de plus (il reconnaît un config.yaml déjà à jour). Sans lui, tout marche quand même :
`style_ia` vaut « true » quand la ligne est absente.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

OLD_HEADER = "# ----- Infos perso dites par le programme (jamais envoyées à une IA) -----"
NEW_HEADER = """# ----- Infos perso : cours, anniversaires, rappels (phrases écrites par le programme) -----
# Si tu as une consigne sur la façon de les dire (« /consigne cite mes cours sans la salle »), Groq (jamais Gemini) les
# reformule, puis le programme vérifie qu'aucune heure ni aucun nom n'a changé. Sans consigne de ce genre : aucune IA."""
STYLE_LINE = "  style_ia: true             # false = tes consignes ne s'appliquent pas à ces phrases : rien n'est envoyé à Groq"

_PERSO_RE = re.compile(r"^perso:[ \t]*(?:#.*)?$", re.M)
_STYLE_RE = re.compile(r"^[ \t]+style_ia[ \t]*:", re.M)


def _add_style_line(text: str) -> str | None:
    """Ajoute `style_ia` à la fin de la section perso (None si la section n'existe pas)."""
    found = _PERSO_RE.search(text)
    if found is None:
        return None
    lines = text.splitlines(keepends=True)
    start = text.count("\n", 0, found.start())              # numéro de la ligne « perso: »
    last = start
    for i in range(start + 1, len(lines)):
        if lines[i].strip() == "":
            continue
        if not lines[i][0].isspace():                       # une clé ou un commentaire collé à gauche : fin de la section
            break
        last = i
    if not lines[last].endswith("\n"):
        lines[last] += "\n"
    lines.insert(last + 1, STYLE_LINE + "\n")
    return "".join(lines)


def migrate(text: str) -> tuple[str, str]:
    """Retourne (nouveau texte, message). Le texte est inchangé si rien à faire."""
    done = []
    if OLD_HEADER in text:
        text = text.replace(OLD_HEADER, NEW_HEADER, 1)
        done.append("commentaire corrigé")
    if not _STYLE_RE.search(text):
        added = _add_style_line(text)
        if added is None:
            if done:
                return text, "Commentaire corrigé ; je ne trouve pas la section « perso: » (rien d'autre à faire : `style_ia` vaut « true » par défaut)."
            return text, "Je ne trouve pas la section « perso: » dans config.yaml : rien à faire (`style_ia` vaut « true » par défaut)."
        text = added
        done.append("`style_ia: true` ajouté")
    if not done:
        return text, "Ton config.yaml est déjà à jour (section perso)."
    return text, "Section « perso » mise à jour : " + " et ".join(done) + "."


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
        assert isinstance(after, dict) and isinstance(after.get("perso"), dict)
        stripped = {k: v for k, v in after["perso"].items() if k != "style_ia"}
        assert stripped == (before.get("perso") or {}) and {k: v for k, v in after.items() if k != "perso"} == {
            k: v for k, v in before.items() if k != "perso"}, "autre chose que style_ia a changé"
    except ImportError:
        pass
    except Exception as exc:
        print(f"ERREUR : le résultat n'est pas valide ({exc.__class__.__name__}) : rien n'a été modifié.")
        return 1
    path.with_name(path.name + ".avant-perso").write_text(text, encoding="utf-8")
    path.write_text(new, encoding="utf-8")
    print(f"Ancienne version gardée dans {path.name}.avant-perso")
    return 0


if __name__ == "__main__":
    sys.exit(main())
