"""Всё, что связано с Google Таблицей: заказы, пользователи, группы, остатки."""
import base64
import json
import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import gspread

import config
import logic
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

CACHE_TTL = 15  # секунд: чтобы не дёргать Google при каждом нажатии кнопки
READ_TTL = 20  # секунд: сколько помним прочитанный лист (Google разрешает ~60 чтений в минуту на всех)
_book = None

# Методы листа, которые только читают и не сбрасывают запомненное содержимое
_READS = {"get_all_values", "get_all_records", "row_values", "col_values", "find", "findall", "get", "get_values",
          "acell", "cell", "range"}


class _CachedSheet:
    """Обёртка над листом Google: get_all_values() помнит результат READ_TTL секунд, любая запись его сбрасывает.
    Приложение, бот и Алиса читают одни и те же листы — так вместо десятков чтений в минуту выходит несколько.
    Если Google ответил «лимит исчерпан» (429), отдаём последнее прочитанное, чтобы экран не падал."""

    def __init__(self, ws, ttl: float = READ_TTL):
        self._ws = ws
        self._ttl = ttl
        self._vals = None
        self._t = 0.0

    def get_all_values(self, *a, fresh: bool = False, **kw):
        """fresh=True — прочитать из Google прямо сейчас. Так читают перед записью «было + изменение»,
        чтобы не затереть то, что человек только что поправил руками в таблице."""
        if a or kw:
            return self._ws.get_all_values(*a, **kw)
        if not fresh and self._vals is not None and time.time() - self._t < self._ttl:
            return [list(r) for r in self._vals]
        try:
            self._vals = self._ws.get_all_values()
        except gspread.exceptions.APIError as e:
            if self._vals is not None and "429" in str(e) and not fresh:
                log.warning("Google: лимит чтений, беру лист «%s» из памяти", self._ws.title)
                return [list(r) for r in self._vals]
            raise
        self._t = time.time()
        return [list(r) for r in self._vals]

    def forget(self):
        self._vals = None

    def __getattr__(self, name):
        attr = getattr(self._ws, name)
        if not callable(attr) or name in _READS:
            return attr

        def write(*a, **kw):  # запись: сначала делаем, потом забываем старое содержимое
            try:
                return attr(*a, **kw)
            finally:
                self._vals = None
        return write


class _Tabs(dict):
    """Запомненные листы; каждый сразу оборачивается в _CachedSheet."""

    def __setitem__(self, key, ws):
        # заказы не держим в памяти: их правит менеджер, а бот пишет в строки по номерам — нужна свежая таблица
        if not isinstance(ws, _CachedSheet):
            ws = _CachedSheet(ws, 0 if key == "orders" else READ_TTL)
        super().__setitem__(key, ws)


_tabs: dict = _Tabs()
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
        ws = _tabs["orders"]
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
        ws = _tabs[title]
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
    seen: dict = {}
    for i, v in enumerate(values[1:], start=2):
        v = list(v) + [""] * (width - len(v))
        if not cell(v, "order_no"):
            continue
        dkey = tuple(" ".join(cell(v, k).lower().split()) for k in
                     ("order_no", "address", "phone", "size", "color", "article"))
        dup_of = seen.get(dkey)  # строка, на которую похожа эта (тот же заказ, адрес, размер, цвет); только для /dups
        seen.setdefault(dkey, i)
        try:
            qty = max(1, int(float(cell(v, "qty").replace(",", ".")))) if cell(v, "qty") else 1
        except ValueError:
            qty = 1
        raw_status = cell(v, "status")
        if raw_status.lower().startswith("доставляется"):
            raw_status = ""  # «Доставляется» — заказ ещё в работе, для логики бота это то же, что пустой статус
        rows.append({
            "row": i, "added": cell(v, "added"), "channel": cell(v, "channel"), "kind": cell(v, "kind"),
            "ship": cell(v, "ship"), "delivery": cell(v, "delivery"), "size": cell(v, "size"), "qty": qty,
            "color": cell(v, "color"), "order_no": cell(v, "order_no"), "article": cell(v, "article"),
            "phone": cell(v, "phone"), "address": cell(v, "address"), "status": raw_status,
            "has_status": bool(cell(v, "status")),
            "dup_of": dup_of,
        })
    _cache.update(t=now, rows=rows)
    return rows


