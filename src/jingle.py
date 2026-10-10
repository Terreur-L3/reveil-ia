"""La sonnerie de réveil : petites cloches qui montent doucement.

Deux fichiers sont produits :
  - reveil.mp3   = ~10 s de sonnerie collées devant le podcast. C'est lui que le téléphone joue à l'heure du réveil
                   (le podcast seul, latest.mp3, reste celui qu'on t'envoie dans Telegram) ;
  - sonnerie.mp3 = ~45 s de sonnerie seule, de plus en plus forte : ce que la sonnette fait jouer si le podcast
                   du jour n'est pas prêt. Le réveil sonne ainsi à l'heure quoi qu'il arrive.
Aucune dépendance : la sonnerie est calculée ici, ffmpeg ne sert qu'à coller et à encoder.
"""
from __future__ import annotations

import math
import shutil
import struct
import subprocess
import wave
from pathlib import Path

from . import audio

SAMPLE_RATE = 24000
# do mi sol do : un petit arpège joyeux, répété trois fois en montant le volume
NOTES = [523.25, 659.25, 783.99, 1046.50]
ROUNDS = [0.35, 0.6, 0.9]
STEP = 0.42                                  # écart entre deux notes
ROUND_LEN = STEP * len(NOTES) + 1.5          # un arpège + sa résonance


class JingleError(RuntimeError):
    pass


def _bell(freq: float, seconds: float, volume: float) -> list[float]:
    count = int(seconds * SAMPLE_RATE)
    out = []
    for i in range(count):
        t = i / SAMPLE_RATE
        attack = min(1.0, t / 0.008)
        decay = math.exp(-3.2 * t / seconds)
        wave_sum = (math.sin(2 * math.pi * freq * t)
                    + 0.45 * math.sin(2 * math.pi * freq * 2 * t)
                    + 0.2 * math.sin(2 * math.pi * freq * 3.01 * t))
        out.append(volume * attack * decay * wave_sum / 1.65)
    return out


def _write(path: Path | str, seconds: float, schedule: list[tuple[float, float]]) -> float:
    """Mélange un arpège à chaque (début en secondes, volume) de `schedule` et l'écrit en WAV mono 16 bits."""
    total = int(seconds * SAMPLE_RATE)
    samples = [0.0] * total
    bells = [_bell(freq, 1.5, 1.0) for freq in NOTES]          # calculées une fois, puis mises au volume voulu
    for start, volume in schedule:
        for k, bell in enumerate(bells):
            begin = int((start + k * STEP) * SAMPLE_RATE)
            for i, v in enumerate(bell):
                if begin + i >= total:
                    break
                samples[begin + i] += v * volume
    peak = max(1.0, max(abs(v) for v in samples))
    with wave.open(str(path), "wb") as fh:
        fh.setnchannels(1)
        fh.setsampwidth(2)
        fh.setframerate(SAMPLE_RATE)
        fh.writeframes(b"".join(struct.pack("<h", int(32000 * v / peak)) for v in samples))
    return seconds


def make_wav(path: Path | str, *, seconds: float = 10.0) -> float:
    """Écrit la sonnerie d'introduction (WAV mono 16 bits) : trois arpèges, de plus en plus forts. Retourne sa durée."""
    pause = max(0.2, (seconds - ROUND_LEN * len(ROUNDS)) / len(ROUNDS))
    schedule = [(k * (ROUND_LEN + pause), volume) for k, volume in enumerate(ROUNDS)]
    return _write(path, seconds, schedule)


def make_alarm_wav(path: Path | str, *, seconds: float = 45.0) -> float:
    """Sonnerie « réveil » seule : des arpèges qui s'enchaînent en montant de 40 % à 100 % du volume."""
    period = ROUND_LEN + 1.0
    count = max(1, int((seconds - ROUND_LEN) // period) + 1)
    schedule = [(k * period, 0.4 + 0.6 * (k / (count - 1) if count > 1 else 1.0)) for k in range(count)]
    return _write(path, seconds, schedule)


def _ffmpeg() -> str:
    ffmpeg = audio.ensure_ffmpeg() or shutil.which("ffmpeg")
    if not ffmpeg:
        raise JingleError("ffmpeg est absent")
    return ffmpeg


def _encode(args: list[str], part: Path, out_mp3: Path) -> None:
    try:
        subprocess.run(args, check=True, capture_output=True, timeout=300)
        if not part.exists() or part.stat().st_size < 5000:
            raise JingleError("fichier vide")
        part.replace(out_mp3)
    except subprocess.CalledProcessError as exc:
        tail = (exc.stderr or b"").decode("utf-8", "replace").strip().splitlines()[-1:]
        raise JingleError("ffmpeg a échoué : " + " ".join(tail)) from exc
    except subprocess.TimeoutExpired as exc:
        raise JingleError("ffmpeg a pris trop de temps") from exc


def build_alarm(out_mp3: Path | str, *, seconds: float = 45.0) -> Path:
    """sonnerie.mp3 = la sonnerie seule (~45 s), jouée à l'heure du réveil quand le podcast du jour n'est pas prêt."""
    out_mp3 = Path(out_mp3)
    ffmpeg = _ffmpeg()
    wav = out_mp3.with_name(out_mp3.stem + ".wav")
    part = out_mp3.with_name(out_mp3.name + ".part.mp3")
    try:
        make_alarm_wav(wav, seconds=seconds)
        _encode([ffmpeg, "-nostdin", "-loglevel", "error", "-y", "-i", str(wav), "-ac", "1", "-ar", str(SAMPLE_RATE),
                 "-c:a", "libmp3lame", "-b:a", "64k", str(part)], part, out_mp3)
    finally:
        wav.unlink(missing_ok=True)
        part.unlink(missing_ok=True)
    return out_mp3


def build(podcast_mp3: Path | str, out_mp3: Path | str, *, seconds: float = 10.0) -> Path:
    """reveil.mp3 = sonnerie + podcast. Lève JingleError si ffmpeg manque ou échoue (le podcast seul reste valable)."""
    podcast_mp3, out_mp3 = Path(podcast_mp3), Path(out_mp3)
    ffmpeg = _ffmpeg()
    wav = out_mp3.with_name(out_mp3.stem + ".intro.wav")
    part = out_mp3.with_name(out_mp3.name + ".part.mp3")
    try:
        make_wav(wav, seconds=seconds)
        graph = (f"[0:a]aresample={SAMPLE_RATE},aformat=channel_layouts=mono[a0];"
                 f"[1:a]aresample={SAMPLE_RATE},aformat=channel_layouts=mono[a1];"
                 "[a0][a1]concat=n=2:v=0:a=1[a]")
        _encode([ffmpeg, "-nostdin", "-loglevel", "error", "-y", "-i", str(wav), "-i", str(podcast_mp3),
                 "-filter_complex", graph, "-map", "[a]", "-c:a", "libmp3lame", "-b:a", "64k", str(part)], part, out_mp3)
    finally:
        wav.unlink(missing_ok=True)
        part.unlink(missing_ok=True)
    return out_mp3
