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
    "Добавлен", "Канал", "Тип", "Дата отгрузки", "Дата доставки", "Размер", "Количество",
    "Цвет", "Номер заказа", "Артикул", "Отгрузка / телефон", "Адрес", "Статус",
]
# Поле заказа -> название столбца в шапке. Столбцы ищутся ПО НАЗВАНИЮ, поэтому добавлять/двигать их в таблице можно.
FIELDS = [("added", "Добавлен"), ("channel", "Канал"), ("kind", "Тип"), ("ship", "Дата отгрузки"),
          ("delivery", "Дата доставки"), ("size", "Размер"), ("qty", "Количество"), ("color", "Цвет"),
          ("order_no", "Номер заказа"), ("article", "Артикул"), ("phone", "Отгрузка / телефон"),
          ("address", "Адрес"), ("status", "Статус")]
_layout_cache: dict = {}


def _set_layout(header_row: list) -> dict:
    """{поле: номер столбца с 0}. Не нашли название — берём место из стандартной шапки (для «Количества» — нет столбца)."""
    names = [(h or "").strip().lower() for h in header_row]
    default = [k for k, _ in FIELDS if k != "qty" or "количество" in names]
    lay = {}
    for key, title in FIELDS:
        if title.lower() in names:
            lay[key] = names.index(title.lower())
        elif key in default:  # название в шапке не нашли — берём стандартное место
            lay[key] = default.index(key)
    _layout_cache.clear()
    _layout_cache.update(lay)
    return lay


def layout() -> dict:
    if not _layout_cache:
        _set_layout(orders_ws().row_values(1))
    return _layout_cache


