"""Synthèse vocale : texte → mp3.

Deux moteurs, tous deux gratuits :
  - « gemini » : Gemini TTS (voix très naturelles, qui respirent et mettent le ton). Essayé en premier quand
    config.yaml dit `moteur: gemini` et que la clé GEMINI_API_KEY est là.
  - « edge »   : Edge TTS (voix neuronales, plus « lues »). Sert aussi de SECOURS : si Gemini ne répond pas
    (quota du jour atteint, panne, clé absente…), le podcast sort quand même, avec la voix Edge, et la raison est
    notée dans meta.json (« voix_note ») : un réveil ne finit jamais en silence.

Ce qui est envoyé à Gemini : uniquement le texte du podcast (jamais le journal, jamais la mémoire). Les textes
tirés de ton journal privé (la question « Inspi ») passent avec `private=True` : ils restent sur Edge.

Essai des voix :  python -m src.tts --essai   (envoie quelques échantillons dans Telegram ; voir voix.yml)
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import io
import logging
import os
import re
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path

import requests

log = logging.getLogger(__name__)

FALLBACK_VOICES = ["fr-FR-HenriNeural", "fr-FR-DeniseNeural"]

INTERACTIONS_URL = "https://generativelanguage.googleapis.com/v1beta/interactions"
LEGACY_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

DEFAULT_GEMINI = {
    "modeles": ["gemini-3.8-flash-tts", "gemini-3.8-flash-lite-tts"],
    "voix": "Sulafat",
    "style": ("warm, natural and relaxed, like a friendly morning radio host chatting with a close friend; "
              "calm smooth pace, a light smile in the voice"),
    "delai_max_s": 90,          # on réessaie (quota, surcharge) pendant ce temps au plus, puis voix de secours
    "taille_morceau": 4500,     # les textes plus longs sont dits en plusieurs morceaux, recollés ensuite
    "modele_ancien": "gemini-3.1-flash-tts-preview",   # essayé seulement si l'API « interactions » est refusée
}

_last_note = ""


def last_note() -> str:
    """Pourquoi la dernière synthèse n'a pas utilisé le moteur demandé (« » si tout s'est passé normalement)."""
    return _last_note


# --------------------------------------------------------------------------- Edge TTS (voix de secours)
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


# --------------------------------------------------------------------------- Gemini TTS
class GeminiTTSError(RuntimeError):
    """`fatal` = inutile de réessayer ou de changer de modèle (clé refusée…)."""

    def __init__(self, message: str, *, fatal: bool = False, status: int = 0):
        super().__init__(message)
        self.fatal = fatal
        self.status = status


def _api_error_text(resp: requests.Response, limit: int = 300) -> str:
    try:
        data = resp.json()
        text = (data.get("error") or {}).get("message") or str(data)
    except ValueError:
        text = resp.text
    return " ".join(str(text).split())[:limit]


def _split_text(text: str, size: int) -> list[str]:
    """Coupe aux fins de phrases, en morceaux d'au plus `size` caractères."""
    text = " ".join(text.split())
    if len(text) <= size:
        return [text]
    sentences = re.split(r"(?<=[.!?…])\s+", text)
    chunks: list[str] = []
    current = ""
    for sentence in sentences:
        while len(sentence) > size:                       # phrase interminable : on coupe à un espace
            cut = sentence.rfind(" ", 0, size)
            if cut <= 0:
                cut = size
            head, sentence = sentence[:cut], sentence[cut:].lstrip()
            if current:
                chunks.append(current)
                current = ""
            chunks.append(head)
        if current and len(current) + 1 + len(sentence) > size:
            chunks.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        chunks.append(current)
    return [c for c in chunks if c]


def _wav_from_audio(data: bytes, mime: str = "") -> bytes:
    """Octets reçus → fichier WAV complet (24 kHz, mono, 16 bits si l'audio arrive « brut »)."""
    if data[:4] == b"RIFF":
        return data
    rate = 24000
    m = re.search(r"rate=(\d+)", mime or "")
    if m:
        rate = int(m.group(1))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(data)
    return buf.getvalue()


def _find_audio_interactions(payload: dict) -> tuple[str, str] | None:
    """Le dernier bloc audio d'une réponse « interactions » : (données base64, type)."""
    found = None
    for step in payload.get("steps") or []:
        if not isinstance(step, dict) or step.get("type") != "model_output":
            continue
        for item in step.get("content") or []:
            if isinstance(item, dict) and item.get("type") == "audio" and item.get("data"):
                found = (item["data"], str(item.get("mime_type") or ""))
    return found