# ------------------------------------------------------------- цвета строк
GREEN = {"red": 0.0, "green": 1.0, "blue": 0.0}      # доставлен
YELLOW = {"red": 1.0, "green": 1.0, "blue": 0.0}     # ещё не доставлен
BLUE = {"red": 0.6, "green": 0.8, "blue": 1.0}       # городской заказ, ещё не доставлен
RED = {"red": 1.0, "green": 0.3, "blue": 0.3}        # отмена или возврат
WHITE = {"red": 1.0, "green": 1.0, "blue": 1.0}
STATUS_OPEN = "Доставляется"
STATUS_CHOICES = [STATUS_OPEN, "Доставлен", "Отмена", "Возврат"]
_colored: dict = {}
_dropdown_done = {"ok": False}  # номер строки -> (номер заказа, цвет), чтобы не перекрашивать лишний раз


def color_for(status: str, city: bool = False):
    """Цвет строки по столбцу «Статус»: пусто — жёлтый (город — голубой), «Доставлен…» — зелёный,
    «Отмена/Возврат» — красный."""
    st = (status or "").strip().lower().replace("ё", "е")
    if not st:
        return ("blue", BLUE) if city else ("yellow", YELLOW)
    if "отмен" in st or "возврат" in st:
        return "red", RED
    if st.startswith("доставлен"):
        return "green", GREEN
    return None


def sync_colors() -> int:
    """Закрашивает строки заказов по статусу (зелёный / жёлтый / красный). Возвращает, сколько строк перекрашено.
    Работает и для ручных пометок менеджера («Отмена», «Возврат»): вызывается после изменений и раз в 5 минут."""
    ws = orders_ws()
    try:
        ensure_status_dropdown(ws)
    except Exception:
        log.exception("Не смог настроить список в колонке «Статус»")  # цвета всё равно красим
    todo: list = []  # (строка, имя цвета, цвет)
    seen_rows = set()
    for r in read_orders(force=True):
        seen_rows.add(r["row"])
        c = color_for(r["status"], logic.is_city(r))
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


def ensure_status_dropdown(ws) -> None:
    """Делает ячейки колонки «Статус» выпадающим списком (Доставляется / Доставлен / Отмена / Возврат)
    и пишет «Доставляется» в строках, где статус пуст. Другие значения (например «Доставлен 31.10 14:05») разрешены."""
    col = layout().get("status")
    if col is None:
        return
    if not _dropdown_done["ok"]:
        get_book().batch_update({"requests": [{"setDataValidation": {
            "range": {"sheetId": ws.id, "startRowIndex": 1, "endRowIndex": max(ws.row_count, 2),
                      "startColumnIndex": col, "endColumnIndex": col + 1},
            "rule": {"condition": {"type": "ONE_OF_LIST", "values": [{"userEnteredValue": v} for v in STATUS_CHOICES]},
                     "showCustomUi": True, "strict": False}}}]})
        _dropdown_done["ok"] = True
    empty = [r["row"] for r in read_orders() if not r.get("has_status")]
    if empty:
        letter = _col_letter(col)
        ws.batch_update([{"range": f"{letter}{n}", "values": [[STATUS_OPEN]]} for n in empty],
                        value_input_option="RAW")
        _invalidate()


def stamp_manual_delivered() -> list:
    """Менеджер выбрал в списке просто «Доставлен» — дописываем дату и время, как при нажатии у курьера
    (по дате потом считаются доставки и недельный итог). Возвращает отмеченные строки."""
    rows = [r for r in read_orders(force=True) if r["status"].strip().lower().rstrip(".") == "доставлен"]
    if not rows:
        return []
    stamp = "Доставлен " + datetime.now(ZoneInfo(config.TIMEZONE)).strftime("%d.%m %H:%M")
    col = _col_letter(layout()["status"])
    orders_ws().batch_update([{"range": f"{col}{r['row']}", "values": [[stamp]]} for r in rows],
                             value_input_option="RAW")
    _invalidate()
    return rows


