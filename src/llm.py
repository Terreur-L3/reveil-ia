"""Client minimal pour l'API Gemini (REST), avec repli automatique entre plusieurs modèles."""
from __future__ import annotations

import logging
import time

import requests

log = logging.getLogger(__name__)

API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
_TRANSIENT = {500, 502, 503, 504}


class LLMError(RuntimeError):
    pass


def _extract_text(resp: requests.Response) -> str:
    try:
        parts = resp.json()["candidates"][0]["content"]["parts"]
    except (ValueError, KeyError, IndexError, TypeError):
        return ""
    return "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()


def _short_error(resp: requests.Response) -> str:
    try:
        return str(resp.json().get("error", {}).get("message", ""))[:160]
    except Exception:
        return resp.text[:160]


def generate(
    prompt: str,
    *,
    system: str,
    api_key: str,
    models: list[str],
    temperature: float = 0.7,
    timeout: int = 90,
) -> tuple[str, str]:
    """Retourne (texte, modèle_utilisé). Essaie les modèles dans l'ordre."""
    if not api_key:
        raise LLMError("Clé GEMINI_API_KEY absente (secret GitHub ou fichier .env).")

    body = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": temperature},
    }
    headers = {"x-goog-api-key": api_key, "Content-Type": "application/json"}
    errors: list[str] = []

    for model in models:
        for attempt in range(3):
            try:
                resp = requests.post(
                    API_URL.format(model=model), headers=headers, json=body, timeout=timeout
                )
            except requests.RequestException as exc:
                errors.append(f"{model} : réseau ({exc.__class__.__name__})")
                time.sleep(5 * (attempt + 1))
                continue

            if resp.status_code == 200:
                text = _extract_text(resp)
                if text:
                    log.info("Texte généré avec %s", model)
                    return text, model
                errors.append(f"{model} : réponse vide")
                break

            detail = _short_error(resp)
            errors.append(f"{model} : HTTP {resp.status_code} {detail}")
            if resp.status_code in (400, 401, 403) and "api key" in detail.lower():
                raise LLMError(f"Clé API refusée par Google : {detail}")
            if resp.status_code in _TRANSIENT:
                time.sleep(8 * (attempt + 1))
                continue
            if resp.status_code == 429 and attempt == 0:
                time.sleep(20)
                continue
            break  # modèle indisponible ou quota épuisé → modèle suivant

    raise LLMError("Aucun modèle n'a répondu :\n  " + "\n  ".join(errors))
