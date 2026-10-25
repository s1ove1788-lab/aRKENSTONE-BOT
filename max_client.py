"""Тонкая обёртка над HTTP API мессенджера MAX."""
import logging
import time

import requests

import config

log = logging.getLogger("orders-bot")

MAX_PDF_BYTES = 20 * 1024 * 1024
MAX_TEXT_LEN = 3500  # у MAX лимит на сообщение ~4000 символов
_last_sent: dict = {}


def _headers() -> dict:
    return {"Authorization": config.TOKEN}


# ---------------------------------------------------------------- кнопки
def cb(text: str, payload: str) -> dict:
    """Кнопка, по нажатию на которую бот получит payload."""
    return {"type": "callback", "text": text[:120], "payload": payload}


def link(text: str, url: str) -> dict:
    """Кнопка-ссылка (открывает адрес в браузере)."""
    return {"type": "link", "text": text[:120], "url": url}


def _keyboard(rows):
    if not rows:
        return []
    return [{"type": "inline_keyboard", "payload": {"buttons": rows}}]


# ------------------------------------------------------------- сообщения
def split_text(text: str) -> list:
    parts, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > MAX_TEXT_LEN and cur:
            parts.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        parts.append(cur.rstrip("\n"))
    return parts or [""]


def _throttle(target: dict) -> None:
    """MAX разрешает не больше 2 сообщений в секунду в один чат."""
    key = (target["type"], target["id"])
    wait = 0.55 - (time.time() - _last_sent.get(key, 0))
    if wait > 0:
        time.sleep(wait)
    _last_sent[key] = time.time()


def send(target: dict, text: str, rows=None) -> bool:
    """target = {"type": "user" | "chat", "id": 123}; rows — ряды кнопок (необязательно)."""
    param = "user_id" if target["type"] == "user" else "chat_id"
    chunks = split_text(text)
    ok = True
    for i, chunk in enumerate(chunks):
        body = {"text": chunk}
        if rows and i == len(chunks) - 1:
            body["attachments"] = _keyboard(rows)
        _throttle(target)
        try:
            r = requests.post(f"{config.API}/messages", params={param: target["id"]},
                              headers=_headers(), json=body, timeout=30)
            if r.status_code != 200:
                log.warning("Не отправилось %s: %s %s", target, r.status_code, r.text[:300])
                ok = False
        except requests.RequestException:
            log.exception("Ошибка сети при отправке %s", target)
            ok = False
    return ok


def answer(callback_id: str, text=None, rows=None, notification=None) -> bool:
    """Ответ на нажатие кнопки: всплывающая подсказка и/или замена текста сообщения с кнопками."""
    body = {}
    if notification:
        body["notification"] = notification
    if text is not None:
        body["message"] = {"text": text[:4000], "attachments": _keyboard(rows)}
    if not body:
        return True
    try:
        r = requests.post(f"{config.API}/answers", params={"callback_id": callback_id},
                          headers=_headers(), json=body, timeout=30)
        if r.status_code != 200:
            log.warning("Не удалось ответить на кнопку: %s %s", r.status_code, r.text[:300])
            return False
        return True
    except requests.RequestException:
        log.exception("Ошибка сети при ответе на кнопку")
        return False


# --------------------------------------------------------------- прочее
def get_me():
    return requests.get(f"{config.API}/me", headers=_headers(), timeout=30)


def get_updates(marker):
    params = {"timeout": 30, "limit": 100, "types": "message_created,message_callback,bot_started"}
    if marker is not None:
        params["marker"] = marker
    return requests.get(f"{config.API}/updates", params=params, headers=_headers(), timeout=45)


def download(url: str, dest: str) -> None:
    r = requests.get(url, stream=True, timeout=60)
    if r.status_code in (401, 403):
        r = requests.get(url, stream=True, timeout=60, headers=_headers())
    r.raise_for_status()
    size = 0
    with open(dest, "wb") as f:
        for part in r.iter_content(65536):
            size += len(part)
            if size > MAX_PDF_BYTES:
                raise ValueError("Файл слишком большой (больше 20 МБ).")
            f.write(part)