def _col_letter(i: int) -> str:
    return chr(65 + i)
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
    try:
        values = orders_ws().get_all_values()
    except gspread.exceptions.APIError:
        # лист переименовали или переставили, пока бот работал — забываем запомненные листы и находим заново
        _tabs.clear()
        values = orders_ws().get_all_values()
    lay = _set_layout(values[0] if values else [])
    width = max(lay.values(), default=0) + 1

    def cell(v, key):
        i = lay.get(key)
        return v[i].strip() if i is not None and i < len(v) else ""

    rows = []
    for i, v in enumerate(values[1:], start=2):
        v = list(v) + [""] * (width - len(v))
        if not cell(v, "order_no"):
            continue
        try:
            qty = max(1, int(float(cell(v, "qty").replace(",", ".")))) if cell(v, "qty") else 1
        except ValueError:
            qty = 1
        rows.append({
            "row": i, "added": cell(v, "added"), "channel": cell(v, "channel"), "kind": cell(v, "kind"),
            "ship": cell(v, "ship"), "delivery": cell(v, "delivery"), "size": cell(v, "size"), "qty": qty,
            "color": cell(v, "color"), "order_no": cell(v, "order_no"), "article": cell(v, "article"),
            "phone": cell(v, "phone"), "address": cell(v, "address"), "status": cell(v, "status"),
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
                      "startColumnIndex": 0, "endColumnIndex": max(layout().values(), default=len(HEADERS) - 1) + 1},
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
        lay = layout()
        width = max(lay.values(), default=0) + 1
        rows = []
        for o in new:
            vals = {"added": now, "channel": o.channel, "kind": o.kind, "ship": o.ship_date,
                    "delivery": o.delivery_date, "size": o.size, "qty": getattr(o, "qty", 1) or 1,
                    "color": o.color, "order_no": o.order_no, "article": o.article,
                    "phone": o.shipment_or_phone, "address": o.address, "status": ""}
            row = [""] * width
            for key, val in vals.items():
                if key in lay:
                    row[lay[key]] = val
            rows.append(row)
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
        col = _col_letter(layout()["status"])
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


# --------------------------------------------------------------- каркасы
FRAMES_HEADERS = ["Размер", "Количество"]


def frame_widths() -> list:
    return sorted({int(c.split(".")[0]) for c in config.STOCK_CODES})


def _frames_ws():
    """Вкладка со сваренными каркасами. Подходит и «Свареные каркасы», и «Сваренные каркасы»; нет — создаём."""
    ws = _tabs.get("frames")
    if ws is None:
        book = get_book()
        names = [config.FRAMES_TAB, "Свареные каркасы", "Сваренные каркасы", "Каркасы"]
        for n in names:
            try:
                ws = book.worksheet(n)
                break
            except gspread.WorksheetNotFound:
                continue
        if ws is None:
            ws = book.add_worksheet(title=config.FRAMES_TAB, rows=50, cols=2)
            ws.append_row(FRAMES_HEADERS, value_input_option="RAW")
        _tabs["frames"] = ws
    values = ws.get_all_values()
    have = {v[0].strip() for v in values[1:] if v and v[0].strip()}
    missing = [w for w in frame_widths() if str(w) not in have]
    if missing:
        ws.append_rows([[w, 0] for w in missing], value_input_option="USER_ENTERED")
    return ws


def _frames_col(values: list) -> int:
    """Номер столбца (с 0) с количеством: по заголовку «Кол…» (в вашей таблице он с опечаткой), иначе второй."""
    for i, h in enumerate(values[0] if values else []):
        if (h or "").strip().lower().startswith("кол"):
            return i
    return 1


def _num(x) -> int:
    try:
        return int(float(str(x).replace(",", ".").replace(" ", "") or 0))
    except ValueError:
        return 0


def frames_get() -> dict:
    """{ширина: сколько сваренных каркасов}."""
    ws = _frames_ws()
    values = ws.get_all_values()
    col = _frames_col(values)
    res = {w: 0 for w in frame_widths()}
    for v in values[1:]:
        if v and v[0].strip().isdigit():
            res[int(v[0])] = _num(v[col]) if col < len(v) else 0
    return res


def frames_add(width: int, delta: int, who: str = "") -> int:
    """Меняет число каркасов размера width прямо в ячейке (минус — убрать, ниже нуля не уходит). Возвращает новое число."""
    ws = _frames_ws()
    values = ws.get_all_values()
    col = _frames_col(values)
    for i, v in enumerate(values[1:], start=2):
        if v and v[0].strip() == str(width):
            new = max(0, (_num(v[col]) if col < len(v) else 0) + delta)
            ws.update_cell(i, col + 1, new)
            return new
    ws.append_row([width, max(0, delta)], value_input_option="RAW")
    return max(0, delta)


# ------------------------------------------------- склад: сырьё и ТМЦ
INV_HEADERS = ["Название", "Остаток", "Ед.", "Минимум"]


def _inv_ws(kind: str):
    return _tab(config.INV_TABS[kind], INV_HEADERS)


def _inv_cols(header: list) -> dict:
    """Столбцы ищем по названию в шапке (название, остаток/количество, ед., минимум), иначе 1–4 по порядку."""
    cols = {"name": 0, "qty": 1, "unit": 2, "min": 3}
    for i, h in enumerate(header):
        t = (h or "").strip().lower()
        if t.startswith("назв") or t.startswith("позиц") or t.startswith("материал"):
            cols["name"] = i
        elif t.startswith("остат") or t.startswith("кол"):
            cols["qty"] = i
        elif t.startswith("ед"):
            cols["unit"] = i
        elif t.startswith("мин"):
            cols["min"] = i
    return cols


def _fnum(x) -> float:
    try:
        return float(str(x).replace(",", ".").replace(" ", "") or 0)
    except ValueError:
        return 0.0


def inv_get(kind: str) -> list:
    """[{'row', 'name', 'qty', 'unit', 'min'}] — позиции склада из таблицы."""
    values = _inv_ws(kind).get_all_values()
    cols = _inv_cols(values[0] if values else [])
    res = []
    for i, v in enumerate(values[1:], start=2):
        g = lambda k: (v[cols[k]].strip() if cols[k] < len(v) else "")
        if g("name"):
            res.append({"row": i, "name": g("name"), "qty": _fnum(g("qty")), "unit": g("unit"), "min": _fnum(g("min"))})
    return res


def inv_set(kind: str, row: int, qty: float) -> None:
    ws = _inv_ws(kind)
    cols = _inv_cols(ws.row_values(1))
    ws.update_cell(row, cols["qty"] + 1, int(qty) if float(qty).is_integer() else round(qty, 3))


def inv_new(kind: str, name: str, unit: str = "", minimum: float = 0) -> None:
    ws = _inv_ws(kind)
    cols = _inv_cols(ws.row_values(1))
    row = [""] * (max(cols.values()) + 1)
    row[cols["name"]], row[cols["qty"]], row[cols["unit"]] = name, 0, unit
    if minimum:
        row[cols["min"]] = int(minimum) if float(minimum).is_integer() else minimum
    ws.append_row(row, value_input_option="USER_ENTERED")


# --------------------------------------------------------------- возвраты
RETURNS_HEADERS = ["Дата", "Тип возврата", "Размер", "Статус"]
RET_TAKEN = "Принят на склад"
RET_DONE = "Принят на склад"


def _returns_ws():
    return _tab(config.RETURNS_TAB, RETURNS_HEADERS)


def add_returns(items: list, day) -> None:
    """items: [(тип возврата, размер текстом)]. Каждая кровать — строка со статусом «Забрал курьер»."""
    if items:
        d = day.strftime("%d.%m.%Y")
        _returns_ws().append_rows([[d, kind, size, RET_TAKEN] for kind, size in items], value_input_option="RAW")


def pending_returns() -> list:
    """Возвраты, которые курьер забрал, но склад ещё не принял: [{'row', 'date', 'type', 'size'}]."""
    res = []
    for i, v in enumerate(_returns_ws().get_all_values()[1:], start=2):
        v = list(v) + [""] * (4 - len(v))
        if v[2].strip() and v[3].strip() in ("", RET_TAKEN):
            res.append({"row": i, "date": v[0].strip(), "type": v[1].strip(), "size": v[2].strip()})
    return res


def mark_returns_received(rows: list) -> list:
    """Ставит «Принят на склад» тем из указанных строк, которые всё ещё ждут приёмки. Возвращает принятые."""
    wanted = set(rows)
    ok = [r for r in pending_returns() if r["row"] in wanted]
    if ok:
        _returns_ws().batch_update([{"range": f"D{r['row']}", "values": [[RET_DONE]]} for r in ok],
                                   value_input_option="RAW")
    return ok


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
_OLD_NOTES = ("было до запуска", "первый запуск")


def _handled(tab: str) -> set:
    """ID, о которых бот уже сообщал. Записи «было до запуска» не считаются: такие заказы снова сверяются с таблицей."""
    return {v[0].strip() for v in _tab(tab, MARKET_HEADERS).get_all_values()[1:]
            if v and v[0].strip() and (len(v) < 4 or v[3].strip() not in _OLD_NOTES)}


def table_order_ids() -> set:
    """Номера заказов, которые уже есть в таблице (у Озона «123-1#2» считается как «123-1»)."""
    return {r["order_no"].split("#")[0] for r in read_orders(force=True)}


def market_seen() -> set:
    """ID заказов Маркета, о которых бот уже знает."""
    return _handled(config.MARKET_TAB)


def market_add(rows: list) -> None:
    if rows:
        _tab(config.MARKET_TAB, MARKET_HEADERS).append_rows(rows, value_input_option="RAW")


def set_shipment_cell(row: int, value: str) -> None:
    """Вписывает номер отгрузки в столбец «Отгрузка / телефон» уже существующей строки."""
    orders_ws().update(values=[[value]], range_name=f"{_col_letter(layout()['phone'])}{row}", value_input_option="RAW")
    _invalidate()


def set_order_dates(updates: list) -> None:
    """updates: [(номер строки, поле 'ship' или 'delivery', текст)] — меняет даты в таблице заказов (перенос доставки)."""
    if updates:
        lay = layout()
        orders_ws().batch_update([{"range": f"{_col_letter(lay[f])}{r}", "values": [[t]]} for r, f, t in updates],
                                 value_input_option="RAW")
        _invalidate()
        safe_sync_colors()


# ------------------------------------------------------------------ Озон
def oz_seen() -> set:
    return _handled(config.OZON_TAB)


def oz_add(rows: list) -> None:
    if rows:
        _tab(config.OZON_TAB, MARKET_HEADERS).append_rows(rows, value_input_option="RAW")
