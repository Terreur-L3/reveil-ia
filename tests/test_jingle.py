"""Tests hors ligne de la sonnerie (jingle) : les sons calculés, puis les fichiers mp3 fabriqués avec ffmpeg.

Lancer :  python -m unittest discover -s tests -t . -v
Les tests mp3 demandent ffmpeg (ils sont sautés sans lui).
"""
from __future__ import annotations

import shutil
import struct
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path
from unittest import mock

from src import audio, jingle
from src.main import audio_duration

needs_ffmpeg = unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg absent")


def read_wav(path) -> tuple[int, int, int, list[int]]:
    with wave.open(str(path), "rb") as fh:
        frames = fh.readframes(fh.getnframes())
        return fh.getnchannels(), fh.getsampwidth(), fh.getframerate(), list(struct.unpack(f"<{fh.getnframes()}h", frames))


def peak(samples: list[int], start_s: float, end_s: float) -> int:
    rate = jingle.SAMPLE_RATE
    return max(abs(v) for v in samples[int(start_s * rate):int(end_s * rate)])


class Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def podcast(self, seconds: float = 3.0) -> Path:
        path = self.tmp / "podcast.mp3"
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"sine=frequency=330:duration={seconds}",
                        "-ac", "1", "-c:a", "libmp3lame", "-b:a", "48k", str(path)], check=True)
        return path

    def leftovers(self) -> list[str]:
        return sorted(p.name for p in self.tmp.iterdir())


