"""Всё, что связано с Google Таблицей: заказы, пользователи, группы, остатки."""
import base64
import json
import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import gspread

import config
from pdf_parser import Order

log = logging.getLogger("orders-bot")

HEADERS = [
    "Добавлен", "Канал", "Тип", "Дата отгрузки", "Дата доставки", "Размер",
    "Цвет", "Номер заказа", "Артикул", "Отгрузка / телефон", "Адрес", "Статус",
]
ORDER_COL = 8    # «Номер заказа» — по нему ищем дубли и нужные строки
STATUS_COL = 12  # «Статус» — любая непустая отметка убирает заказ из списков
USERS_HEADERS = ["ID", "Имя", "Роль"]
GROUPS_HEADERS = ["Тип", "ID"]
STOCK_HEADERS = ["Дата и время", "Размер", "Остаток, шт.", "Кто внёс"]
MARKET_HEADERS = ["ID заказа", "Впервые замечен", "Кампания", "Как замечен"]
SHIP_HEADERS = ["Дата и время", "Дата отгрузки", "Код", "Размер", "Цвет", "Кол-во",
                "Остаток до", "Остаток после", "Кто внёс", "Номер заказа", "Ножки", "Канал"]

CACHE_TTL = 5  # секунд: чтобы не дёргать Google при каждом нажатии кнопки
_book = None
_tabs: dict = {}
_cache = {"t": 0.0, "rows": None}


def _now_str() -> str:
    return datetime.now(ZoneInfo(config.TIMEZONE)).strftime("%d.%m.%Y %H:%M")


# ---------------------------------------------------------------- доступ
_gc = None


def get_client():
    """Один клиент Google на весь бот (нужен и для основной таблицы, и для таблицы стоимости доставок)."""
    global _gc
    if _gc is None:
        if config.CREDS_JSON:
            raw = config.CREDS_JSON
            if not raw.startswith("{"):
                raw = base64.b64decode(raw).decode("utf-8")
            _gc = gspread.service_account_from_dict(json.loads(raw))
        else:
            _gc = gspread.service_account(filename=config.CREDS_FILE)
    return _gc


def get_book():
    global _book
    if _book is None:
        _book = get_client().open_by_key(config.SHEET_ID)
    return _book


def _ensure_header(ws, headers: list) -> None:
    row1 = ws.row_values(1)
    if not row1:
        ws.append_row(headers, value_input_option="RAW")
    elif len(row1) < len(headers) and headers[: len(row1)] == row1:
        # старая таблица без новых столбцов (например, «Статус») — дописываем шапку
        ws.update(values=[headers], range_name="A1", value_input_option="RAW")


def orders_ws():
    ws = _tabs.get("orders")
    if ws is None:
        book = get_book()
        ws = book.worksheet(config.SHEET_TAB) if config.SHEET_TAB else book.sheet1
        _ensure_header(ws, HEADERS)
        _tabs["orders"] = ws
    return ws


def _tab(title: str, headers: list):
    ws = _tabs.get(title)
    if ws is None:
        book = get_book()
        try:
            ws = book.worksheet(title)
        except gspread.WorksheetNotFound:
            ws = book.add_worksheet(title=title, rows=200, cols=max(len(headers), 4))
        _ensure_header(ws, headers)
        _tabs[title] = ws
    return ws


def sheet_url(kind: str) -> str:
    ws = orders_ws() if kind == "orders" else _tab(config.STOCK_TAB, STOCK_HEADERS)
    return f"https://docs.google.com/spreadsheets/d/{config.SHEET_ID}/edit#gid={ws.id}"


# ---------------------------------------------------------------- заказы
def read_orders(force: bool = False) -> list:
    now = time.time()
    if not force and _cache["rows"] is not None and now - _cache["t"] < CACHE_TTL:
        return _cache["rows"]
    values = orders_ws().get_all_values()
    rows = []
    for i, v in enumerate(values[1:], start=2):
        v = list(v) + [""] * (len(HEADERS) - len(v))
        if not v[ORDER_COL - 1].strip():
            continue
        rows.append({
            "row": i, "added": v[0], "channel": v[1].strip(), "kind": v[2].strip(),
            "ship": v[3].strip(), "delivery": v[4].strip(), "size": v[5].strip(),
            "color": v[6].strip(), "order_no": v[7].strip(), "article": v[8].strip(),
            "phone": v[9].strip(), "address": v[10].strip(), "status": v[11].strip(),
        })
    _cache.update(t=now, rows=rows)
    return rows


def _invalidate() -> None:
    _cache["rows"] = None


def add_orders(orders: list) -> list:
    """Добавляет только новые заказы (по номеру). Возвращает добавленные."""
    existing = {r["order_no"] for r in read_orders(force=True)}
    new = [o for o in orders if o.order_no not in existing]
    if new:
        now = _now_str()
        rows = [[now, o.channel, o.kind, o.ship_date, o.delivery_date, o.size, o.color,
                 o.order_no, o.article, o.shipment_or_phone, o.address, ""] for o in new]
        # RAW — чтобы длинные номера и телефоны не превращались в числа
        orders_ws().append_rows(rows, value_input_option="RAW")
        _invalidate()
    return new


