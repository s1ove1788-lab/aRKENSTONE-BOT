"""
Бот для MAX: принимает PDF с заказами от менеджера, добавляет новые заказы
в Google Таблицу и уведомляет всех подписчиков.
"""
import base64
import json
import logging
import os
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import gspread
import requests
from dotenv import load_dotenv

from pdf_parser import Order, parse_pdf

load_dotenv()

# ---------------------------------------------------------------- настройки
TOKEN = (os.getenv("MAX_BOT_TOKEN") or os.getenv("BOT_TOKEN") or "").strip()
API = os.getenv("MAX_API_BASE", "https://platform-api.max.ru").rstrip("/")
def _parse_ids(raw: str) -> set:
    ids = set()
    for part in raw.replace(" ", "").replace(";", ",").split(","):
        if part.lstrip("-").isdigit():
            ids.add(int(part))
        elif part:
            print(f"ВНИМАНИЕ: в MANAGER_IDS должно быть число, а не «{part}» — пропускаю", flush=True)
    return ids


MANAGER_IDS = _parse_ids(os.getenv("MANAGER_IDS", ""))
CREDS_FILE = os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")
# Либо содержимое credentials.json целиком (для хостингов), либо то же в base64
CREDS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON", "").strip()
SUBS_TAB = "Подписчики"  # лист, где хранится список подписчиков
SHEET_ID = os.getenv("SHEET_ID", "").strip().strip("\"'")
if "/d/" in SHEET_ID:  # вставили ссылку целиком — достаём из неё код
    SHEET_ID = SHEET_ID.split("/d/")[1].split("/")[0]
SHEET_TAB = os.getenv("SHEET_TAB", "").strip()  # пусто = первый лист
TIMEZONE = os.getenv("TIMEZONE", "Europe/Moscow")
STATE_FILE = Path(os.getenv("STATE_FILE", "state.json"))

MAX_PDF_BYTES = 20 * 1024 * 1024
MAX_TEXT_LEN = 3500  # у MAX лимит на сообщение ~4000 символов

HEADERS = [
    "Добавлен", "Канал", "Тип", "Дата отгрузки", "Дата доставки", "Размер",
    "Цвет", "Номер заказа", "Артикул", "Отгрузка / телефон", "Адрес",
]
ORDER_COL = HEADERS.index("Номер заказа") + 1  # по этой колонке ищем дубли

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("orders-bot")

HELP_TEXT = (
    "Привет! Я бот заказов.\n\n"
    "• Менеджер присылает мне PDF с заказами — я добавляю новые в таблицу.\n"
    "• Все подписчики получают уведомление о новых заказах.\n\n"
    "Команды:\n"
    "/start — подписаться на уведомления (в личке)\n"
    "/subscribe — подписать на уведомления групповой чат (написать в группе)\n"
    "/stop — отписаться\n"
    "/id — показать ваш ID"
)

# ------------------------------------------------------------- состояние
def load_state() -> dict:
    """Маркер последнего события храним в файле (если он переживёт перезапуск),
    а подписчиков — в Google Таблице, на листе «Подписчики»."""
    st = {"marker": None, "subscribers": []}
    if STATE_FILE.exists():
        try:
            st["marker"] = json.loads(STATE_FILE.read_text(encoding="utf-8")).get("marker")
        except Exception:
            log.warning("Не смог прочитать %s, начинаю с чистого состояния", STATE_FILE)
    return st


def save_state() -> None:
    try:
        STATE_FILE.write_text(json.dumps({"marker": state["marker"]}), encoding="utf-8")
    except Exception:
        log.warning("Не смог сохранить %s (не страшно)", STATE_FILE)


state = load_state()


def load_subscribers() -> None:
    ws = get_subs_sheet()
    subs = []
    for row in ws.get_all_values()[1:]:
        if len(row) >= 2 and row[0] in ("user", "chat") and row[1].strip().lstrip("-").isdigit():
            subs.append({"type": row[0], "id": int(row[1])})
    state["subscribers"] = subs
    log.info("Подписчиков: %d", len(subs))


def subscribe(target: dict) -> bool:
    if target in state["subscribers"]:
        return False
    state["subscribers"].append(target)
    try:
        get_subs_sheet().append_row([target["type"], str(target["id"])], value_input_option="RAW")
    except Exception:
        log.exception("Не смог записать подписчика в таблицу")
    return True


def unsubscribe(target: dict) -> bool:
    if target not in state["subscribers"]:
        return False
    state["subscribers"].remove(target)
    try:
        ws = get_subs_sheet()
        for i, row in enumerate(ws.get_all_values(), start=1):
            if len(row) >= 2 and row[0] == target["type"] and row[1] == str(target["id"]):
                ws.delete_rows(i)
                break
    except Exception:
        log.exception("Не смог удалить подписчика из таблицы")
    return True