def _find_audio_legacy(payload: dict) -> tuple[str, str] | None:
    for cand in payload.get("candidates") or []:
        for part in ((cand or {}).get("content") or {}).get("parts") or []:
            inline = (part or {}).get("inlineData") or (part or {}).get("inline_data") or {}
            if inline.get("data"):
                return inline["data"], str(inline.get("mimeType") or inline.get("mime_type") or "")
    return None


def _post(url: str, key: str, body: dict, timeout: int) -> requests.Response:
    return requests.post(url, headers={"x-goog-api-key": key, "Content-Type": "application/json"},
                         json=body, timeout=timeout)


def _one_call(text: str, voice: str, model: str, style: str, key: str, legacy_model: str, timeout: int = 120) -> bytes:
    """Un seul appel Gemini TTS → WAV. Lève GeminiTTSError (fatale ou non)."""
    body = {
        "model": model,
        "input": [{"type": "user_input", "content": [{
            "type": "text", "text": text,
            **({"annotations": [{"type": "speech_metadata", "style": style}]} if style else {}),
        }]}],
        "response_format": {"type": "audio"},
        "generation_config": {"speech_config": [{"voice": voice}]},
    }
    resp = _post(INTERACTIONS_URL, key, body, timeout)
    found = None
    if resp.status_code == 200:
        found = _find_audio_interactions(resp.json())
        if not found:
            raise GeminiTTSError("Gemini a répondu sans audio", status=200)
    elif resp.status_code in (400, 404, 405, 422) and legacy_model and "voice" not in _api_error_text(resp).lower():
        # L'API « interactions » ne reconnaît pas la requête : on tente l'ancienne forme, une fois.
        legacy_text = f"Say this in a {style} way, word for word: {text}" if style else text
        legacy = {
            "contents": [{"parts": [{"text": legacy_text}]}],
            "generationConfig": {"responseModalities": ["AUDIO"], "speechConfig": {
                "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice}}}},
        }
        resp = _post(LEGACY_URL.format(model=legacy_model), key, legacy, timeout)
        if resp.status_code == 200:
            found = _find_audio_legacy(resp.json())
            if not found:
                raise GeminiTTSError("Gemini a répondu sans audio", status=200)
    if found is None:
        code = resp.status_code
        text_err = _api_error_text(resp)
        fatal = code in (401, 403) or (code == 400 and re.search(r"api key|permission|billing", text_err, re.I) is not None)
        raise GeminiTTSError(f"Gemini TTS a répondu {code} : {text_err}", fatal=fatal, status=code)
    data, mime = found
    try:
        return _wav_from_audio(base64.b64decode(data), mime)
    except Exception as exc:
        raise GeminiTTSError(f"audio illisible ({exc.__class__.__name__})") from exc


def _retry_wait(resp_error: GeminiTTSError, attempt: int) -> float:
    return min(30.0, 6.0 * (attempt + 1))


def _gemini_wav(text: str, gcfg: dict, key: str, voice_override: str | None = None) -> tuple[bytes, str, str]:
    """Texte → (WAV, voix, modèle). Réessaie un moment (quota, surcharge), essaie les modèles dans l'ordre."""
    voice = voice_override or str(gcfg.get("voix") or DEFAULT_GEMINI["voix"])
    models = [m for m in (gcfg.get("modeles") or DEFAULT_GEMINI["modeles"]) if m]
    style = str(gcfg.get("style") if gcfg.get("style") is not None else DEFAULT_GEMINI["style"]).strip()
    budget = float(gcfg.get("delai_max_s", DEFAULT_GEMINI["delai_max_s"]))
    legacy_model = str(gcfg.get("modele_ancien", DEFAULT_GEMINI["modele_ancien"]) or "")
    size = int(gcfg.get("taille_morceau", DEFAULT_GEMINI["taille_morceau"]))
    chunks = _split_text(text, max(300, size))
    started = time.monotonic()
    waited = 0.0                     # les pauses déjà faites (compte aussi quand le temps est simulé dans les tests)
    last: Exception | None = None

    def spent() -> float:
        return max(waited, time.monotonic() - started)

    for model in models:
        wavs: list[bytes] = []
        failed = False
        for chunk in chunks:
            attempt = 0
            while True:
                try:
                    wavs.append(_one_call(chunk, voice, model, style, key, legacy_model if model == models[0] else ""))
                    break
                except GeminiTTSError as exc:
                    last = exc
                    log.warning("Gemini TTS (%s, %s) : %s", model, voice, exc)
                    if exc.fatal:
                        raise
                    retryable = exc.status in (0, 429, 500, 502, 503, 504)
                    if not retryable or spent() + _retry_wait(exc, attempt) > budget:
                        failed = True
                        break
                    time.sleep(_retry_wait(exc, attempt))
                    waited += _retry_wait(exc, attempt)
                    attempt += 1
                except requests.RequestException as exc:
                    last = GeminiTTSError(f"réseau : {exc.__class__.__name__}")
                    log.warning("Gemini TTS (%s) : %s", model, exc.__class__.__name__)
                    if spent() + 6 > budget:
                        failed = True
                        break
                    time.sleep(6)
                    waited += 6
            if failed:
                break
        if not failed and wavs:
            return _join_wavs(wavs), voice, model
    raise last or GeminiTTSError("Gemini TTS n'a rien renvoyé")


def _join_wavs(wavs: list[bytes]) -> bytes:
    if len(wavs) == 1:
        return wavs[0]
    out = io.BytesIO()
    params = None
    with wave.open(out, "wb") as w:
        for blob in wavs:
            with wave.open(io.BytesIO(blob), "rb") as r:
                if params is None:
                    params = r.getparams()
                    w.setparams(params)
                elif (r.getnchannels(), r.getsampwidth(), r.getframerate()) != (params.nchannels, params.sampwidth, params.framerate):
                    raise GeminiTTSError("morceaux audio de formats différents")
                w.writeframes(r.readframes(r.getnframes()))
    return out.getvalue()


def _speed_factor(rate: str) -> float:
    """« +10% » → 1.10 ; « -15% » → 0.85 (limites : 0,7 à 1,4)."""
    m = re.fullmatch(r"\s*([+-]?)(\d{1,2})%\s*", str(rate or ""))
    if not m:
        return 1.0
    value = int(m.group(2)) * (-1 if m.group(1) == "-" else 1)
    return max(0.7, min(1.4, 1 + value / 100))


def _wav_to_mp3(wav: bytes, out_path: Path, factor: float = 1.0) -> None:
    from . import audio

    ffmpeg = audio.ensure_ffmpeg()
    if not ffmpeg:
        raise GeminiTTSError("ffmpeg est nécessaire pour fabriquer le mp3")
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "voix.wav"
        src.write_bytes(wav)
        part = out_path.with_name(out_path.name + ".part.mp3")
        cmd = [ffmpeg, "-loglevel", "error", "-y", "-i", str(src)]
        if abs(factor - 1.0) > 0.005:
            cmd += ["-filter:a", f"atempo={factor:.3f}"]
        cmd += ["-ac", "1", "-ar", "44100", "-c:a", "libmp3lame", "-b:a", "96k", str(part)]
        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=180)
            if not part.exists() or part.stat().st_size < 3000:
                raise GeminiTTSError("mp3 vide")
            os.replace(part, out_path)
        except subprocess.CalledProcessError as exc:
            raise GeminiTTSError(f"ffmpeg a échoué ({(exc.stderr or b'').decode('utf-8', 'ignore')[-200:]})") from exc
        finally:
            part.unlink(missing_ok=True)


def _gemini(text: str, cfg: dict, out_path: Path, key: str) -> str:
    gcfg = {**DEFAULT_GEMINI, **(cfg.get("gemini") or {})}
    wav, voice, model = _gemini_wav(text, gcfg, key)
    _wav_to_mp3(wav, out_path, _speed_factor(cfg.get("vitesse", "+0%")))
    return f"gemini:{voice}"


# --------------------------------------------------------------------------- l'entrée unique
def synthesize(text: str, cfg: dict, out_path: Path | str, *, private: bool = False) -> str:
    """Écrit le mp3 et retourne le nom de la voix utilisée.

    `private=True` : le texte vient de ton journal ou de ta mémoire → jamais envoyé à Gemini (Edge seulement).
    """
    global _last_note
    _last_note = ""
    out_path = Path(out_path)
    engine = str(cfg.get("moteur", "edge")).lower()
    if engine not in ("edge", "gemini"):
        raise ValueError(f"Moteur vocal inconnu ou pas encore branché : {engine}")

    if engine == "gemini" and not private:
        key = (os.environ.get("GEMINI_API_KEY") or "").strip()
        if not key:
            _last_note = "clé GEMINI_API_KEY absente : voix Edge à la place"
        else:
            try:
                return _gemini(text, cfg, out_path, key)
            except Exception as exc:
                reason = str(exc).splitlines()[0][:220] if str(exc) else exc.__class__.__name__
                _last_note = f"voix Gemini indisponible ({reason}) : voix Edge à la place"
                log.warning("%s", _last_note)
                out_path.unlink(missing_ok=True)
    return _edge(text, cfg, out_path)


# --------------------------------------------------------------------------- essai des voix
ESSAI_PAR_DEFAUT = ["Sulafat", "Achird", "Kore", "Charon", "Orus", "Algieba"]
ESSAI_TEXTE = ("Bonjour ! On est {jour}. Il fera doux ce matin, et côté actus, une bonne nouvelle : "
               "un nouveau procédé pourrait réduire la pollution des océans. "
               "Allez, respire un coup, et on démarre la journée tranquillement.")


def _essai(args: argparse.Namespace) -> int:
    import yaml

    from . import reglages, telegram
    from .fr import JOURS, MOIS

    root = Path(args.racine).resolve()
    cfg = yaml.safe_load((root / "config.yaml").read_text(encoding="utf-8")) or {}
    voix_cfg = cfg.get("voix") or {}
    gcfg = {**DEFAULT_GEMINI, **(voix_cfg.get("gemini") or {})}
    key = (os.environ.get("GEMINI_API_KEY") or "").strip()
    bot = telegram.Bot.from_env(os.environ)
    if bot is None:
        print("Telegram n'est pas configuré (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID).")
        return 2

    wanted = []
    for word in re.split(r"[\s,;]+", args.voix or os.environ.get("ESSAI_VOIX", "")):
        name = reglages.GEMINI_BY_LOWER.get(word.strip().lower())
        if name and name not in wanted:
            wanted.append(name)
    wanted = wanted[:8] or list(voix_cfg.get("essai") or ESSAI_PAR_DEFAUT)[:8]

    from datetime import datetime
    from zoneinfo import ZoneInfo

    now = datetime.now(ZoneInfo(cfg.get("fuseau", "Europe/Paris")))
    text = ESSAI_TEXTE.format(jour=f"{JOURS[now.weekday()]} {now.day} {MOIS[now.month - 1]}")
    pause = float(voix_cfg.get("essai_pause_s", 12))
    out = Path(tempfile.mkdtemp(prefix="essai-voix-"))
    lines: list[str] = []
    done = failed = 0

    if not key:
        bot.send_message("⚠️ Essai des voix : la clé GEMINI_API_KEY n'est pas dans les secrets de GitHub (Settings → Secrets "
                         "and variables → Actions). Sans elle, seules les voix Edge existent.")
        wanted = []
        failed = 1

    for i, name in enumerate(wanted):
        if i:
            time.sleep(pause)               # le quota gratuit limite le nombre d'appels par minute
        try:
            wav, voice, model = _gemini_wav(text, {**gcfg, "delai_max_s": 150}, key, voice_override=name)
            mp3 = out / f"{name}.mp3"
            _wav_to_mp3(wav, mp3, _speed_factor(voix_cfg.get("vitesse", "+0%")))
            desc = reglages.GEMINI_VOICES.get(name, ("", ""))[0]
            bot.send_voice(mp3, caption=f"🎙️ {name} — {desc}\nPour la choisir : /voix {name.lower()}")
            lines.append(f"✅ {name}")
            done += 1
        except Exception as exc:
            failed += 1
            reason = str(exc).splitlines()[0][:200] if str(exc) else exc.__class__.__name__
            lines.append(f"❌ {name} : {reason}")
            log.warning("Essai %s : %s", name, reason)
            if isinstance(exc, GeminiTTSError) and exc.fatal:
                break

    # Une voix Edge de référence : on entend la différence.
    try:
        ref = out / "edge.mp3"
        edge_voice = _edge(text, {"edge_voix": "fr-FR-RemyMultilingualNeural", "vitesse": "+0%"}, ref, retries=1)
        bot.send_voice(ref, caption=f"🎙️ Pour comparer : voix Edge classique ({edge_voice}).")
    except Exception as exc:
        log.warning("Voix Edge de comparaison impossible : %s", exc)

    summary = "🎙️ Essai des voix terminé : " + (f"{done} voix Gemini envoyée(s)." if done else "aucune voix Gemini n'a répondu.")
    if failed:
        summary += "\n" + "\n".join(lines[:8])
        summary += "\nSi le message parle de quota : le palier gratuit est limité, réessaie dans une heure ou demain."
    if done:
        summary += "\nChoisis avec /voix <nom> (par exemple /voix sulafat)."
    bot.send_message(summary)
    print(summary)
    return 0 if done else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Outils de voix.")
    parser.add_argument("--essai", action="store_true", help="envoie des échantillons de voix dans Telegram")
    parser.add_argument("--voix", default="", help="noms de voix Gemini à essayer, séparés par des espaces")
    parser.add_argument("--racine", default=str(Path(__file__).resolve().parents[1]))
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if args.essai:
        return _essai(args)
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
