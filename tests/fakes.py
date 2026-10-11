"""Faux internet local pour les tests : flux RSS, Gemini, Groq (texte et transcription), Telegram.

Aucun accès réseau réel : un petit serveur HTTP écoute sur 127.0.0.1 et les modules sont
branchés dessus en remplaçant leurs adresses.
"""
from __future__ import annotations

import base64
import email
import email.policy
import json
import os
import re
import threading
import time
from email.utils import formatdate
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

os.environ["NO_PROXY"] = os.environ["no_proxy"] = "127.0.0.1,localhost"

FRENCH_MONTHS = {
    "janvier": 1, "février": 2, "fevrier": 2, "mars": 3, "avril": 4, "mai": 5, "juin": 6, "juillet": 7,
    "août": 8, "aout": 8, "septembre": 9, "octobre": 10, "novembre": 11, "décembre": 12, "decembre": 12,
}


class Req:
    def __init__(self, method: str, path: str, headers: dict, body: bytes):
        self.method, self.path, self.headers, self.body = method, path, headers, body

    @property
    def json(self):
        try:
            return json.loads(self.body.decode("utf-8"))
        except Exception:
            return None

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "replace")

    @property
    def plain(self) -> str:
        """Le contenu lisible : un JSON est décodé (accents inclus) pour pouvoir y chercher un mot."""
        data = self.json
        return json.dumps(data, ensure_ascii=False) if data is not None else self.text

    def multipart(self) -> dict:
        """Champs d'un envoi multipart : {nom: texte} et {nom: (nom_fichier, octets)}."""
        ctype = self.headers.get("Content-Type") or self.headers.get("content-type") or ""
        msg = email.message_from_bytes(
            b"Content-Type: " + ctype.encode() + b"\r\n\r\n" + self.body, policy=email.policy.HTTP
        )
        fields: dict = {}
        for part in msg.iter_parts():
            name = part.get_param("name", header="content-disposition")
            filename = part.get_filename()
            payload = part.get_payload(decode=True) or b""
            fields[name] = (filename, payload) if filename else payload.decode("utf-8", "replace")
        return fields


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # silence
        pass

    def _serve(self, method: str):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        req = Req(method, self.path, dict(self.headers), body)
        world: World = self.server.world  # type: ignore[attr-defined]
        with world.lock:
            world.requests.append(req)
            try:
                status, headers, payload = world.dispatch(req)
            except Exception as exc:  # une erreur du faux serveur doit se voir dans le test
                status, headers, payload = 500, {}, f"fake server error: {exc!r}".encode()
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        self._serve("GET")

    def do_POST(self):
        self._serve("POST")


def rss_xml(name: str, items: list[dict]) -> str:
    out = [f'<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>{escape(name)}</title>']
    for it in items:
        stamp = time.time() - float(it.get("age_h", 1)) * 3600
        out.append(
            f"<item><title>{escape(it['title'])}</title>"
            f"<description>{escape(it.get('summary', ''))}</description>"
            f"<pubDate>{formatdate(stamp)}</pubDate></item>"
        )
    out.append("</channel></rss>")
    return "".join(out)


def fake_jpeg(tag: str = "") -> bytes:
    """Des octets qui commencent comme un JPEG (c'est tout ce que le programme vérifie avant d'envoyer)."""
    return b"\xff\xd8\xff\xe0" + f"IMG[{tag}]".encode() + b"\0" * 120


def fake_png(tag: str = "") -> bytes:
    return b"\x89PNG\r\n\x1a\n" + f"IMG[{tag}]".encode() + b"\0" * 120


def timetable_reading(week: str | None = None, days=("lun", "mar", "mer", "jeu", "ven")) -> dict:
    """Ce que le « modèle de vision » lit sur un emploi du temps d'école."""
    return {
        "type": "emploi_du_temps", "description": "Emploi du temps de la semaine", "texte": "",
        "semaine_du": week, "jours_vus": list(days),
        "cours": [
            {"jour": "lun", "debut": "09:00", "fin": "12:30", "intitule": "Étalonnage", "salle": "204", "enseignant": "M. Durand"},
            {"jour": "lun", "debut": "14:00", "fin": "17:00", "intitule": "Prise de vue", "salle": "Studio B", "enseignant": None},
            {"jour": "mar", "debut": "10:00", "fin": "12:00", "intitule": "Histoire du cinéma", "salle": None, "enseignant": None},
            {"jour": "jeu", "debut": "09:30", "fin": "13:00", "intitule": "Montage", "salle": "Salle 12", "enseignant": None},
        ],
    }