def cancel_rows(reasons: dict) -> list:
    """Площадка сообщила, что заказ отменён: у открытых строк (без статуса) пишем «Отмена ДД.ММ».
    reasons: {номер заказа без «#N»: причина}. Возвращает [(строка, причина)] — только то, что отмечено сейчас."""
    if not reasons:
        return []
    hit = [(r, reasons[r["order_no"].split("#")[0]]) for r in read_orders(force=True)
           if not r["status"] and r["order_no"].split("#")[0] in reasons]
    if not hit:
        return []
    stamp = "Отмена " + datetime.now(ZoneInfo(config.TIMEZONE)).strftime("%d.%m")
    col = _col_letter(layout()["status"])
    orders_ws().batch_update([{"range": f"{col}{r['row']}", "values": [[stamp]]} for r, _ in hit],
                             value_input_option="RAW")
    _invalidate()
    safe_sync_colors()
    return hit


_need_last: dict = {"grid": None}


def _block_rows(values: list, col: int, title_row: int) -> tuple:
    """Таблица-блок на листе «Остатки»: {ширина: строка (с 0)}, строка «Итого» (или None). title_row — строка заголовка блока,
    ниже неё шапка (Ширина / Чёрные / Белые), затем строки с шириной в столбце col."""
    rows, r = {}, title_row + 2
    while r < len(values) and col < len(values[r]) and values[r][col].strip().isdigit():
        rows[int(values[r][col].strip())] = r
        r += 1
    total = r if r < len(values) and col < len(values[r]) and values[r][col].strip().lower().startswith("итог") else None
    return rows, total


def need_sync() -> bool:
    """На листе «Остатки», рядом с таблицей остатков, бот ведёт два блока:
    «Нужно …» — сколько кроватей нужно по всем недоставленным и неотменённым заказам (по ширине: чёрные / белые),
    «Не хватает» — нужно минус то, что уже есть в остатках (не меньше нуля). Блоки находит по заголовкам в первых строках
    листа; если их нет — создаёт справа от остатков. Пишет в таблицу только если цифры изменились."""
    ws = _stock_ws()
    values = ws.get_all_values()
    need_col = lack_col = title_row = None
    for r, row in enumerate(values[:3]):
        for c, cell in enumerate(row):
            low = cell.lower()
            if "хватает" in low and lack_col is None:
                lack_col, title_row = c, r
            elif "нужно" in low and need_col is None:
                need_col, title_row = c, r
    widths = frame_widths()
    created = []
    if need_col is None or lack_col is None:
        # блоков нет — рисуем их в том же виде, как у менеджера: заголовок, шапка, ширины, «Итого»
        title_row = 0
        for name, col in (("Нужно на сегодня", 4), ("Не хватает", 8)):
            if (need_col if name.startswith("Нужно") else lack_col) is None:
                block = [[name, "", ""], ["Ширина", "Чёрные", "Белые"]] + [[w, "", ""] for w in widths] + [["Итого", "", ""]]
                ws.update(range_name=f"{_col_letter(col)}1", values=block, value_input_option="USER_ENTERED")
                created.append(name)
                if name.startswith("Нужно"):
                    need_col = col
                else:
                    lack_col = col
        values = ws.get_all_values()
    counts = logic.need_counts(read_orders())
    stock = latest_stock()
    updates, grid = [], []
    for col, kind in ((need_col, "need"), (lack_col, "lack")):
        rows, total = _block_rows(values, col, title_row)
        sums = [0, 0]
        for w, r in sorted(rows.items()):
            for i, color in enumerate(("04", "06")):
                need = counts.get(f"{w}.{color}", 0)
                val = need if kind == "need" else max(0, need - stock.get(f"{w}.{color}", (0, ""))[0])
                sums[i] += val
                grid.append(val)
                updates.append({"range": f"{_col_letter(col + 1 + i)}{r + 1}", "values": [[val]]})
        if total is not None:
            for i in range(2):
                updates.append({"range": f"{_col_letter(col + 1 + i)}{total + 1}", "values": [[sums[i]]]})
                grid.append(sums[i])
    if not updates or (_need_last["grid"] == grid and not created):
        return False
    ws.batch_update(updates, value_input_option="USER_ENTERED")
    _need_last["grid"] = grid
    return True