# ------------------------------------------------------------------ MAX API
def _headers() -> dict:
    return {"Authorization": TOKEN}


def send(target: dict, text: str) -> bool:
    """target = {"type": "user" | "chat", "id": 123}. Длинный текст режется на части."""
    key = "user_id" if target["type"] == "user" else "chat_id"
    ok = True
    for chunk in split_text(text):
        try:
            r = requests.post(
                f"{API}/messages",
                params={key: target["id"]},
                headers=_headers(),
                json={"text": chunk},
                timeout=30,
            )
            if r.status_code != 200:
                log.warning("Не отправилось %s: %s %s", target, r.status_code, r.text[:300])
                ok = False
        except requests.RequestException:
            log.exception("Ошибка сети при отправке %s", target)
            ok = False
        time.sleep(0.3)  # не частим
    return ok


def split_text(text: str) -> list[str]:
    parts, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > MAX_TEXT_LEN and cur:
            parts.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        parts.append(cur.rstrip("\n"))
    return parts or [""]


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


# --------------------------------------------------------- Google Таблица
_book = None
_ws = None


def get_book():
    global _book
    if _book is None:
        if CREDS_JSON:
            raw = CREDS_JSON
            if not raw.startswith("{"):
                raw = base64.b64decode(raw).decode("utf-8")
            gc = gspread.service_account_from_dict(json.loads(raw))
        else:
            gc = gspread.service_account(filename=CREDS_FILE)
        _book = gc.open_by_key(SHEET_ID)
    return _book


def get_sheet():
    global _ws
    if _ws is None:
        book = get_book()
        _ws = book.worksheet(SHEET_TAB) if SHEET_TAB else book.sheet1
        if not _ws.row_values(1):
            _ws.append_row(HEADERS, value_input_option="RAW")
    return _ws


def get_subs_sheet():
    book = get_book()
    try:
        return book.worksheet(SUBS_TAB)
    except gspread.WorksheetNotFound:
        ws = book.add_worksheet(title=SUBS_TAB, rows=100, cols=2)
        ws.append_row(["Тип", "ID"], value_input_option="RAW")
        return ws


def add_to_sheet(orders: list[Order]) -> list[Order]:
    """Добавляет в таблицу только те заказы, которых там ещё нет. Возвращает добавленные."""
    ws = get_sheet()
    existing = {v.strip() for v in ws.col_values(ORDER_COL)}
    new = [o for o in orders if o.order_no not in existing]
    if new:
        now = datetime.now(ZoneInfo(TIMEZONE)).strftime("%d.%m.%Y %H:%M")
        rows = [
            [now, o.channel, o.kind, o.ship_date, o.delivery_date, o.size, o.color,
             o.order_no, o.article, o.shipment_or_phone, o.address]
            for o in new
        ]
        # RAW — чтобы длинные номера и телефоны не превращались в числа
        ws.append_rows(rows, value_input_option="RAW")
    return new


# ------------------------------------------------------------- уведомления
def format_order(o: Order) -> str:
    what = " ".join(x for x in (o.size, o.color) if x)
    return (
        f"• {o.channel} · {what} · {o.address}\n"
        f"  отгрузка {o.ship_date}, доставка {o.delivery_date} · № {o.order_no}"
    )


def notify_all(new: list[Order], who: str) -> None:
    lines = [f"🆕 Новых заказов: {len(new)}" + (f" (загрузил(а): {who})" if who else "")]
    lines += [format_order(o) for o in new]
    if SHEET_ID:
        lines.append(f"\nТаблица: https://docs.google.com/spreadsheets/d/{SHEET_ID}")
    text = "\n".join(lines)
    for target in list(state["subscribers"]):
        send(target, text)


# --------------------------------------------------------------- обработка
def process_pdf(url: str, filename: str, reply_to: dict, who: str) -> None:
    send(reply_to, f"Читаю файл «{filename}»…")
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "orders.pdf")
        try:
            download(url, path)
            orders = parse_pdf(path)
        except ValueError as e:
            send(reply_to, f"⚠️ {e}")
            return
        except Exception:
            log.exception("Ошибка при чтении PDF")
            send(reply_to, "⚠️ Не получилось скачать или прочитать файл. Попробуйте отправить ещё раз.")
            return

    try:
        new = add_to_sheet(orders)
    except Exception:
        log.exception("Ошибка при записи в Google Таблицу")
        send(reply_to, "⚠️ Заказы прочитал, но записать в таблицу не вышло. Сообщите администратору.")
        return

    dups = len(orders) - len(new)
    if not new:
        send(reply_to, f"Все {len(orders)} заказов из файла уже есть в таблице, ничего не добавлял.")
        return
    notify_all(new, who)
    summary = f"✅ Добавлено в таблицу: {len(new)}"
    if dups:
        summary += f"\nПропущено (уже были): {dups}"
    send(reply_to, summary)