def mark_delivered(order_nos: list) -> list:
    """Пишет в столбец «Статус» отметку о доставке. Строки остаются в таблице, но пропадают из списков."""
    wanted = set(order_nos)
    marked = [r for r in read_orders(force=True) if r["order_no"] in wanted and not r["status"]]
    rows = [r["row"] for r in marked]
    if rows:
        stamp = "Доставлен " + datetime.now(ZoneInfo(config.TIMEZONE)).strftime("%d.%m %H:%M")
        col = chr(64 + STATUS_COL)
        orders_ws().batch_update(
            [{"range": f"{col}{r}", "values": [[stamp]]} for r in rows],
            value_input_option="RAW",
        )
        _invalidate()
    return marked  # только те, что отмечены сейчас (повторное нажатие ничего не вернёт)


# ---------------------------------------------------------- пользователи
_LABEL_TO_KEY = {v.lower(): k for k, v in config.ROLE_LABELS.items()}


def load_users() -> dict:
    users = {}
    for v in _tab(config.USERS_TAB, USERS_HEADERS).get_all_values()[1:]:
        if len(v) >= 3 and v[0].strip().isdigit():
            role = _LABEL_TO_KEY.get(v[2].strip().lower())
            if role:
                users[int(v[0])] = {"name": v[1], "role": role}
    return users


def save_user(uid: int, name: str, role: str) -> None:
    ws = _tab(config.USERS_TAB, USERS_HEADERS)
    values = [config.ROLE_LABELS[role]]
    row = [str(uid), name, values[0]]
    ids = ws.col_values(1)
    if str(uid) in ids:
        i = ids.index(str(uid)) + 1
        ws.update(values=[row], range_name=f"A{i}:C{i}", value_input_option="RAW")
    else:
        ws.append_row(row, value_input_option="RAW")


def delete_user(uid: int) -> None:
    ws = _tab(config.USERS_TAB, USERS_HEADERS)
    ids = ws.col_values(1)
    if str(uid) in ids:
        ws.delete_rows(ids.index(str(uid)) + 1)


# ---------------------------------------------------------------- группы
def load_groups() -> list:
    groups = []
    for v in _tab(config.GROUPS_TAB, GROUPS_HEADERS).get_all_values()[1:]:
        if len(v) >= 2 and v[0] == "chat" and v[1].strip().lstrip("-").isdigit():
            groups.append(int(v[1]))
    return groups


def add_group(chat_id: int) -> None:
    _tab(config.GROUPS_TAB, GROUPS_HEADERS).append_row(["chat", str(chat_id)], value_input_option="RAW")


def remove_group(chat_id: int) -> None:
    ws = _tab(config.GROUPS_TAB, GROUPS_HEADERS)
    for i, v in enumerate(ws.get_all_values(), start=1):
        if len(v) >= 2 and v[0] == "chat" and v[1] == str(chat_id):
            ws.delete_rows(i)
            return


# --------------------------------------------------------------- остатки
def add_stock(code: str, qty: int, who: str) -> None:
    _tab(config.STOCK_TAB, STOCK_HEADERS).append_row(
        [_now_str(), code, qty, who], value_input_option="RAW")


def latest_stock() -> dict:
    """{'120.04': (5, '25.10.2026 14:30')} — последнее внесённое значение по каждому размеру."""
    res = {}
    for v in _tab(config.STOCK_TAB, STOCK_HEADERS).get_all_values()[1:]:
        if len(v) >= 3 and v[1].strip():
            try:
                res[v[1].strip()] = (int(float(v[2])), v[0])
            except ValueError:
                continue
    return res


# -------------------------------------------------------------- отгрузки
def add_shipment(code: str, qty: int, who: str, before, after, day, order_no: str = "",
                 legs: str = "", channel: str = "") -> None:
    """Запись «кровать отгружена» (курьер отметил доставку); before/after — остаток до и после (None, если не списывали)."""
    width, color = code.split(".")
    _tab(config.SHIP_TAB, SHIP_HEADERS).append_row(
        [_now_str(), day.strftime("%d.%m.%Y"), code, f"{width}/{legs or 30}",
         {"04": "чёрная", "06": "белая"}.get(color, color), qty,
         "" if before is None else before, "" if after is None else after, who, order_no, legs, channel],
        value_input_option="RAW")


def shipped_on(day) -> dict:
    """{'160.04': 2} — сколько отгружено в указанный день."""
    want = day.strftime("%d.%m.%Y")
    res: dict = {}
    for v in _tab(config.SHIP_TAB, SHIP_HEADERS).get_all_values()[1:]:
        if len(v) >= 6 and v[1].strip() == want:
            try:
                res[v[2].strip()] = res.get(v[2].strip(), 0) + int(float(v[5]))
            except ValueError:
                continue
    return res


# ---------------------------------------------------------------- Маркет
def market_seen() -> set:
    """ID заказов Маркета, о которых бот уже знает."""
    return {v[0].strip() for v in _tab(config.MARKET_TAB, MARKET_HEADERS).get_all_values()[1:]
            if v and v[0].strip()}


def market_add(rows: list) -> None:
    if rows:
        _tab(config.MARKET_TAB, MARKET_HEADERS).append_rows(rows, value_input_option="RAW")


def set_shipment_cell(row: int, value: str) -> None:
    """Вписывает номер отгрузки в столбец «Отгрузка / телефон» уже существующей строки."""
    orders_ws().update(values=[[value]], range_name=f"J{row}", value_input_option="RAW")
    _invalidate()


# ------------------------------------------------------------------ Озон
def oz_seen() -> set:
    return {v[0].strip() for v in _tab(config.OZON_TAB, MARKET_HEADERS).get_all_values()[1:] if v and v[0].strip()}


def oz_add(rows: list) -> None:
    if rows:
        _tab(config.OZON_TAB, MARKET_HEADERS).append_rows(rows, value_input_option="RAW")
