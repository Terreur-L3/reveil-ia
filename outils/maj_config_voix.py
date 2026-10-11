"""Met à jour la section « voix » de TON config.yaml pour la voix réaliste (Gemini), sans toucher au reste.

Pourquoi un outil plutôt qu'un fichier de remplacement : ton config.yaml contient tes réglages (flux, durée, etc.).
Cet outil ne remplace QUE le bloc « ----- Voix du podcast ----- », garde ta voix Edge et ta vitesse actuelles,
et sauvegarde l'ancien fichier dans config.yaml.avant-voix.

    python3 outils/maj_config_voix.py            # applique
    python3 outils/maj_config_voix.py --verifier # dit seulement ce qu'il ferait

Relancer l'outil ne fait rien de plus (il reconnaît un config.yaml déjà à jour).
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

HEADER = "# ----- Voix du podcast -----"

BLOCK = '''# ----- Voix du podcast -----
voix:
  moteur: gemini          # gemini = voix très naturelles (gratuit, clé GEMINI_API_KEY) ; edge = voix classiques
                          # Si Gemini ne répond pas (quota, panne), le podcast sort quand même avec la voix Edge.
  gemini:
    voix: Sulafat         # Sulafat (chaleureuse), Achird (amicale), Kore, Charon, Orus, Algieba, Puck, Zephyr…
                          # Pour écouter : Telegram → /voix essai ; pour choisir : /voix sulafat
                          # Une voix créée sur mesure (« Voice design », nom en voice_…) se met ici aussi.
    style: "warm, natural and relaxed, like a friendly morning radio host chatting with a close friend; calm smooth pace, a light smile in the voice"
    modeles: [gemini-3.8-flash-tts, gemini-3.8-flash-lite-tts]   # essayés dans l'ordre
    delai_max_s: 90       # temps maximum passé à réessayer (quota, surcharge) avant la voix de secours
  edge_voix: {edge_voix}   # voix de secours (et voix des questions « Inspi », qui restent privées)
                          # Autres voix Edge : fr-FR-DeniseNeural, fr-FR-RemyMultilingualNeural, fr-FR-VivienneMultilingualNeural
  vitesse: "{vitesse}"          # ex. "+10%" pour parler plus vite, "-10%" plus lentement (valable pour les deux moteurs)
  essai: [Sulafat, Achird, Kore, Charon, Orus, Algieba]   # voix envoyées par « /voix essai »
  essai_pause_s: 12       # pause entre deux échantillons (le quota gratuit limite les appels par minute)

'''


def find_block(lines: list[str]) -> tuple[int, int] | None:
    """(début, fin) du bloc voix : de l'en-tête jusqu'au prochain en-tête « # ----- » (exclu)."""
    start = next((i for i, line in enumerate(lines) if line.strip() == HEADER), None)
    if start is None:
        return None
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("# -----")), len(lines))
    return start, end


def migrate(text: str) -> tuple[str, str]:
    """Retourne (nouveau texte, message). Le texte est inchangé si rien à faire."""
    if re.search(r"^\s{2}gemini:\s*$", text, re.M) and re.search(r"^\s{2}moteur:\s*gemini", text, re.M):
        return text, "Ton config.yaml est déjà à jour (voix Gemini)."
    lines = text.splitlines(keepends=True)
    found = find_block(lines)
    if found is None:
        return text, "ERREUR : je ne trouve pas le bloc « ----- Voix du podcast ----- » dans config.yaml (rien n'a été modifié)."
    start, end = found
    old = "".join(lines[start:end])
    edge = re.search(r"^\s{2}edge_voix:\s*([\w-]+)", old, re.M)
    speed = re.search(r"^\s{2}vitesse:\s*\"?([+-]?\d{1,2}%)\"?", old, re.M)
    block = BLOCK.format(edge_voix=edge.group(1) if edge else "fr-FR-HenriNeural", vitesse=speed.group(1) if speed else "+0%")
    return "".join(lines[:start]) + block + "".join(lines[end:]), "Section « voix » mise à jour : voix Gemini activée, ta voix Edge et ta vitesse gardées."


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default=str(Path(__file__).resolve().parents[1] / "config.yaml"))
    parser.add_argument("--verifier", action="store_true", help="ne modifie rien")
    args = parser.parse_args(argv)
    path = Path(args.config)
    text = path.read_text(encoding="utf-8")
    new, message = migrate(text)
    print(message)
    if message.startswith("ERREUR"):
        return 1
    if new == text or args.verifier:
        return 0
    try:                                   # on ne remplace jamais le fichier par quelque chose d'illisible
        import yaml

        data = yaml.safe_load(new)
        assert data["voix"]["moteur"] == "gemini" and data["voix"]["gemini"]["voix"]
    except ImportError:
        pass
    except Exception as exc:
        print(f"ERREUR : le résultat n'est pas valide ({exc.__class__.__name__}) : rien n'a été modifié.")
        return 1
    path.with_name(path.name + ".avant-voix").write_text(text, encoding="utf-8")
    path.write_text(new, encoding="utf-8")
    print(f"Ancienne version gardée dans {path.name}.avant-voix")
    return 0


if __name__ == "__main__":
    sys.exit(main())
