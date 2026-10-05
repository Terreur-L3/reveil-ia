"""Client minimal de l'API Telegram Bot (https://core.telegram.org/bots/api).

Sécurité : seule TA conversation (TELEGRAM_CHAT_ID) est écoutée. N'importe qui peut écrire à un bot :
tout message venant d'ailleurs est ignoré, sans réponse. Le jeton n'apparaît jamais dans les erreurs.

Aide pour trouver ton identifiant :   python -m src.telegram
"""
from __future__ import annotations

import logging
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import requests

log = logging.getLogger(__name__)

API_ROOT = "https://api.telegram.org"
MAX_DOWNLOAD = 20 * 1024 * 1024  # limite imposée par Telegram aux bots pour télécharger un fichier
MAX_TEXT = 4000                  # un message Telegram accepte 4096 caractères
AUDIO_EXT = {
    ".mp3", ".m4a", ".ogg", ".oga", ".opus", ".wav", ".flac", ".aac", ".amr", ".3gp",
    ".mp4", ".webm", ".mpga", ".mpeg",
}

BTN_INSPI = "💡 Inspi"
BTN_MEMOIRE = "🧠 Mémoire"
KEYBOARD = {
    "keyboard": [[{"text": BTN_INSPI}, {"text": BTN_MEMOIRE}]],
    "resize_keyboard": True,
    "is_persistent": True,
}
COMMANDS = [
    {"command": "inspi", "description": "Je n'ai plus d'inspiration : pose-moi une question"},
    {"command": "memoire", "description": "Ce que tu as appris sur moi"},
    {"command": "aide", "description": "Comment ça marche"},
]


class TelegramError(RuntimeError):
    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


class FileTooBig(TelegramError):
    pass


class Conflict(TelegramError):
    """409 : un webhook est actif, ou un autre programme lit déjà les messages."""


@dataclass
class Incoming:
    update_id: int
    message_id: int
    ts: int
    kind: str              # "text" ou "audio"
    text: str = ""         # le texte du message (ou la légende d'un vocal)
    file_id: str = ""
    file_size: int = 0
    duration: int = 0
    name: str = ""
    reply_to: int | None = None
    forwarded: bool = False


def normalize(update: dict, chat_id: int) -> Incoming | None:
    """Transforme une mise à jour Telegram en message simple. None = à ignorer (autre chat, photo, etc.)."""
    msg = update.get("message")
    if not isinstance(msg, dict):
        return None
    chat = msg.get("chat") or {}
    if chat.get("id") != chat_id or chat.get("type") != "private":
        return None
    if (msg.get("from") or {}).get("is_bot"):
        return None
    base = {
        "update_id": int(update.get("update_id", 0)),
        "message_id": int(msg.get("message_id", 0)),
        "ts": int(msg.get("date", 0)),
        "reply_to": (msg.get("reply_to_message") or {}).get("message_id"),
        "forwarded": bool(msg.get("forward_origin") or msg.get("forward_date")),
    }
    caption = str(msg.get("caption") or "")
    for key in ("voice", "audio"):
        media = msg.get(key)
        if isinstance(media, dict) and media.get("file_id"):
            return Incoming(
                kind="audio", text=caption, file_id=media["file_id"], file_size=int(media.get("file_size") or 0),
                duration=int(media.get("duration") or 0), name=str(media.get("file_name") or ""), **base,
            )
    doc = msg.get("document")
    if isinstance(doc, dict) and doc.get("file_id"):
        name = str(doc.get("file_name") or "")
        mime = str(doc.get("mime_type") or "")
        if mime.startswith(("audio/", "video/")) or Path(name).suffix.lower() in AUDIO_EXT:
            return Incoming(
                kind="audio", text=caption, file_id=doc["file_id"], file_size=int(doc.get("file_size") or 0),
                name=name, **base,
            )
        return None
    if isinstance(msg.get("text"), str):
        return Incoming(kind="text", text=msg["text"], **base)
    return None


def _chunks(text: str, size: int = MAX_TEXT) -> list[str]:
    text = text.strip() or "…"
    parts: list[str] = []
    while len(text) > size:
        cut = text.rfind("\n", 0, size)
        if cut < size // 2:
            cut = text.rfind(" ", 0, size)
        if cut < size // 2:
            cut = size
        parts.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    parts.append(text)
    return parts