# =========================================================================== les sons
class TestSounds(Tmp):
    def test_intro_ring_has_the_asked_length_and_format(self):
        for seconds in (6.0, 10.0, 12.5):
            with self.subTest(seconds):
                wav = self.tmp / "intro.wav"
                self.assertEqual(jingle.make_wav(wav, seconds=seconds), seconds)
                channels, width, rate, samples = read_wav(wav)
                self.assertEqual((channels, width, rate), (1, 2, jingle.SAMPLE_RATE))
                self.assertEqual(len(samples), int(seconds * rate))

    def test_intro_ring_gets_louder_in_three_rounds(self):
        wav = self.tmp / "intro.wav"
        jingle.make_wav(wav, seconds=10)
        _, _, _, samples = read_wav(wav)
        pause = max(0.2, (10 - jingle.ROUND_LEN * 3) / 3)
        starts = [k * (jingle.ROUND_LEN + pause) for k in range(3)]
        peaks = [peak(samples, s, s + jingle.STEP * 4 + 0.5) for s in starts]
        self.assertLess(peaks[0], peaks[1])
        self.assertLess(peaks[1], peaks[2])
        self.assertGreater(peaks[0], 3000)                                  # audible dès la première fois
        self.assertLessEqual(max(abs(v) for v in samples), 32000)           # jamais d'écrêtage

    def test_alarm_ring_rises_from_gentle_to_full(self):
        wav = self.tmp / "alarm.wav"
        jingle.make_alarm_wav(wav, seconds=45)
        _, _, _, samples = read_wav(wav)
        self.assertEqual(len(samples), 45 * jingle.SAMPLE_RATE)
        period = jingle.ROUND_LEN + 1.0
        count = int((45 - jingle.ROUND_LEN) // period) + 1
        first = peak(samples, 0, jingle.STEP * 4 + 0.5)
        last_start = (count - 1) * period
        last = peak(samples, last_start, last_start + jingle.STEP * 4 + 0.5)
        self.assertGreater(last, first * 1.8)                               # de 40 % à 100 % du volume
        self.assertGreater(count, 3)
        self.assertGreater(peak(samples, 44, 45), 0)                        # elle sonne jusqu'au bout

    def test_a_very_short_alarm_still_rings_once(self):
        wav = self.tmp / "short.wav"
        jingle.make_alarm_wav(wav, seconds=5)
        _, _, _, samples = read_wav(wav)
        self.assertEqual(len(samples), 5 * jingle.SAMPLE_RATE)
        self.assertGreater(peak(samples, 0, 2), 3000)


# =========================================================================== les fichiers mp3
@needs_ffmpeg
class TestFiles(Tmp):
    def test_fallback_ring(self):
        out = self.tmp / "sonnerie.mp3"
        self.assertEqual(jingle.build_alarm(out, seconds=20), out)
        self.assertAlmostEqual(audio_duration(out), 20, delta=1.5)
        self.assertEqual(self.leftovers(), ["sonnerie.mp3"])                 # ni .wav ni .part.mp3

    def test_reveil_is_the_ring_then_the_whole_podcast(self):
        podcast = self.podcast(4)
        out = self.tmp / "reveil.mp3"
        jingle.build(podcast, out, seconds=10)
        self.assertAlmostEqual(audio_duration(out), 14, delta=1.5)
        self.assertEqual(self.leftovers(), ["podcast.mp3", "reveil.mp3"])
        self.assertAlmostEqual(audio_duration(podcast), 4, delta=1)         # le podcast d'origine n'est pas touché

    def test_a_longer_podcast_is_never_cut(self):
        out = self.tmp / "reveil.mp3"
        jingle.build(self.podcast(30), out, seconds=8)
        self.assertAlmostEqual(audio_duration(out), 38, delta=2)

    def test_a_broken_podcast_raises_and_leaves_the_old_file_alone(self):
        bad = self.tmp / "bad.mp3"
        bad.write_bytes(b"ceci n'est pas un mp3" * 50)
        out = self.tmp / "reveil.mp3"
        out.write_text("ancien", encoding="utf-8")
        with self.assertRaises(jingle.JingleError) as ctx:
            jingle.build(bad, out)
        self.assertIn("ffmpeg a échoué", str(ctx.exception))
        self.assertEqual(out.read_text(encoding="utf-8"), "ancien")
        self.assertEqual(self.leftovers(), ["bad.mp3", "reveil.mp3"])        # les fichiers temporaires sont nettoyés

    def test_a_missing_podcast_raises(self):
        with self.assertRaises(jingle.JingleError):
            jingle.build(self.tmp / "absent.mp3", self.tmp / "reveil.mp3")
        self.assertEqual(self.leftovers(), [])

    def test_a_stuck_ffmpeg_is_abandoned(self):
        with mock.patch.object(jingle.subprocess, "run", side_effect=subprocess.TimeoutExpired("ffmpeg", 300)):
            with self.assertRaises(jingle.JingleError) as ctx:
                jingle.build_alarm(self.tmp / "sonnerie.mp3", seconds=10)
        self.assertIn("trop de temps", str(ctx.exception))
        self.assertEqual(self.leftovers(), [])

    def test_an_empty_result_is_an_error(self):
        def silent_ffmpeg(args, **kw):
            Path(args[-1]).write_bytes(b"")             # ffmpeg « réussit » mais ne produit rien
            return subprocess.CompletedProcess(args, 0)

        with mock.patch.object(jingle.subprocess, "run", side_effect=silent_ffmpeg):
            with self.assertRaises(jingle.JingleError) as ctx:
                jingle.build_alarm(self.tmp / "sonnerie.mp3", seconds=10)
        self.assertIn("fichier vide", str(ctx.exception))
        self.assertEqual(self.leftovers(), [])


class TestWithoutFfmpeg(Tmp):
    def test_both_builders_say_so_clearly(self):
        with mock.patch.object(audio, "ensure_ffmpeg", return_value=None), mock.patch.object(jingle.shutil, "which", return_value=None):
            with self.assertRaises(jingle.JingleError) as ctx:
                jingle.build_alarm(self.tmp / "sonnerie.mp3")
            self.assertIn("ffmpeg est absent", str(ctx.exception))
            with self.assertRaises(jingle.JingleError):
                jingle.build(self.tmp / "podcast.mp3", self.tmp / "reveil.mp3")
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
