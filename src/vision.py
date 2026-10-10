"""Lecture d'images : emploi du temps, documents, captures d'écran…

Confidentialité : les images ne partent QUE vers Groq (chaîne « vision » de config.yaml), jamais vers Gemini,
et elles ne sont jamais gardées : seul le texte lu est rangé dans la mémoire.
"""
from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import audio, llm
from .fr import date_fr
from .jsonutil import load_json
from .pacer import estimate_tokens

log = logging.getLogger(__name__)

MAX_BYTES = 9 * 1024 * 1024      # Groq accepte 20 Mo en base64 (+33 %) : on garde de la marge
IMAGE_TOKENS = 2048              # chaque image compte pour 2 048 « tokens » chez Groq
OUTPUT_TOKENS = 3500

SYSTEM = """Tu lis <<support>> envoyé(e) par <<prenom>> (emploi du temps d'école, planning, document, capture d'écran ou photo). Tu n'inventes RIEN : uniquement ce qui est lisible.

Réponds par un objet JSON avec exactement ces clés :
- "type" : "emploi_du_temps" si <<support>> montre un planning de cours ou d'horaires de la semaine (grille ou liste de cours avec jours et horaires) ; sinon "autre" ;
- "description" : une phrase qui dit ce que c'est ;
- "texte" : si type = "autre", le texte important (dates, noms, horaires, consignes) recopié fidèlement, 1500 caractères maximum ; sinon "" ;
- "semaine_du" : si des dates précises sont affichées (« semaine du 05/10 », dates en en-tête de colonne), la date du LUNDI de cette semaine au format AAAA-MM-JJ (l'année se déduit de la date du jour s'il n'y en a pas) ; sinon null ;
- "jours_vus" : les jours visibles, même sans cours, parmi lun, mar, mer, jeu, ven, sam, dim ;
- "cours" : une liste d'objets {"jour", "debut", "fin", "intitule", "salle", "enseignant"}, UN par cours ou créneau. "jour" parmi lun…dim ; "debut" et "fin" au format HH:MM sur 24 h ; "salle" et "enseignant" valent null s'ils ne sont pas écrits. Si une légende précise une classe, un groupe ou une option, ne garde que ce qui la concerne.
Si c'est un emploi du temps mais qu'il est illisible ou coupé, mets type = "emploi_du_temps", cours = [] et dis pourquoi dans "description". Aucun commentaire en dehors du JSON."""


class ImageError(RuntimeError):
    pass


@dataclass
class Reading:
    type: str = "autre"                  # "emploi_du_temps" ou "autre"
    description: str = ""
    texte: str = ""
    semaine_du: str = ""
    jours_vus: list[str] = field(default_factory=list)
    cours: list[dict] = field(default_factory=list)


# --------------------------------------------------------------------------- préparer l'image
def sniff_mime(head: bytes) -> str | None:
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return None


def _ffmpeg_to_jpeg(path: Path, out: Path, max_side: int) -> bool:
    ffmpeg = audio.ensure_ffmpeg()      # sur GitHub Actions, l'installe s'il manque ; ailleurs, celui qui est là
    if not ffmpeg:
        return False
    scale = f"scale='min({max_side},iw)':'min({max_side},ih)':force_original_aspect_ratio=decrease"
    try:
        subprocess.run(
            [ffmpeg, "-nostdin", "-loglevel", "error", "-y", "-i", str(path), "-frames:v", "1", "-vf", scale,
             "-q:v", "3", str(out)], check=True, capture_output=True, timeout=120,
        )
    except Exception:
        return False
    return out.exists() and out.stat().st_size > 0


def prepare_image(path: Path | str, *, max_bytes: int = MAX_BYTES) -> tuple[str, bytes]:
    """(type MIME, octets) prêts à envoyer. Convertit ou réduit avec ffmpeg si le format ou le poids l'exige."""
    path = Path(path)
    data = path.read_bytes()
    mime = sniff_mime(data[:16])
    if mime and len(data) <= max_bytes:
        return mime, data
    out = path.with_name(path.stem + "-prep.jpg")
    try:
        for side in (2600, 1800, 1200):
            if _ffmpeg_to_jpeg(path, out, side) and out.stat().st_size <= max_bytes:
                return "image/jpeg", out.read_bytes()
    finally:
        out.unlink(missing_ok=True)      # seuls les octets envoyés comptent : pas de copie qui traîne
    if mime is None:
        raise ImageError("format d'image non pris en charge (envoie plutôt un JPEG ou un PNG)")
    raise ImageError("image trop lourde (ffmpeg n'a pas pu la réduire)")


# --------------------------------------------------------------------------- lire la réponse
def _clip(value, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def parse(reply: str) -> Reading:
    data = load_json(reply)
    if not isinstance(data, dict):
        raise ValueError("réponse illisible")
    kind = "emploi_du_temps" if "emploi" in str(data.get("type", "")).lower() else "autre"
    cours = data.get("cours")
    jours = data.get("jours_vus")
    texte = str(data.get("texte") or "").strip()
    return Reading(
        type=kind,
        description=_clip(data.get("description"), 300),
        texte=texte[:2500],
        semaine_du=_clip(data.get("semaine_du"), 40) if data.get("semaine_du") else "",
        jours_vus=[str(j) for j in jours][:7] if isinstance(jours, list) else [],
        cours=[c for c in cours if isinstance(c, dict)][:120] if isinstance(cours, list) else [],
    )


# --------------------------------------------------------------------------- appeler le modèle
def _ask(prompt: str, system: str, chain: list[dict], keys: dict, pacer, images) -> Reading:
    last: Exception | None = None
    for attempt in range(2):  # une seconde chance si la réponse est illisible
        if pacer is not None:
            pacer.wait(estimate_tokens(system, prompt, output=OUTPUT_TOKENS) + IMAGE_TOKENS * len(images or []))
        reply, model = llm.generate(
            prompt, system=system, chain=chain, keys=keys, temperature=0.1, json_mode=True, timeout=150,
            images=images or None,
        )
        try:
            reading = parse(reply)
            log.info("Lecture faite avec %s : %s, %d cours", model, reading.type, len(reading.cours))
            return reading
        except ValueError as exc:
            last = exc
            log.warning("Lecture : réponse illisible (essai %d/2)", attempt + 1)
    raise llm.LLMError(f"réponse illisible ({last})")


def read_images(images: list[tuple[str, bytes]], *, caption: str, now: datetime, cfg: dict, keys: dict,
                prenom: str = "", pacer=None) -> Reading:
    chain = (cfg.get("llm") or {}).get("vision") or []
    system = SYSTEM.replace("<<support>>", "l'image").replace("<<prenom>>", prenom or "la personne")
    prompt = f"Date du jour : {date_fr(now.date())}."
    if caption.strip():
        prompt += f"\nLégende écrite par la personne : « {caption.strip()[:300]} »"
    return _ask(prompt, system, chain, keys, pacer, images)


def read_text(text: str, *, now: datetime, cfg: dict, keys: dict, prenom: str = "", pacer=None) -> Reading:
    """Un emploi du temps écrit à la main dans le chat (« lundi 9h-12h étalonnage salle 204… »)."""
    chain = (cfg.get("llm") or {}).get("prive") or []
    system = SYSTEM.replace("<<support>>", "le texte").replace("<<prenom>>", prenom or "la personne")
    prompt = f"Date du jour : {date_fr(now.date())}.\n\nTexte :\n\"\"\"\n{text.strip()[:3000]}\n\"\"\""
    return _ask(prompt, system, chain, keys, pacer, None)
