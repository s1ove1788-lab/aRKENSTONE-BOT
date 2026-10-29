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


SETTINGS_HEADERS = ["Ключ", "Значение"]
_settings: dict = {"t": 0.0, "data": {}}


def get_setting(key: str, max_age: float = 60.0) -> str:
    """Значение из листа «Настройки» (там бот хранит, например, код созданной таблицы стоимости доставок)."""
    if time.time() - _settings["t"] > max_age:
        rows = _tab(config.SETTINGS_TAB, SETTINGS_HEADERS).get_all_values()[1:]
        _settings["data"] = {r[0].strip(): (r[1].strip() if len(r) > 1 else "") for r in rows if r and r[0].strip()}
        _settings["t"] = time.time()
    return _settings["data"].get(key, "")


def set_setting(key: str, value: str) -> None:
    ws = _tab(config.SETTINGS_TAB, SETTINGS_HEADERS)
    cell = ws.find(key, in_column=1)
    if cell:
        ws.update(values=[[value]], range_name=f"B{cell.row}", value_input_option="RAW")
    else:
        ws.append_row([key, value], value_input_option="RAW")
    _settings["data"][key] = value


def sheet_url(kind: str) -> str:
    ws = orders_ws() if kind == "orders" else _stock_ws()
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


# ------------------------------------------------------------- цвета строк
GREEN = {"red": 0.0, "green": 1.0, "blue": 0.0}      # доставлен
YELLOW = {"red": 1.0, "green": 1.0, "blue": 0.0}     # ещё не доставлен
RED = {"red": 1.0, "green": 0.3, "blue": 0.3}        # отмена или возврат
WHITE = {"red": 1.0, "green": 1.0, "blue": 1.0}
_colored: dict = {}  # номер строки -> (номер заказа, цвет), чтобы не перекрашивать лишний раз


def color_for(status: str):
    """Цвет строки по столбцу «Статус»: пусто — жёлтый, «Доставлен…» — зелёный, «Отмена/Возврат» — красный."""
    st = (status or "").strip().lower().replace("ё", "е")
    if not st:
        return "yellow", YELLOW
    if "отмен" in st or "возврат" in st:
        return "red", RED
    if st.startswith("доставлен"):
        return "green", GREEN
    return None


def sync_colors() -> int:
    """Закрашивает строки заказов по статусу (зелёный / жёлтый / красный). Возвращает, сколько строк перекрашено.
    Работает и для ручных пометок менеджера («Отмена», «Возврат»): вызывается после изменений и раз в 5 минут."""
    ws = orders_ws()
    todo: list = []  # (строка, имя цвета, цвет)
    seen_rows = set()
    for r in read_orders(force=True):
        seen_rows.add(r["row"])
        c = color_for(r["status"])
        name, rgb = c if c else ("none", WHITE)
        if c is None and r["row"] not in _colored:
            continue  # незнакомый статус и мы эту строку не красили — не трогаем
        if _colored.get(r["row"]) != (r["order_no"], name):
            todo.append((r["row"], r["order_no"], name, rgb))
    if not todo:
        return 0
    requests = []
    for row, _, _, rgb in todo:
        requests.append({"repeatCell": {
            "range": {"sheetId": ws.id, "startRowIndex": row - 1, "endRowIndex": row,
                      "startColumnIndex": 0, "endColumnIndex": len(HEADERS)},
            "cell": {"userEnteredFormat": {"backgroundColor": rgb}},
            "fields": "userEnteredFormat.backgroundColor"}})
    for i in range(0, len(requests), 200):
        get_book().batch_update({"requests": requests[i:i + 200]})
    for row, order_no, name, _ in todo:
        _colored[row] = (order_no, name)
    return len(todo)


def safe_sync_colors() -> None:
    try:
        sync_colors()
    except Exception:
        log.exception("Не смог закрасить строки таблицы заказов")


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
        safe_sync_colors()
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
        safe_sync_colors()
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
_COLOR_BY_HEADER = (("черн", "04"), ("бел", "06"))


def _stock_ws():
    ws = _tabs.get("stock")
    if ws is None:
        book = get_book()
        try:
            ws = book.worksheet(config.STOCK_TAB)
        except gspread.WorksheetNotFound:
            # листа нет — создаём таблицу остатков в том виде, как её ведёт менеджер: размеры в строках, цвета в столбцах
            ws = book.add_worksheet(title=config.STOCK_TAB, rows=20, cols=3)
            ws.update(values=[["Кровати", "Черный (шт.)", "Белый (Шт.)"]] +
                      [[w, 0, 0] for w in sorted({int(c.split(".")[0]) for c in config.STOCK_CODES})],
                      range_name="A1", value_input_option="USER_ENTERED")
        _tabs["stock"] = ws
    return ws


def _stock_layout(values: list) -> tuple:
    """(столбец по коду цвета {'04': 1, '06': 2}, строка по ширине {'120': 2, ...}) — номера с единицы."""
    cols = {"04": 2, "06": 3}  # по умолчанию B — чёрная, C — белая
    for i, h in enumerate(values[0] if values else [], start=1):
        for key, color in _COLOR_BY_HEADER:
            if key in h.lower():
                cols[color] = i
    rows = {}
    for r, v in enumerate(values[1:], start=2):
        if v and v[0].strip().isdigit():
            rows[v[0].strip()] = r
    return cols, rows


def add_stock(code: str, qty: int, who: str = "") -> None:
    """Меняет число в таблице остатков прямо в ячейке (новые строки не добавляются)."""
    width, color = code.split(".")
    ws = _stock_ws()
    values = ws.get_all_values()
    cols, rows = _stock_layout(values)
    row = rows.get(width)
    if row is None:  # такого размера в таблице ещё нет — добавляем строку
        row = len(values) + 1
        ws.update(values=[[int(width)]], range_name=f"A{row}", value_input_option="USER_ENTERED")
    ws.update_cell(row, cols.get(color, 2 if color == "04" else 3), int(qty))


def latest_stock() -> dict:
    """{'120.04': (5, '')} — текущие числа из таблицы остатков. Пустая ячейка = нет данных."""
    values = _stock_ws().get_all_values()
    cols, rows = _stock_layout(values)
    res = {}
    for width, r in rows.items():
        for color, c in cols.items():
            cell = values[r - 1][c - 1].strip() if len(values[r - 1]) >= c else ""
            if cell == "":
                continue
            try:
                res[f"{width}.{color}"] = (int(float(cell.replace(",", ".").replace(" ", ""))), "")
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