class Bot:
    def __init__(self, token: str, chat_id: int | str, *, timeout: int = 30):
        self.token = str(token).strip()
        self.chat_id = int(chat_id)
        self.timeout = timeout

    @classmethod
    def from_env(cls, environ=None) -> "Bot | None":
        environ = os.environ if environ is None else environ
        token = (environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
        chat = (environ.get("TELEGRAM_CHAT_ID") or "").strip()
        if not token or not chat:
            return None
        try:
            return cls(token, chat)
        except ValueError:
            log.warning("TELEGRAM_CHAT_ID doit être un nombre : Telegram désactivé")
            return None

    # ----- appels bruts
    def _url(self, method: str) -> str:
        return f"{API_ROOT}/bot{self.token}/{method}"

    def _call(self, method: str, *, body: dict | None = None, data: dict | None = None,
              files: dict | None = None, timeout: int | None = None, retries: int = 2):
        last = ""
        for attempt in range(retries + 1):
            try:
                resp = requests.post(
                    self._url(method), json=body, data=data, files=files, timeout=timeout or self.timeout
                )
            except requests.RequestException as exc:  # jamais le texte de l'exception : il contient le jeton
                last = f"réseau ({exc.__class__.__name__})"
                time.sleep(2 * (attempt + 1))
                continue
            try:
                payload = resp.json()
            except ValueError:
                payload = {}
            if payload.get("ok"):
                return payload.get("result")
            code = int(payload.get("error_code") or resp.status_code)
            desc = str(payload.get("description") or resp.text[:200])
            last = f"HTTP {code} {desc}"
            if code == 429 and attempt < retries:
                wait = (payload.get("parameters") or {}).get("retry_after", 5)
                time.sleep(min(float(wait), 30.0))
                continue
            if code in (500, 502, 503, 504) and attempt < retries:
                time.sleep(2 * (attempt + 1))
                continue
            if code == 409:
                raise Conflict(f"{method} : {desc}", code)
            if "file is too big" in desc.lower():
                raise FileTooBig(f"{method} : {desc}", code)
            raise TelegramError(f"{method} : {desc}", code)
        raise TelegramError(f"{method} : {last}")

    # ----- lecture
    def get_updates(self, offset: int | None = None, timeout: int = 0, limit: int = 100) -> list[dict]:
        body: dict = {"timeout": timeout, "limit": limit, "allowed_updates": ["message"]}
        if offset:
            body["offset"] = offset
        return self._call("getUpdates", body=body, timeout=timeout + 20) or []

    def download(self, file_id: str, dest: Path) -> Path:
        info = self._call("getFile", body={"file_id": file_id}) or {}
        size = int(info.get("file_size") or 0)
        if size > MAX_DOWNLOAD:
            raise FileTooBig(f"getFile : fichier de {size // 1024 // 1024} Mo", 400)
        file_path = info.get("file_path")
        if not file_path:
            raise TelegramError("getFile : pas de chemin de fichier")
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + ".part")
        last = ""
        for attempt in range(3):
            try:
                with requests.get(f"{API_ROOT}/file/bot{self.token}/{file_path}", stream=True, timeout=120) as resp:
                    if resp.status_code != 200:
                        last = f"HTTP {resp.status_code}"
                    else:
                        with open(part, "wb") as fh:
                            for block in resp.iter_content(1 << 16):
                                fh.write(block)
                        if part.stat().st_size > 0:
                            os.replace(part, dest)
                            return dest
                        last = "fichier vide"
            except requests.RequestException as exc:
                last = f"réseau ({exc.__class__.__name__})"
            part.unlink(missing_ok=True)
            time.sleep(2 * (attempt + 1))
        raise TelegramError(f"téléchargement impossible ({last})")

    # ----- écriture
    def send_message(self, text: str, *, reply_markup: dict | None = None, reply_to: int | None = None) -> dict:
        parts = _chunks(text)
        result: dict = {}
        for i, part in enumerate(parts):
            body: dict = {"chat_id": self.chat_id, "text": part}
            if reply_markup and i == len(parts) - 1:
                body["reply_markup"] = reply_markup
            if reply_to and i == 0:
                body["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
            result = self._call("sendMessage", body=body) or {}
        return result

    def send_voice(self, path: Path | str, *, caption: str | None = None, duration: int | None = None) -> dict:
        path = Path(path)
        data = {"chat_id": str(self.chat_id)}
        if caption:
            data["caption"] = caption[:1000]
        if duration:
            data["duration"] = str(int(duration))
        files = {"voice": (path.name, path.read_bytes(), "audio/mpeg" if path.suffix == ".mp3" else "audio/ogg")}
        return self._call("sendVoice", data=data, files=files, timeout=120) or {}

    def set_commands(self) -> None:
        self._call("setMyCommands", body={"commands": COMMANDS})


def _cli() -> int:
    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    if not token:
        print("Il manque TELEGRAM_BOT_TOKEN (le jeton donné par @BotFather).")
        return 1
    try:
        updates = Bot(token, 0).get_updates()
    except TelegramError as exc:
        print(f"Telegram refuse : {exc}")
        return 1
    seen: dict[int, tuple[str, str]] = {}
    for upd in updates:
        chat = (upd.get("message") or {}).get("chat") or {}
        if chat.get("type") == "private" and chat.get("id"):
            seen[chat["id"]] = (str(chat.get("first_name") or ""), str(chat.get("username") or ""))
    if not seen:
        print("Aucun message trouvé. Ouvre ton bot dans Telegram, envoie-lui /start, puis relance cette commande.")
        return 1
    for cid, (first, user) in seen.items():
        print(f"TELEGRAM_CHAT_ID = {cid}    (compte : {first} @{user})")
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