def safe_need_sync() -> None:
    try:
        need_sync()
    except Exception:
        log.exception("Не смог обновить блоки «Нужно / Не хватает» на листе «Остатки»")


def safe_sync_colors() -> None:
    try:
        sync_colors()
    except Exception:
        log.exception("Не смог закрасить строки таблицы заказов")
    safe_need_sync()


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
                    "phone": o.shipment_or_phone, "address": o.address, "status": STATUS_OPEN}
            row = [""] * width
            for key, val in vals.items():
                if key in lay:
                    row[lay[key]] = val
            rows.append(row)
        # RAW — чтобы длинные номера и телефоны не превращались в числа
        orders_ws().append_rows(rows, value_input_option="RAW")
        _invalidate()
        if not safe_sort_orders():
            safe_sync_colors()
    return new


def fix_shifted_rows() -> int:
    """Чинит строки, которые старая версия бота записала в таблицу ДО добавления столбца «Количество»:
    все столбцы после «Размера» съехали на одну ячейку влево. Возвращает, сколько строк исправлено."""
    ws = orders_ws()
    values = ws.get_all_values()
    lay = _set_layout(values[0] if values else [])
    if "qty" not in lay:
        return 0
    q, last = lay["qty"], lay["address"]
    colors = ("чёрный", "черный", "белый")
    data = []
    for i, v in enumerate(values[1:], start=2):
        v = list(v) + [""] * (last + 2 - len(v))
        if v[q].strip().lower() in colors and v[lay["color"]].strip() and not v[lay["address"]].strip():
            old = [x.strip() for x in v[q:last + 1]]          # цвет, номер, артикул, телефон, адрес
            new = ["1"] + old[:-1]                               # количество = 1, остальное сдвигаем вправо
            for off, val in enumerate(new):
                data.append({"range": f"{_col_letter(q + off)}{i}", "values": [[val]]})
    if data:
        ws.batch_update(data, value_input_option="RAW")
        _invalidate()
        safe_sync_colors()
    return len(data) // max(1, last - q + 1)


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
        ws = _tabs["stock"]
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
    values = ws.get_all_values(fresh=True)
    cols, rows = _stock_layout(values)
    row = rows.get(width)
    if row is None:  # такого размера в таблице ещё нет — добавляем строку
        row = len(values) + 1
        ws.update(values=[[int(width)]], range_name=f"A{row}", value_input_option="USER_ENTERED")
    ws.update_cell(row, cols.get(color, 2 if color == "04" else 3), int(qty))


def latest_stock(fresh: bool = False) -> dict:
    """{'120.04': (5, '')} — текущие числа из таблицы остатков. Пустая ячейка = нет данных.
    fresh=True — перед записью нового остатка (иначе можно затереть ручную правку)."""
    values = _stock_ws().get_all_values(fresh=fresh)
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
        ws = _tabs["frames"]
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


def _legs_start(values: list) -> int:
    """Строка (с 0), где начинается блок «Размер ножки»; нет блока — конец листа."""
    for i, v in enumerate(values):
        if v and v[0].strip().lower().startswith("размер нож"):
            return i
    return len(values)


def frames_get() -> dict:
    """{ширина: сколько сваренных каркасов} — только верхний блок, без ножек."""
    ws = _frames_ws()
    values = ws.get_all_values()
    col = _frames_col(values)
    res = {w: 0 for w in frame_widths()}
    for v in values[1:_legs_start(values)]:
        if v and v[0].strip().isdigit():
            res[int(v[0])] = _num(v[col]) if col < len(v) else 0
    return res