def handle_update(u: dict) -> None:
    kind = u.get("update_type")

    if kind == "bot_started":
        user = u.get("user") or {}
        if user.get("user_id"):
            target = {"type": "user", "id": user["user_id"]}
            subscribe(target)
            send(target, HELP_TEXT + "\n\n✅ Вы подписаны на уведомления.")
        return

    if kind != "message_created":
        return

    msg = u.get("message") or {}
    sender = msg.get("sender") or {}
    if sender.get("is_bot"):
        return
    sender_id = sender.get("user_id")
    recipient = msg.get("recipient") or {}
    in_group = recipient.get("chat_type") == "chat"
    if in_group:
        reply_to = {"type": "chat", "id": recipient.get("chat_id")}
    else:
        reply_to = {"type": "user", "id": sender_id}
    if not reply_to["id"]:
        return

    body = msg.get("body") or {}
    text = (body.get("text") or "").strip().lower().split("@")[0].split(" ")[0]

    if text == "/id":
        send(reply_to, f"Ваш ID: {sender_id}")
        return
    if text in ("/start", "/subscribe"):
        if subscribe(reply_to):
            send(reply_to, HELP_TEXT + "\n\n✅ Подписал на уведомления.")
        else:
            send(reply_to, "Уже подписаны ✅")
        return
    if text == "/stop":
        send(reply_to, "Отписал." if unsubscribe(reply_to) else "Вы и не были подписаны.")
        return

    for att in body.get("attachments") or []:
        if att.get("type") != "file":
            continue
        filename = att.get("filename") or "file.pdf"
        if not filename.lower().endswith(".pdf"):
            if not in_group:
                send(reply_to, "Мне нужен именно PDF-файл.")
            continue
        if sender_id not in MANAGER_IDS:
            if not in_group:
                send(reply_to, f"Загружать файлы может только менеджер. Ваш ID: {sender_id}")
            continue
        url = (att.get("payload") or {}).get("url")
        if not url:
            send(reply_to, "⚠️ MAX не передал ссылку на файл. Попробуйте отправить ещё раз.")
            continue
        who = sender.get("name") or sender.get("first_name") or ""
        process_pdf(url, filename, reply_to, who)


# -------------------------------------------------------------------- запуск
def main() -> None:
    if not TOKEN or not SHEET_ID:
        wanted = ["MAX_BOT_TOKEN", "BOT_TOKEN", "SHEET_ID", "GOOGLE_CREDENTIALS_JSON", "MANAGER_IDS"]
        seen = ", ".join(f"{k}={'есть' if os.getenv(k) else 'ПУСТО'}" for k in wanted)
        extra = [k for k in os.environ if "SHEET" in k.upper() and k != "SHEET_ID"]
        hint = f" Похожие по названию: {extra}." if extra else ""
        sys.exit(
            "Не хватает настроек (переменные окружения в панели хостинга или файл .env). "
            f"Бот видит: {seen}.{hint} Проверьте написание ключей: латиница, заглавные, без пробелов."
        )
    if not MANAGER_IDS:
        log.warning("MANAGER_IDS пуст: никто не сможет загружать файлы. "
                    "Напишите боту /id и впишите свой ID в .env")

    me = requests.get(f"{API}/me", headers=_headers(), timeout=30)
    if me.status_code in (401, 403):
        sys.exit("MAX не принял токен. Проверьте MAX_BOT_TOKEN в .env")
    me.raise_for_status()
    log.info("Бот запущен: %s", me.json().get("name") or me.json())

    try:
        load_subscribers()
    except Exception:
        log.exception("Не смог загрузить подписчиков из таблицы. Проверьте SHEET_ID, "
                      "ключ Google и доступ сервисного аккаунта к таблице")

    marker = state.get("marker")
    while True:
        params = {"timeout": 30, "limit": 100, "types": "message_created,bot_started"}
        if marker is not None:
            params["marker"] = marker
        try:
            r = requests.get(f"{API}/updates", params=params, headers=_headers(), timeout=45)
            if r.status_code in (401, 403):
                sys.exit("MAX не принял токен. Проверьте MAX_BOT_TOKEN в .env")
            r.raise_for_status()
            data = r.json()
        except requests.RequestException:
            log.exception("Ошибка связи с MAX, повторю через 5 секунд")
            time.sleep(5)
            continue

        for update in data.get("updates") or []:
            try:
                handle_update(update)
            except Exception:
                log.exception("Ошибка при обработке события")

        if data.get("marker") is not None:
            marker = data["marker"]
            state["marker"] = marker
            save_state()


if __name__ == "__main__":
    main()
