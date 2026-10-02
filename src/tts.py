"""Synthèse vocale : texte → mp3.

Moteur actuel : Edge TTS (gratuit, voix neuronales françaises, non officiel).
Plus tard (phase 4) : un moteur « xtts » pour ta propre voix.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path

log = logging.getLogger(__name__)

FALLBACK_VOICES = ["fr-FR-HenriNeural", "fr-FR-DeniseNeural"]


async def _edge_save(text: str, voice: str, rate: str, out_path: Path) -> None:
    import edge_tts

    await edge_tts.Communicate(text, voice, rate=rate).save(str(out_path))


def _edge(text: str, cfg: dict, out_path: Path, retries: int = 3) -> str:
    voice = cfg.get("edge_voix", FALLBACK_VOICES[0])
    rate = cfg.get("vitesse", "+0%")
    candidates = [voice] + [v for v in FALLBACK_VOICES if v != voice]
    last: Exception | None = None

    # On écrit dans un fichier temporaire : en cas d'échec, edge-tts laisse un fichier vide
    # derrière lui, et on ne veut surtout pas publier un mp3 cassé.
    part = out_path.with_name(out_path.name + ".part")

    for v in candidates:
        for attempt in range(retries):
            try:
                asyncio.run(_edge_save(text, v, rate, part))
                if part.exists() and part.stat().st_size > 5000:
                    os.replace(part, out_path)
                    return v
                raise RuntimeError("fichier audio vide")
            except Exception as exc:
                last = exc
                log.warning("Edge TTS (%s) essai %d/%d : %s", v, attempt + 1, retries, exc)
                part.unlink(missing_ok=True)
                time.sleep(3 * (attempt + 1))

    raise RuntimeError(f"Edge TTS indisponible ({last}). Essaie : pip install -U edge-tts")


def synthesize(text: str, cfg: dict, out_path: Path | str) -> str:
    """Écrit le mp3 et retourne le nom de la voix utilisée."""
    engine = cfg.get("moteur", "edge")
    if engine == "edge":
        return _edge(text, cfg, Path(out_path))
    raise ValueError(f"Moteur vocal inconnu ou pas encore branché : {engine}")