def _legs_cells(values: list) -> dict:
    """Блок «Размер ножки» в любом месте листа -> {высота: (строка, {цвет: столбец})}, номера с единицы.
    Цвет '' — одно количество (сваренные); '04'/'06' — чёрные/белые, если под заголовком есть «Черные»/«Белые»
    (шапка может занимать две строки из-за объединённых ячеек)."""
    for r, row in enumerate(values):
        for c, cell in enumerate(row):
            if not cell.strip().lower().startswith("размер нож"):
                continue
            colors, res, rr = {}, {}, r + 1
            for hr in (r, r + 1, r + 2):  # шапка: ищем «черн»/«бел» в двух строках под заголовком
                if hr < len(values):
                    for cc, h in enumerate(values[hr]):
                        low = h.strip().lower()
                        if cc > c and low.startswith("черн"):
                            colors["04"] = cc + 1
                        elif cc > c and low.startswith("бел"):
                            colors["06"] = cc + 1
            while rr < len(values) and not (values[rr][c].strip().isdigit() if c < len(values[rr]) else False) and rr <= r + 2:
                rr += 1  # пропускаем вторую строку шапки
            while rr < len(values):
                v = values[rr][c].strip() if c < len(values[rr]) else ""
                if not v.isdigit():
                    break
                res[int(v)] = (rr + 1, colors or {"": c + 2})
                rr += 1
            return res
    return {}


def _legs_ws(kind: str):
    """Ножки: сваренные — на листе сваренных каркасов, окрашенные — на листе «Остатки»."""
    return _stock_ws() if kind == "painted" else _frames_ws()


def legs_get(kind: str = "welded") -> dict:
    """Сваренные: {высота: шт.}. Окрашенные по цветам: {высота: {'04': чёрных, '06': белых}}."""
    values = _legs_ws(kind).get_all_values()
    res = {}
    for h, (r, cols) in sorted(_legs_cells(values).items()):
        nums = {color: _num(_cell(values, r, c - 1)) for color, c in cols.items()}
        res[h] = nums[""] if "" in nums else nums
    return res


def legs_add(height: int, delta: int, kind: str = "welded", color: str = "") -> int:
    """Меняет число ножек высоты height (и цвета, если блок по цветам). Ниже нуля не уходит. Возвращает новое число."""
    ws = _legs_ws(kind)
    values = ws.get_all_values(fresh=True)
    cell = _legs_cells(values).get(int(height))
    if not cell:
        raise ValueError(f"нет строки для ножек {height}")
    r, cols = cell
    c = cols.get(color) or cols.get("")
    if not c:
        raise ValueError("выберите цвет ножек")
    new = max(0, _num(_cell(values, r, c - 1)) + delta)
    ws.update_cell(r, c, new)
    return new


def frames_add(width: int, delta: int, who: str = "") -> int:
    """Меняет число каркасов размера width прямо в ячейке (минус — убрать, ниже нуля не уходит). Возвращает новое число."""
    ws = _frames_ws()
    values = ws.get_all_values(fresh=True)
    col = _frames_col(values)
    for i, v in enumerate(values[1:], start=2):
        if v and v[0].strip() == str(width):
            new = max(0, (_num(v[col]) if col < len(v) else 0) + delta)
            ws.update_cell(i, col + 1, new)
            return new
    ws.append_row([width, max(0, delta)], value_input_option="RAW")
    return max(0, delta)


# ------------------------------------------------------------- заготовки
# Лист «Заготовки»: сверху строки размеров (120…200) с деталями, справа общие позиции (ноги 25/30/35, уголки) —
# у них одна объединённая ячейка, число хранится в строке 2. Ниже таблица «На сколько кроватей хватает».
COMMON_START = ("Ноги 25", "Ноги 30", "Ноги 35")


def _blanks_ws():
    ws = _tabs.get("blanks")
    if ws is None:
        ws = get_book().worksheet(config.BLANKS_TAB)  # лист создаёт человек; нет листа — ошибка, функция выключена
        _tabs["blanks"] = ws
        ws = _tabs["blanks"]
    return ws


def _blanks_layout(values: list) -> dict:
    header = values[0] if values else []
    per, common, is_common = {}, {}, False
    for i, h in enumerate(header[1:], start=1):
        name = h.replace("(шт.)", "").replace("(шт)", "").strip()
        if not name:
            continue
        if name.startswith(COMMON_START):
            is_common = True
        if is_common:
            common[name + (" (общие)" if name in per else "")] = i
        else:
            per[name] = i
    sizes, bottom, top = {}, {}, True
    for r, v in enumerate(values[1:], start=2):
        a = (v[0] if v else "").strip()
        if a.lower() == "размер":
            top = False
            continue
        if a.isdigit():
            (sizes if top else bottom)[int(a)] = r
    return {"per": per, "common": common, "sizes": sizes, "bottom": bottom}


