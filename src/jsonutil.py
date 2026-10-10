"""Lecture tolérante du JSON renvoyé par une IA (blocs ```json, texte autour, « réflexion » visible, etc.)."""
from __future__ import annotations

import json
import re

_FENCE_RE = re.compile(r"^```[A-Za-z]*\s*|\s*```$", re.MULTILINE)
# Certains modèles « qui réfléchissent » laissent leur raisonnement entre <think> et </think> (ou le coupent en route).
_THINK_RE = re.compile(r"<think>.*?(?:</think>|\Z)", re.DOTALL | re.IGNORECASE)


def strip_think(text: str) -> str:
    """Retire le raisonnement interne d'un modèle (jamais destiné à être lu ni lu à voix haute)."""
    return _THINK_RE.sub("", text or "").strip()


def load_json(text: str):
    """Retourne l'objet JSON trouvé dans `text`, ou None si rien d'exploitable."""
    cleaned = _FENCE_RE.sub("", strip_think(text)).strip()
    try:
        return json.loads(cleaned)
    except ValueError:
        pass
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(cleaned[start:end + 1])
        except ValueError:
            return None
    return None
