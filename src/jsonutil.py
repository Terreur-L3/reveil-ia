"""Lecture tolérante du JSON renvoyé par une IA (blocs ```json, texte autour, etc.)."""
from __future__ import annotations

import json
import re

_FENCE_RE = re.compile(r"^```[A-Za-z]*\s*|\s*```$", re.MULTILINE)


def load_json(text: str):
    """Retourne l'objet JSON trouvé dans `text`, ou None si rien d'exploitable."""
    cleaned = _FENCE_RE.sub("", (text or "").strip()).strip()
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