def _cell(values: list, r: int, c: int) -> str:
    return values[r - 1][c].strip() if r - 1 < len(values) and c < len(values[r - 1]) else ""


def blanks_get() -> dict:
    """{'per': [детали], 'common': [общие], 'sizes': {ширина: {деталь: число}}, 'common_qty': {общая: число}}."""
    values = _blanks_ws().get_all_values()
    lay = _blanks_layout(values)
    return {
        "per": list(lay["per"]), "common": list(lay["common"]),
        "sizes": {w: {n: _num(_cell(values, r, c)) for n, c in lay["per"].items()} for w, r in sorted(lay["sizes"].items())},
        "common_qty": {n: _num(_cell(values, 2, c)) for n, c in lay["common"].items()},
    }


def blanks_add(changes: list) -> list:
    """changes: [(ширина или None для общих, деталь, изменение)]. Ниже нуля не уходит.
    Возвращает [(ширина, деталь, было, стало)]."""
    ws = _blanks_ws()
    values = ws.get_all_values(fresh=True)
    lay = _blanks_layout(values)
    cur, res, updates = {}, [], []
    for w, name, d in changes:
        if w is None:
            r, c = 2, lay["common"].get(name)
        else:
            r, c = lay["sizes"].get(int(w)), lay["per"].get(name)
        if r is None or c is None:
            raise ValueError(f"нет ячейки для «{name}» {w or ''}")
        before = cur.get((r, c), _num(_cell(values, r, c)))
        after = max(0, before + int(d))
        cur[(r, c)] = after
        res.append((w, name, before, after))
    for (r, c), v in cur.items():
        updates.append({"range": f"{_col_letter(c)}{r}", "values": [[v]]})
    if updates:
        ws.batch_update(updates, value_input_option="RAW")
    return res


BLANKS_MERGE = (("Изголовье", "Изголовье правое", "Изголовье левое"), ("Ноги", "Ноги правая", "Ноги левая"))


def blanks_merge() -> list:
    """Разовая переделка листа «Заготовки»: правое + левое -> одна колонка (изголовье, ноги).
    Остатки складываются, лишняя колонка удаляется только в верхней таблице (нижняя «На сколько хватает» не трогается).
    Если колонки уже объединены — ничего не делает. Возвращает, что объединено."""
    ws = _blanks_ws()
    done = []
    for new, a, b in BLANKS_MERGE:
        values = ws.get_all_values(fresh=True)
        lay = _blanks_layout(values)
        if a not in lay["per"] or b not in lay["per"] or not lay["sizes"]:
            continue
        ca, cb = lay["per"][a], lay["per"][b]
        keep, drop = min(ca, cb), max(ca, cb)
        updates = [{"range": f"{_col_letter(keep)}1", "values": [[f"{new} (шт.)"]]}]
        for w, r in lay["sizes"].items():
            updates.append({"range": f"{_col_letter(keep)}{r}",
                            "values": [[_num(_cell(values, r, ca)) + _num(_cell(values, r, cb))]]})
        ws.batch_update(updates, value_input_option="RAW")
        get_book().batch_update({"requests": [{"deleteRange": {
            "range": {"sheetId": ws.id, "startRowIndex": 0, "endRowIndex": max(lay["sizes"].values()),
                      "startColumnIndex": drop, "endColumnIndex": drop + 1},
            "shiftDimension": "COLUMNS"}}]})
        ws.forget()
        done.append(f"«{a}» + «{b}» → «{new}»")
    if done:
        blanks_refresh()
    return done


