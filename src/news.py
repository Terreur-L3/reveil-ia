"""Récupération des actus depuis des flux RSS (gratuit, sans clé)."""
from __future__ import annotations

import calendar
import html
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace

import feedparser
import requests

log = logging.getLogger(__name__)

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; reveil-ia/1.0)"}
_TAG_RE = re.compile(r"</?[A-Za-z][^>]*>")  # vraies balises seulement (pas « a < b »)
_SPACE_RE = re.compile(r"\s+")


@dataclass
class Item:
    source: str
    categorie: str
    title: str
    summary: str
    published: float | None  # timestamp UTC (None si le flux ne donne pas de date)
    id: int = 0              # numéro attribué une fois la liste finale constituée

    def brief(self, summary_limit: int = 280) -> str:
        line = f"[{self.source}] {self.title}"
        summary = _shorten(self.summary, summary_limit)
        return f"{line} — {summary}" if summary else line


def _shorten(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0].rstrip(",;:") + "…"


def _clean(text: str, limit: int) -> str:
    # certains flux échappent deux fois leur HTML : on décode, on retire les balises, on re-décode
    text = html.unescape(text or "")
    text = html.unescape(_TAG_RE.sub(" ", text))
    text = _SPACE_RE.sub(" ", text).strip()
    return _shorten(text, limit)


def _fetch_feed(name: str, categorie: str, url: str, timeout: int = 20) -> list[Item]:
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
        items.append(Item(name, categorie, title, summary, published))
    return items


def _norm(title: str) -> str:
    return re.sub(r"\W+", " ", title.lower()).strip()


@dataclass
class FeedStatus:
    nom: str
    ok: bool
    recents: int = 0   # articles assez récents
    total: int = 0     # articles dans le flux
    erreur: str = ""

    def line(self) -> str:
        if not self.ok:
            return f"- {self.nom} : EN ÉCHEC ({self.erreur})"
        if self.recents == 0:
            return f"- {self.nom} : aucun article récent (sur {self.total})"
        return f"- {self.nom} : {self.recents} récents (sur {self.total})"


def fetch(cfg: dict) -> tuple[list[Item], list[FeedStatus]]:
    """Retourne (actus récentes numérotées, état de chaque flux).

    Les actus sont mélangées entre sources (une par source à tour de rôle) pour que
    un gros flux ne noie pas les autres.
    """
    max_age = float(cfg.get("age_max_heures", 36)) * 3600
    per_feed = int(cfg.get("max_par_flux", 5))
    total = int(cfg.get("max_total", 60))
    now = time.time()

    feeds = [f for f in cfg.get("flux", []) if f.get("url")]
    if not feeds:
        return [], []

    def job(feed: dict) -> tuple[list[Item] | None, str]:
        name = feed.get("nom", "?")
        try:
            return _fetch_feed(name, feed.get("categorie", "general"), feed["url"]), ""
        except Exception as exc:  # un flux en panne ne doit pas tout bloquer
            reason = exc.__class__.__name__
            if isinstance(exc, requests.HTTPError) and exc.response is not None:
                reason = f"HTTP {exc.response.status_code}"
            log.warning("Flux ignoré (%s) : %s", name, reason)
            return None, reason

    with ThreadPoolExecutor(max_workers=min(8, len(feeds))) as pool:
        results = list(pool.map(job, feeds))  # même ordre que la configuration

    per_source: list[list[Item]] = []
    statuses: list[FeedStatus] = []
    for feed, (items, reason) in zip(feeds, results):
        name = feed.get("nom", "?")
        if items is None:
            statuses.append(FeedStatus(name, False, erreur=reason))
            continue
        fresh = [i for i in items if i.published is None or now - i.published <= max_age]
        fresh.sort(key=lambda i: i.published if i.published is not None else now, reverse=True)
        log.info("%s : %d articles récents sur %d", name, len(fresh), len(items))
        statuses.append(FeedStatus(name, True, len(fresh), len(items)))
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
    return [replace(item, id=n) for n, item in enumerate(merged[:total], start=1)], statuses


def fetch_news(cfg: dict) -> list[Item]:
    return fetch(cfg)[0]
