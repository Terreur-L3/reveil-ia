"""Récupération des actus depuis des flux RSS (gratuit, sans clé)."""
from __future__ import annotations

import calendar
import html
import logging
import re
import time
from dataclasses import dataclass

import feedparser
import requests

log = logging.getLogger(__name__)

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; reveil-ia/1.0)"}
_TAG_RE = re.compile(r"</?[A-Za-z][^>]*>")  # vraies balises seulement (pas « a < b »)
_SPACE_RE = re.compile(r"\s+")


@dataclass
class Item:
    source: str
    title: str
    summary: str
    published: float | None  # timestamp UTC (None si le flux ne donne pas de date)

    def as_line(self) -> str:
        if self.summary:
            return f"[{self.source}] {self.title} — {self.summary}"
        return f"[{self.source}] {self.title}"


def _clean(text: str, limit: int) -> str:
    # certains flux échappent deux fois leur HTML : on décode, on retire les balises, on re-décode
    text = html.unescape(text or "")
    text = html.unescape(_TAG_RE.sub(" ", text))
    text = _SPACE_RE.sub(" ", text).strip()
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0].rstrip(",;:") + "…"
    return text


def _fetch_feed(name: str, url: str, timeout: int = 20) -> list[Item]:
    resp = requests.get(url, headers=HEADERS, timeout=timeout)
    resp.raise_for_status()
    parsed = feedparser.parse(resp.content)
    items: list[Item] = []
    for entry in parsed.entries:
        title = _clean(entry.get("title", ""), 200)
        if not title:
            continue
        stamp = entry.get("published_parsed") or entry.get("updated_parsed")
        published = float(calendar.timegm(stamp)) if stamp else None
        summary = _clean(entry.get("summary", "") or entry.get("description", ""), 280)
        if summary.lower().startswith(title.lower()[:40]):
            summary = ""  # résumé qui répète simplement le titre
        items.append(Item(name, title, summary, published))
    return items


def _norm(title: str) -> str:
    return re.sub(r"\W+", " ", title.lower()).strip()


def fetch_news(cfg: dict) -> list[Item]:
    """Retourne les actus récentes, mélangées entre les sources (une par source à tour de rôle)."""
    max_age = float(cfg.get("age_max_heures", 36)) * 3600
    per_feed = int(cfg.get("max_par_flux", 6))
    total = int(cfg.get("max_total", 24))
    now = time.time()

    per_source: list[list[Item]] = []
    for feed in cfg.get("flux", []):
        name, url = feed.get("nom", "?"), feed.get("url", "")
        try:
            items = _fetch_feed(name, url)
        except Exception as exc:  # un flux en panne ne doit pas tout bloquer
            log.warning("Flux ignoré (%s) : %s", name, exc)
            continue
        fresh = [i for i in items if i.published is None or now - i.published <= max_age]
        fresh.sort(key=lambda i: i.published if i.published is not None else now, reverse=True)
        log.info("%s : %d articles récents sur %d", name, len(fresh), len(items))
        per_source.append(fresh[:per_feed])

    merged: list[Item] = []
    seen: set[str] = set()
    for rank in range(per_feed):
        for lst in per_source:
            if rank < len(lst):
                key = _norm(lst[rank].title)
                if key not in seen:
                    seen.add(key)
                    merged.append(lst[rank])
    return merged[:total]
