"""Transcription de la voix en texte avec Groq (Whisper), offre gratuite.

Quotas gratuits à respecter (au moment de l'écriture) : 7 200 secondes d'audio par heure,
28 800 par jour, 20 requêtes par minute, 25 Mo par fichier, 10 secondes facturées au minimum.
Groq ne s'entraîne pas sur les données de ses clients.
"""
from __future__ import annotations

import logging
import re
import time
from pathlib import Path

import requests

log = logging.getLogger(__name__)

GROQ_STT_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
BILLING_MIN_S = 10.0


class STTError(RuntimeError):
    pass


class RateLimited(STTError):
    def __init__(self, retry_after: float, detail: str = ""):
        super().__init__(f"quota Groq atteint, réessayer dans {int(retry_after)} s")
        self.retry_after = retry_after
        self.detail = detail


# Phrases que Whisper « invente » sur du silence ou de la musique (très courantes en français).
_HALLUCINATION_RE = re.compile(
    r"amara\.org|sous-?titr\w*\s+(r[ée]alis\w+|par|de|st|fr)|merci d['’]avoir (regard|suivi|[ée]cout)\w*"
    r"|n['’]oubliez pas de (vous )?abonner|abonnez-vous|à (très )?bient[ôo]t pour une prochaine"
    r"|merci à tous pour|like et partage|^\W*(musique|applaudissements|rires?|silence)\W*$",
    re.IGNORECASE,
)


def billed(seconds: float) -> float:
    """Secondes d'audio facturées (donc décomptées du quota) pour une requête."""
    return max(BILLING_MIN_S, float(seconds or 0))


def prune(history: list, now: float, window: float = 3600.0) -> list:
    return [h for h in history if now - float(h[0]) < window]


def usage_in_window(history: list, now: float, window: float = 3600.0) -> float:
    return sum(float(h[1]) for h in prune(history, now, window))


def _clean_segments(payload: dict) -> str:
    segments = payload.get("segments")
    if not isinstance(segments, list) or not segments:
        text = str(payload.get("text") or "").strip()
        return "" if len(text.split()) <= 14 and _HALLUCINATION_RE.search(text) else text
    kept: list[str] = []
    for seg in segments:
        text = str(seg.get("text") or "").strip()
        if not text:
            continue
        no_speech = float(seg.get("no_speech_prob") or 0)
        logprob = float(seg.get("avg_logprob") or 0)
        if no_speech > 0.6 and logprob < -0.8:
            continue  # probablement du silence ou du bruit
        if float(seg.get("compression_ratio") or 0) > 2.4:
            continue  # répétition en boucle
        if len(text.split()) <= 14 and _HALLUCINATION_RE.search(text):
            continue
        kept.append(text)
    return re.sub(r"\s+", " ", " ".join(kept)).strip()


def _error_detail(resp: requests.Response) -> str:
    try:
        err = resp.json().get("error", {})
        return str(err.get("message", "") if isinstance(err, dict) else err)[:300]
    except Exception:
        return (resp.text or "")[:300]


def _retry_after(resp: requests.Response, detail: str) -> float:
    try:
        return float(resp.headers.get("retry-after", ""))
    except ValueError:
        pass
    m = re.search(r"try again in\s+(?:(\d+)h)?(?:(\d+)m)?(?:(\d+(?:\.\d+)?)s)?", detail)
    if m and any(m.groups()):
        h, mi, s = (float(g) if g else 0.0 for g in m.groups())
        return h * 3600 + mi * 60 + s
    return 60.0


def transcribe(path: Path | str, key: str, *, model: str, language: str = "fr", timeout: int = 180,
               retries: int = 2) -> str:
    """Texte d'un fichier audio. Lève RateLimited (quota) ou STTError."""
    path = Path(path)
    last = ""
    for attempt in range(retries + 1):
        try:
            with open(path, "rb") as fh:
                resp = requests.post(
                    GROQ_STT_URL,
                    headers={"Authorization": f"Bearer {key}"},
                    data={"model": model, "language": language, "response_format": "verbose_json", "temperature": "0"},
                    files={"file": (path.name, fh, "application/octet-stream")},
                    timeout=timeout,
                )
        except requests.RequestException as exc:
            last = f"réseau ({exc.__class__.__name__})"
            time.sleep(5 * (attempt + 1))
            continue
        if resp.status_code == 200:
            try:
                return _clean_segments(resp.json())
            except ValueError as exc:
                raise STTError("réponse illisible de Groq") from exc
        detail = _error_detail(resp)
        if resp.status_code == 429:
            raise RateLimited(_retry_after(resp, detail), detail)
        if resp.status_code == 401:
            raise STTError("clé Groq refusée (GROQ_API_KEY)")
        if resp.status_code in (500, 502, 503, 504):
            last = f"HTTP {resp.status_code}"
            time.sleep(5 * (attempt + 1))
            continue
        raise STTError(f"HTTP {resp.status_code} {detail[:200]}")
    raise STTError(f"transcription impossible ({last})")