def blanks_refresh() -> dict:
    """Пересчитывает таблицу «На сколько кроватей хватает» на листе и возвращает {ширина: (хватает, не хватает)}."""
    ws = _blanks_ws()
    values = ws.get_all_values()
    lay = _blanks_layout(values)
    data = {w: {n: _num(_cell(values, r, c)) for n, c in lay["per"].items()} for w, r in lay["sizes"].items()}
    res = {w: logic.blanks_enough(parts) for w, parts in sorted(data.items())}
    updates = []
    for w, r in lay["bottom"].items():
        if w in res:
            n, text = res[w]
            updates.append({"range": f"B{r}:C{r}", "values": [[n, text or "Всего хватает"]]})
    if updates:
        ws.batch_update(updates, value_input_option="RAW")
    return res


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


def inv_get(kind: str, fresh: bool = False) -> list:
    """[{'row', 'name', 'qty', 'unit', 'min'}] — позиции склада из таблицы."""
    values = _inv_ws(kind).get_all_values(fresh=fresh)
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


def _ship_sort_key(ship: str, delivery: str, today):
    """Ключ сортировки: дата доставки (для диапазона — последний день), затем дата отгрузки; непонятное — в конец."""
    from datetime import date
    d = logic.parse_range(delivery, today)
    r = logic.parse_range(ship, today)
    return (d[1] if d else date.max, d[0] if d else date.max, r[1] if r else date.max)


def sort_orders() -> int:
    """Упорядочивает таблицу заказов по дате доставки (а не по времени добавления). Внутри одной даты порядок сохраняется.
    Пишет в таблицу только если порядок изменился. Возвращает число строк, если пересортировала, иначе 0."""
    ws = orders_ws()
    values = ws.get_all_values(fresh=True)
    if len(values) < 3:
        return 0
    lay = _set_layout(values[0])
    if "ship" not in lay:
        return 0
    width = max(lay.values()) + 1
    body = [list(v) + [""] * (width - len(v)) for v in values[1:]]
    body = [v[:max(width, len(v))] for v in body]
    filled = [v for v in body if any(x.strip() for x in v)]
    today = datetime.now(ZoneInfo(config.TIMEZONE)).date()
    ordered = sorted(filled, key=lambda v: _ship_sort_key(v[lay["ship"]], v[lay["delivery"]] if "delivery" in lay else "", today))
    if ordered == filled:
        return 0
    qi = lay.get("qty")
    out = []
    for v in ordered:
        v = list(v)
        if qi is not None and v[qi].strip().isdigit():
            v[qi] = int(v[qi])  # «Количество» остаётся числом
        out.append(v)
    if ws.get_all_values(fresh=True) != values:
        log.info("Сортировка заказов отложена: таблицу только что поменяли, попробую в следующий раз")
        return 0
    ws.update(range_name=f"A2:{_col_letter(len(out[0]) - 1)}{len(out) + 1}", values=out, value_input_option="RAW")
    _colored.clear()
    _invalidate()
    safe_sync_colors()
    return len(out)


def safe_sort_orders() -> int:
    try:
        return sort_orders()
    except Exception:
        log.exception("Не смог упорядочить таблицу заказов по дате доставки")
        return 0


def fill_cells(updates: list) -> None:
    """updates: [(номер строки, поле, текст)] — вписывает значения в ячейки (только вызывающий решает, что пусто)."""
    if updates:
        lay = layout()
        orders_ws().batch_update([{"range": f"{_col_letter(lay[f])}{r}", "values": [[t]]} for r, f, t in updates],
                                 value_input_option="RAW")
        _invalidate()


def set_order_dates(updates: list) -> None:
    """updates: [(номер строки, поле 'ship' или 'delivery', текст)] — меняет даты в таблице заказов (перенос доставки)."""
    if updates:
        lay = layout()
        orders_ws().batch_update([{"range": f"{_col_letter(lay[f])}{r}", "values": [[t]]} for r, f, t in updates],
                                 value_input_option="RAW")
        _invalidate()
        if not safe_sort_orders():
            safe_sync_colors()


# ------------------------------------------------------------------ Озон
def oz_seen() -> set:
    return _handled(config.OZON_TAB)


def oz_add(rows: list) -> None:
    if rows:
        _tab(config.OZON_TAB, MARKET_HEADERS).append_rows(rows, value_input_option="RAW")
