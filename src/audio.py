"""Découpe des vocaux avec ffmpeg (gratuit).

Les longs enregistrements sont coupés en morceaux de quelques minutes : chacun est transcrit à son
rythme (quotas gratuits), et si ça s'arrête en route, la suite reprend là où on en était.
Sans ffmpeg, seuls les petits fichiers dans un format connu passent tels quels.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

DIRECT_EXT = {".mp3", ".m4a", ".mp4", ".ogg", ".oga", ".opus", ".wav", ".flac", ".webm", ".mpga", ".mpeg"}
DIRECT_MAX = 24 * 1024 * 1024  # Groq refuse au-delà de 25 Mo


class AudioError(RuntimeError):
    pass


@dataclass
class Chunk:
    path: Path
    duration: float  # secondes (0 si inconnue)


def find_ffmpeg() -> str | None:
    return shutil.which("ffmpeg")


def ensure_ffmpeg() -> str | None:
    """ffmpeg s'il est là ; sinon, sur GitHub Actions seulement, on l'installe (une fois par exécution)."""
    found = find_ffmpeg()
    if found:
        return found
    if os.environ.get("GITHUB_ACTIONS") and shutil.which("sudo") and shutil.which("apt-get"):
        log.info("Installation de ffmpeg…")
        try:
            subprocess.run(["sudo", "-n", "apt-get", "update", "-qq"], check=True, timeout=180, capture_output=True)
            subprocess.run(
                ["sudo", "-n", "apt-get", "install", "-y", "-qq", "--no-install-recommends", "ffmpeg"],
                check=True, timeout=300, capture_output=True,
            )
        except Exception as exc:
            log.warning("Installation de ffmpeg impossible : %s", exc.__class__.__name__)
        return find_ffmpeg()
    return None


def probe_duration(path: Path) -> float | None:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
            check=True, capture_output=True, text=True, timeout=60,
        ).stdout.strip()
        return float(out)
    except Exception:
        return None


def prepare(path: Path, workdir: Path, segment_s: int, hint_duration: float = 0.0) -> list[Chunk]:
    """Retourne les morceaux à transcrire, dans l'ordre (mono 16 kHz, opus léger)."""
    path = Path(path)
    ffmpeg = ensure_ffmpeg()
    if ffmpeg is None:
        if path.suffix.lower() in DIRECT_EXT and path.stat().st_size <= DIRECT_MAX:
            return [Chunk(path, float(hint_duration or 0))]
        raise AudioError("ffmpeg est nécessaire pour ce fichier (format ou taille)")
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    pattern = workdir / "morceau-%04d.ogg"
    for old in workdir.glob("morceau-*.ogg"):
        old.unlink()
    cmd = [
        ffmpeg, "-nostdin", "-loglevel", "error", "-y", "-i", str(path), "-vn",
        "-ac", "1", "-ar", "16000", "-c:a", "libopus", "-b:a", "24k",
        "-f", "segment", "-segment_time", str(int(segment_s)), "-reset_timestamps", "1", str(pattern),
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=900)
    except subprocess.CalledProcessError as exc:
        tail = (exc.stderr or b"").decode("utf-8", "replace").strip().splitlines()[-1:]
        raise AudioError("ffmpeg n'a pas pu lire ce fichier : " + " ".join(tail)) from exc
    except subprocess.TimeoutExpired as exc:
        raise AudioError("ffmpeg a pris trop de temps") from exc
    chunks = sorted(workdir.glob("morceau-*.ogg"))
    if not chunks:
        raise AudioError("aucun son exploitable dans ce fichier")
    return [Chunk(p, probe_duration(p) or float(segment_s)) for p in chunks]
