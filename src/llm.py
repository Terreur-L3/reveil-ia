"""Clients LLM (Gemini et Groq) avec repli automatique le long d'une « chaîne » de modèles.

Une chaîne est une liste d'entrées {fournisseur, modele, extra?}, essayées dans l'ordre.
Un fournisseur dont la clé est absente ou refusée est simplement sauté, les autres continuent.
"""
from __future__ import annotations

import logging
import time

import requests

log = logging.getLogger(__name__)

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
KEY_ENV = {"gemini": "GEMINI_API_KEY", "groq": "GROQ_API_KEY"}
_TRANSIENT = {500, 502, 503, 504}


class LLMError(RuntimeError):
    pass


def keys_from_env(environ) -> dict[str, str]:
    """Lit les clés API dans l'environnement (secrets GitHub ou fichier .env)."""
    return {prov: (environ.get(var) or "").strip() for prov, var in KEY_ENV.items()}


def _error_text(resp: requests.Response, limit: int = 600) -> str:
    try:
        err = resp.json().get("error", {})
        msg = err.get("message", "") if isinstance(err, dict) else str(err)
        return str(msg)[:limit]
    except Exception:
        return (resp.text or "")[:limit]


# --------------------------------------------------------------------------- Gemini
def _call_gemini(model, system, prompt, key, temperature, json_mode, max_tokens, timeout, extra):
    config = {"temperature": temperature}
    if json_mode:
        config["responseMimeType"] = "application/json"
    if max_tokens:
        config["maxOutputTokens"] = max_tokens
    body = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": config,
    }
    headers = {"x-goog-api-key": key, "Content-Type": "application/json"}
    return requests.post(GEMINI_URL.format(model=model), headers=headers, json=body, timeout=timeout)


def _text_gemini(resp: requests.Response) -> str:
    try:
        parts = resp.json()["candidates"][0]["content"]["parts"]
    except (ValueError, KeyError, IndexError, TypeError):
        return ""
    return "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()


# --------------------------------------------------------------------------- Groq
def _call_groq(model, system, prompt, key, temperature, json_mode, max_tokens, timeout, extra):
    body = {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
        "temperature": temperature,
    }
    if max_tokens:
        body["max_completion_tokens"] = max_tokens
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    body.update(extra or {})
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    return requests.post(GROQ_URL, headers=headers, json=body, timeout=timeout)


def _text_groq(resp: requests.Response) -> str:
    try:
        message = resp.json()["choices"][0]["message"]
    except (ValueError, KeyError, IndexError, TypeError):
        return ""
    return (message.get("content") or "").strip()


_PROVIDERS = {"gemini": (_call_gemini, _text_gemini), "groq": (_call_groq, _text_groq)}


def _retry_wait(resp: requests.Response) -> float:
    try:
        return float(resp.headers.get("retry-after", ""))
    except ValueError:
        return 20.0


def generate(
    prompt: str,
    *,
    system: str,
    chain: list[dict],
    keys: dict[str, str],
    temperature: float = 0.7,
    json_mode: bool = False,
    max_tokens: int | None = None,
    timeout: int = 90,
) -> tuple[str, str]:
    """Retourne (texte, « fournisseur/modèle » utilisé). Essaie la chaîne dans l'ordre."""
    errors: list[str] = []
    dead: set[str] = set()
    if json_mode and "json" not in (system + prompt).lower():
        system += "\nRéponds en JSON."  # Groq exige le mot « JSON » en mode JSON ; sans ce mode, il sert encore de consigne

    for entry in chain or []:
        prov = str(entry.get("fournisseur", "gemini")).lower()
        model = entry.get("modele")
        label = f"{prov}/{model}"
        if prov not in _PROVIDERS or not model:
            errors.append(f"{label} : entrée de chaîne invalide")
            continue
        if prov in dead:
            continue
        key = keys.get(prov, "")
        if not key:
            errors.append(f"{prov} : clé {KEY_ENV[prov]} absente")
            dead.add(prov)
            continue

        call, extract = _PROVIDERS[prov]
        json_on = json_mode
        for attempt in range(3):
            try:
                resp = call(
                    model, system, prompt, key, temperature, json_on, max_tokens, timeout,
                    entry.get("extra"),
                )
            except requests.RequestException as exc:
                errors.append(f"{label} : réseau ({exc.__class__.__name__})")
                time.sleep(5 * (attempt + 1))
                continue

            if resp.status_code == 200:
                text = extract(resp)
                if text:
                    log.info("Texte généré avec %s", label)
                    return text, label
                errors.append(f"{label} : réponse vide")
                break

            detail = _error_text(resp)
            low = detail.lower()
            errors.append(f"{label} : HTTP {resp.status_code} {detail[:160]}")
            if resp.status_code == 401 or (resp.status_code in (400, 403) and "api key" in low):
                errors.append(f"{prov} : clé refusée, fournisseur abandonné")
                dead.add(prov)
                break
            if resp.status_code == 400 and json_on and ("response_format" in low or "json" in low):
                json_on = False  # ce modèle refuse le « mode JSON » : le prompt demande déjà du JSON, on réessaie sans
                errors.append(f"{label} : mode JSON refusé, je réessaie sans")
                continue
            if resp.status_code in _TRANSIENT:
                time.sleep(8 * (attempt + 1))
                continue
            if resp.status_code == 429 and attempt == 0 and "limit: 0" not in low:
                wait = _retry_wait(resp)
                if wait <= 60:  # au-delà, c'est un quota journalier : inutile d'attendre
                    time.sleep(max(wait, 1.0))
                    continue
            break  # modèle indisponible ou quota épuisé → entrée suivante

    raise LLMError("Aucun modèle n'a répondu :\n  " + "\n  ".join(errors or ["chaîne vide"]))