def gemini_reply(text: str) -> dict:
    return {"candidates": [{"content": {"parts": [{"text": text}]}}]}


def groq_reply(text: str) -> dict:
    return {"choices": [{"message": {"content": text}}]}


# --------------------------------------------------------------------------- réponses « intelligentes » par défaut
_ARTICLE_RE = re.compile(r"^(\d+) \| \[(.*?)\] (.*?)(?: — .*)?$", re.MULTILINE)
_TOPIC_RE = re.compile(r"^\d+\. \[[^\]]*\] \[(.*?)\] (.*?)(?: — .*)?$", re.MULTILINE)
_MONTHS = "|".join(FRENCH_MONTHS)


def default_score(article_id: int, source: str, title: str) -> dict:
    """Notation par mots-clés du titre : suffit pour tester le tri sans vraie IA."""
    t = title.lower()
    sc = {"id": article_id, "importance": 4, "interet": 3, "ton": "neutre", "groupe": article_id, "ignorer": False}
    if "houdini" in t or "caméra" in t or "camera" in t:
        sc.update(interet=9, ton="positif")
    if "montage" in t:
        sc.update(interet=9, ton="positif")
    if "minecraft" in t:
        sc.update(interet=8, ton="positif")
    if "greve" in t or "grève" in t:
        sc.update(importance=8)
    if "attentat" in t or "guerre" in t or "séisme" in t:
        sc.update(importance=9, ton="difficile")
    if "bonne nouvelle" in t or "découverte" in t:
        sc.update(importance=7, ton="positif")
    if "promo" in t or "bon plan" in t:
        sc.update(ignorer=True)
    return sc


QUESTIONS = [
    "Où en es-tu avec ton projet principal en ce moment, et qu'est-ce qui te bloque ?",
    "Raconte-moi ta dernière vraie bonne surprise de la semaine, en détail.",
    "Qu'est-ce que tu voudrais apprendre cette année que tu n'as encore jamais osé commencer ?",
    "Décris-moi l'endroit où tu te sens le mieux pour créer.",
    "Quel conseil donnerais-tu à toi-même d'il y a deux ans ?",
    "Quelle personne t'a le plus inspiré récemment, et pourquoi ?",
]


def default_extract(prompt: str) -> dict:
    """Extraction par mots-clés : assez pour tester toute la chaîne sans vraie IA."""
    date_line = re.search(r"Date du message : (\d{4})-(\d{2})-(\d{2})", prompt)
    year = int(date_line.group(1)) if date_line else 2026
    body = prompt.split('"""')[1] if '"""' in prompt else prompt
    out = {"resume": body.strip()[:80], "faits": [], "anniversaires": [], "rappels": [], "aime": [], "evite": []}
    for sentence in re.split(r"(?<=[.!?])\s+", body.strip()):
        if len(sentence.split()) >= 5 and len(out["faits"]) < 3:
            out["faits"].append({"categorie": "projet", "texte": sentence.strip()[:120]})
    m = re.search(rf"anniversaire de (\w+) le (\d{{1,2}}) ({_MONTHS})(?: (\d{{4}}))?", body, re.IGNORECASE)
    if m:
        item = {"nom": m.group(1), "jour": int(m.group(2)), "mois": FRENCH_MONTHS[m.group(3).lower()]}
        item["annee"] = int(m.group(4)) if m.group(4) else None
        out["anniversaires"].append(item)
    m = re.search(rf"rendre (.+?) le (\d{{1,2}}) ({_MONTHS})", body, re.IGNORECASE)
    if m:
        out["rappels"].append({
            "texte": f"rendre {m.group(1)}", "date": f"{year}-{FRENCH_MONTHS[m.group(3).lower()]:02d}-{int(m.group(2)):02d}",
        })
    loved = [("Houdini", "Houdini"), ("montage", "montage vidéo"), ("Minecraft", "Minecraft"), ("pêche", "pêche"),
             ("photographie argentique", "photographie argentique")]
    for needle, label in loved:
        if needle.lower() in body.lower():
            strong = "adore" in body.lower() or "passion" in body.lower()
            out["aime"].append({"sujet": label, "intensite": 3 if strong else 1, "sensible": False})
    if "dépression" in body.lower():
        out["aime"].append({"sujet": "dépression", "intensite": 3, "sensible": True})
    if "marre du football" in body.lower():
        out["evite"].append({"sujet": "football", "sensible": False})
    return out


