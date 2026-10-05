"""Régulateur de débit : respecte le quota gratuit de « tokens par minute » d'un fournisseur."""
from __future__ import annotations

import time
from collections import deque


def estimate_tokens(*texts: str, output: int = 1200) -> int:
    """Estimation prudente (le français coûte environ 1 token pour 3 caractères) + réponse attendue."""
    return sum(len(t) for t in texts) // 3 + output


class Pacer:
    def __init__(self, per_minute: int, *, clock=None, sleep=None):
        self.per_minute = max(1, int(per_minute))
        self._clock = clock or time.monotonic
        self._sleep = sleep
        self._log: deque[tuple[float, int]] = deque()

    def wait(self, tokens: int) -> float:
        """Attend si besoin, puis note la consommation. Retourne le temps attendu (secondes)."""
        slept = 0.0
        for _ in range(3):  # jamais de boucle infinie, même si l'horloge est truquée
            now = self._clock()
            while self._log and now - self._log[0][0] >= 60:
                self._log.popleft()
            used = sum(t for _, t in self._log)
            if not self._log or used + tokens <= self.per_minute:
                break
            pause = min(62.0, self._log[0][0] + 60.5 - now)
            (self._sleep or time.sleep)(max(pause, 0.5))
            slept += max(pause, 0.5)
        self._log.append((self._clock(), tokens))
        return slept