class World:
    def __init__(self):
        self.requests: list[Req] = []
        self.lock = threading.RLock()
        self.feeds: dict[str, list[dict] | int] = {}   # nom → articles, ou code HTTP d'erreur
        self.score_fn = default_score
        self.gemini_fn = self.smart_gemini
        self.groq_fn = self.smart_groq
        self.extract_fn = default_extract
        self.question_n = 0
        self.tts_fn = self.smart_tts                   # Gemini TTS : (requête, « interactions » | « legacy », corps) → (statut, réponse)
        # Groq transcription
        self.transcripts: list[str] = []               # textes à renvoyer, dans l'ordre des appels
        self.stt_fn = self.smart_stt
        self.stt_calls = 0
        # Telegram
        self.tg_token = "123456:TEST-TOKEN"
        self.tg_chat = 4242
        self.tg_updates: list[dict] = []
        self.tg_files: dict[str, bytes] = {}
        self.tg_sent: list[dict] = []
        self.tg_fail: dict[str, int] = {}
        self.tg_conflict = False
        # Groq vision : ce que le « modèle » lit sur les images (dict, texte JSON, ou fonction (texte, images) → dict)
        self.vision_reading = None
        self.vision_fn = self.smart_vision
        self.vision_calls: list[dict] = []               # {"model", "text", "images": [(mime, octets)]}
        # GitHub : lancements de workflows
        self.gh_dispatches: list[dict] = []
        self.gh_fail = 0                                  # code HTTP à renvoyer (0 = tout va bien)
        self.gh_ok_status = 204                           # réussite : 204 sans corps, ou 200 avec le détail du lancement
        self._tg_message_id = 1000
        self._tg_update_id = 500
        self.server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None

    # ----- Gemini TTS : une seconde de « voix » (un la) par appel, dans la forme de réponse de l'API
    @staticmethod
    def wav_bytes(seconds: float = 1.0, raw: bool = False) -> bytes:
        import io, math, struct, wave

        rate = 24000
        frames = b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * i / rate))) for i in range(int(rate * seconds)))
        if raw:
            return frames
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(frames)
        return buf.getvalue()

    def smart_tts(self, req, kind, body):
        import base64

        if kind == "interactions":
            data = base64.b64encode(self.wav_bytes()).decode()
            return 200, {"steps": [
                {"type": "user_input", "content": [{"type": "text", "text": "…"}]},
                {"type": "model_output", "content": [{"type": "audio", "mime_type": "audio/wav", "data": data}]},
            ]}
        data = base64.b64encode(self.wav_bytes(raw=True)).decode()
        return 200, {"candidates": [{"content": {"parts": [{"inlineData": {"mimeType": "audio/L16;codec=pcm;rate=24000", "data": data}}]}}]}

    # ----- cycle de vie
    def start(self) -> "World":
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.server.world = self  # type: ignore[attr-defined]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()

    @property
    def base(self) -> str:
        assert self.server is not None
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def feed_url(self, name: str) -> str:
        return f"{self.base}/rss/{name}.xml"

    # ----- journal des requêtes
    def calls(self, prefix: str) -> list[Req]:
        return [r for r in self.requests if r.path.startswith(prefix)]

    def bodies(self, prefix: str) -> str:
        """Tout ce qui a été envoyé à un service (pour vérifier qu'une donnée privée n'y est pas)."""
        return "\n".join(r.plain for r in self.calls(prefix))

    # ----- Telegram : côté « utilisateur »
    def user_text(self, text: str, *, reply_to: int | None = None, chat: int | None = None, ts: int | None = None) -> int:
        return self._push_update({"text": text}, reply_to=reply_to, chat=chat, ts=ts)

    def user_voice(self, data: bytes, *, duration: int = 30, reply_to: int | None = None, ts: int | None = None,
                   file_id: str | None = None) -> int:
        file_id = file_id or f"voice{self._tg_update_id + 1}"
        self.tg_files[file_id] = data
        voice = {"file_id": file_id, "file_unique_id": "u" + file_id, "duration": duration,
                 "mime_type": "audio/ogg", "file_size": len(data)}
        return self._push_update({"voice": voice}, reply_to=reply_to, ts=ts)

    def user_photo(self, data: bytes, *, caption: str | None = None, ts: int | None = None, file_id: str | None = None,
                   chat: int | None = None) -> int:
        """Une photo envoyée « comme photo » : Telegram la fournit en plusieurs tailles (la plus grande en dernier)."""
        file_id = file_id or f"photo{self._tg_update_id + 1}"
        self.tg_files[file_id] = data
        sizes = [
            {"file_id": file_id + "-s", "file_unique_id": "s" + file_id, "width": 90, "height": 60,
             "file_size": max(1, len(data) // 10)},
            {"file_id": file_id, "file_unique_id": "u" + file_id, "width": 1280, "height": 853, "file_size": len(data)},
        ]
        content: dict = {"photo": sizes}
        if caption:
            content["caption"] = caption
        return self._push_update(content, ts=ts, chat=chat)

    def user_file(self, data: bytes, name: str, mime: str, *, caption: str | None = None, ts: int | None = None,
                  file_id: str | None = None) -> int:
        """Un fichier envoyé « en tant que fichier » (image non compressée, PDF…)."""
        file_id = file_id or f"doc{self._tg_update_id + 1}"
        self.tg_files[file_id] = data
        content: dict = {"document": {"file_id": file_id, "file_unique_id": "u" + file_id, "file_name": name,
                                      "mime_type": mime, "file_size": len(data)}}
        if caption:
            content["caption"] = caption
        return self._push_update(content, ts=ts)

    def _push_update(self, content: dict, *, reply_to=None, chat=None, ts=None) -> int:
        self._tg_update_id += 1
        self._tg_message_id += 1
        message = {
            "message_id": self._tg_message_id,
            "from": {"id": chat or self.tg_chat, "is_bot": False, "first_name": "Test"},
            "chat": {"id": chat or self.tg_chat, "type": "private", "first_name": "Test"},
            "date": ts or int(time.time()),
            **content,
        }
        if reply_to:
            message["reply_to_message"] = {"message_id": reply_to}
        self.tg_updates.append({"update_id": self._tg_update_id, "message": message})
        return message["message_id"]

    def sent(self, method: str | None = None) -> list[dict]:
        return [s for s in self.tg_sent if method is None or s["method"] == method]

    # ----- routage
    def dispatch(self, req: Req):
        path = urlparse(req.path).path
        if path.startswith("/rss/"):
            name = path[len("/rss/"):-len(".xml")]
            data = self.feeds.get(name)
            if data is None:
                return 404, {}, b"not found"
            if isinstance(data, int):
                return data, {}, b"error"
            return 200, {"Content-Type": "application/rss+xml"}, rss_xml(name, data).encode("utf-8")
        if path.startswith("/gemini/"):
            model = path[len("/gemini/"):].split(":")[0]
            status, payload = self.gemini_fn(req, model, req.json or {})
            return self._json(status, payload, gemini_reply)
        if path == "/gemini-tts/interactions" or path.startswith("/gemini-tts/legacy/"):
            kind = "interactions" if path.endswith("/interactions") else "legacy"
            status, payload = self.tts_fn(req, kind, req.json or {})
            return self._json(status, payload, lambda t: {"error": {"message": t}})
        if path == "/groq/chat/completions":
            status, payload = self.groq_fn(req, (req.json or {}).get("model", ""), req.json or {})
            return self._json(status, payload, groq_reply)
        if path == "/groq/audio/transcriptions":
            self.stt_calls += 1
            status, payload = self.stt_fn(req, req.multipart())
            return self._json(status, payload, lambda t: {"text": t})
        if path.startswith("/telegram/"):
            return self.telegram(req, path[len("/telegram/"):])
        if path.startswith("/github/"):
            return self.github(req, path[len("/github/"):])
        return 404, {}, b"unknown route " + path.encode()

    @staticmethod
    def _json(status: int, payload, wrap):
        if status != 200:
            body = payload if isinstance(payload, dict) else {"error": {"message": str(payload)}}
            headers = {"Content-Type": "application/json"}
            if status == 429:
                headers["retry-after"] = "1"
            return status, headers, json.dumps(body).encode()
        body = payload if isinstance(payload, dict) else wrap(str(payload))
        return 200, {"Content-Type": "application/json"}, json.dumps(body).encode()

    # ----- Telegram : côté serveur
    def telegram(self, req: Req, rest: str):
        ok = lambda result: (200, {"Content-Type": "application/json"}, json.dumps({"ok": True, "result": result}).encode())  # noqa: E731

        def err(code: int, desc: str):
            return code, {"Content-Type": "application/json"}, json.dumps(
                {"ok": False, "error_code": code, "description": desc}).encode()

        if rest.startswith("file/bot"):  # téléchargement d'un fichier
            token, _, file_path = rest[len("file/bot"):].partition("/")
            if token != self.tg_token:
                return 401, {}, b"unauthorized"
            data = self.tg_files.get(file_path.split("/")[-1].rsplit(".", 1)[0])
            return (200, {"Content-Type": "application/octet-stream"}, data) if data is not None else (404, {}, b"")
        if not rest.startswith("bot"):
            return 404, {}, b""
        token, _, method = rest[3:].partition("/")
        if token != self.tg_token:
            return err(401, "Unauthorized")
        if method in self.tg_fail:
            return err(self.tg_fail[method], "Simulated failure")
        body = req.json or {}
        if method == "getUpdates":
            if self.tg_conflict:
                return err(409, "Conflict: can't use getUpdates method while webhook is active")
            offset = int(body.get("offset") or 0)
            self.tg_updates = [u for u in self.tg_updates if u["update_id"] >= offset]  # Telegram oublie les anciens
            return ok(self.tg_updates[: int(body.get("limit") or 100)])
        if method == "getFile":
            fid = body.get("file_id", "")
            if fid.startswith("toobig"):
                return err(400, "Bad Request: file is too big")
            if fid not in self.tg_files:
                return err(400, "Bad Request: invalid file_id")
            return ok({"file_id": fid, "file_size": len(self.tg_files[fid]), "file_path": f"voice/{fid}.oga"})
        if method == "sendMessage":
            self._tg_message_id += 1
            self.tg_sent.append({"method": "sendMessage", "text": body.get("text", ""),
                                 "markup": body.get("reply_markup"), "message_id": self._tg_message_id,
                                 "silent": bool(body.get("disable_notification"))})
            return ok({"message_id": self._tg_message_id, "chat": {"id": body.get("chat_id")}, "text": body.get("text")})
        if method == "sendVoice":
            fields = req.multipart()
            self._tg_message_id += 1
            name, data = fields.get("voice", ("", b""))
            self.tg_sent.append({"method": "sendVoice", "caption": fields.get("caption", ""), "bytes": len(data),
                                 "filename": name, "chat_id": fields.get("chat_id"), "duration": fields.get("duration"),
                                 "message_id": self._tg_message_id,
                                 "silent": fields.get("disable_notification") == "true"})
            return ok({"message_id": self._tg_message_id, "chat": {"id": int(fields.get("chat_id", 0))}})
        if method == "setMyCommands":
            self.tg_sent.append({"method": "setMyCommands", "commands": body.get("commands")})
            return ok(True)
        return err(404, f"Not Found: method {method}")

    # ----- GitHub
    def github(self, req: Req, rest: str):
        parts = rest.split("/")
        # repos/<proprio>/<dépôt>/actions/workflows/<fichier>/dispatches
        if len(parts) == 7 and parts[0] == "repos" and parts[3:5] == ["actions", "workflows"] and parts[6] == "dispatches":
            body = req.json or {}
            if self.gh_fail:
                return self.gh_fail, {"Content-Type": "application/json"}, json.dumps({"message": "Resource not accessible"}).encode()
            self.gh_dispatches.append({
                "repo": f"{parts[1]}/{parts[2]}", "workflow": parts[5], "ref": body.get("ref"),
                "inputs": body.get("inputs") or {}, "auth": req.headers.get("Authorization", ""),
            })
            if self.gh_ok_status == 204:
                return 204, {}, b""
            details = {"workflow_run_id": 1234567, "run_url": "https://api.github.com/repos/x/y/actions/runs/1234567",
                       "html_url": "https://github.com/x/y/actions/runs/1234567"}
            return self.gh_ok_status, {"Content-Type": "application/json"}, json.dumps(details).encode()
        return 404, {}, b"{}"

    # ----- Groq vision (message utilisateur = liste : texte + images)
    def smart_vision(self, req: Req, model: str, body: dict, text: str, images: list[tuple[str, bytes]]):
        reading = self.vision_reading
        if callable(reading):
            reading = reading(text, images)
        if reading is None:
            reading = {"type": "autre", "description": "Une image", "texte": "", "semaine_du": None, "jours_vus": [], "cours": []}
        return 200, reading if isinstance(reading, str) else json.dumps(reading, ensure_ascii=False)

    # ----- Groq transcription
    def smart_stt(self, req: Req, fields: dict):
        text = self.transcripts.pop(0) if self.transcripts else f"Texte transcrit numéro {self.stt_calls}."
        return 200, {"text": text, "segments": [
            {"text": text, "no_speech_prob": 0.01, "avg_logprob": -0.2, "compression_ratio": 1.2}]}

    # ----- Gemini et Groq « intelligents » par défaut
    def smart_gemini(self, req: Req, model: str, body: dict):
        system = body["systemInstruction"]["parts"][0]["text"]
        prompt = body["contents"][0]["parts"][0]["text"]
        return 200, self.answer(system, prompt)

    def smart_groq(self, req: Req, model: str, body: dict):
        msgs = {m["role"]: m["content"] for m in body["messages"]}
        user = msgs.get("user", "")
        if isinstance(user, list):  # message avec images : [{"type": "text"}, {"type": "image_url", ...}]
            text = "\n".join(p.get("text", "") for p in user if p.get("type") == "text")
            images = []
            for part in user:
                if part.get("type") == "image_url":
                    url = part["image_url"]["url"]
                    head, _, payload = url.partition(",")
                    images.append((head[len("data:"):].split(";")[0], base64.b64decode(payload)))
            self.vision_calls.append({"model": model, "text": text, "images": images})
            return self.vision_fn(req, model, body, text, images)
        return 200, self.answer(msgs.get("system", ""), user)

    def answer(self, system: str, prompt: str) -> str:
        if "rédacteur en chef" in system:
            arts = _ARTICLE_RE.findall(prompt)
            return json.dumps({"articles": [self.score_fn(int(i), s, t) for i, s, t in arts]}, ensure_ascii=False)
        if "voix d'un mini-podcast" in system:
            topics = _TOPIC_RE.findall(prompt)
            if not topics:  # mode libre : liste « - [Source] Titre »
                topics = re.findall(r"^- \[(.*?)\] (.*?)(?: — .*)?$", prompt, re.MULTILINE)
            lines = ["Bonjour, voici ton point du matin."]
            if "[[PERSO]]" in system:
                lines.append("[[PERSO]]")
            lines += [f"Sujet : {title}." for _src, title in topics]
            lines.append("Belle journée à toi.")
            return "\n\n".join(lines)
        if "interlocuteur bienveillant" in system:  # avant « mémoire personnelle » : les deux phrases s'y trouvent
            question = QUESTIONS[self.question_n % len(QUESTIONS)]
            self.question_n += 1
            if "Houdini" in prompt:
                question = "Où en es-tu avec Houdini en ce moment, et qu'est-ce que tu voudrais réussir à faire ?"
            return json.dumps({"question": question, "categorie": "projet"}, ensure_ascii=False)
        if "mémoire personnelle d'un journal" in system:
            return json.dumps(self.extract_fn(prompt), ensure_ascii=False)
        return "OK"
