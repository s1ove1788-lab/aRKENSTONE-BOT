"""Таблица стоимости доставок (отдельная закрытая Google Таблица). Каждое «Доставлен» курьера = строка в таблице.
Цена: город — 2550 за каждый адрес и +550 за каждую следующую кровать на тот же адрес;
СГТ/ПЭК/ВБ КГТ — 2800 за первую кровать типа за день и +600 за каждую следующую.
Итог дня считает сама таблица формулой."""
import logging

import config
import logic
import sheets

log = logging.getLogger("orders-bot")
TAB = "Доставки"
HEADERS = ["Дата доставки", "Тип", "Адрес", "Цена", "Итог дня", "Номер заказа"]
_state = {"ws": None, "sep": ","}


def enabled() -> bool:
    return bool(config.COST_SHEET_ID)


def delivery_type(o: dict) -> tuple:
    """(тип для таблицы, базовая цена)."""
    ch = (o.get("channel") or "").upper().replace("ЯС", "ЯМ")
    if logic.is_city(o):
        return "Город", config.COST_CITY
    if "WB" in ch or "ВБ" in ch:
        return "ВБ КГТ", config.COST_RUSSIA
    if "ПЭК" in ch:
        return "ПЭК", config.COST_RUSSIA
    if "СГТ" in ch:
        return "ЯМ СГТ", config.COST_RUSSIA
    return (o.get("channel") or "Другое"), config.COST_RUSSIA


def _ws():
    if _state["ws"] is None:
        import gspread
        book = sheets.get_client().open_by_key(config.COST_SHEET_ID)
        try:
            ws = book.worksheet(TAB)
        except gspread.WorksheetNotFound:
            ws = book.add_worksheet(title=TAB, rows=500, cols=len(HEADERS))
        if not ws.row_values(1):
            ws.append_row(HEADERS, value_input_option="RAW")
            try:
                ws.format("A1:F1", {"textFormat": {"bold": True}})
                ws.freeze(rows=1)
            except Exception:
                pass
        _state["ws"] = ws
    return _state["ws"]


def _formula(row: int, sep: str) -> str:
    return f'=IF(A{row}<>A{row + 1}{sep}SUMIF(A:A{sep}A{row}{sep}D:D){sep}"")'


def _write_totals(ws, first: int, last: int) -> None:
    for sep in (_state["sep"], ";" if _state["sep"] == "," else ","):
        ws.batch_update([{"range": f"E{r}", "values": [[_formula(r, sep)]]} for r in range(first, last + 1)],
                        value_input_option="USER_ENTERED")
        shown = str(ws.acell(f"E{last}").value or "")
        if not shown.startswith(("#", "Err", "Ошибка")):
            _state["sep"] = sep
            return
    log.warning("Не получилось записать формулу «Итог дня» в таблицу стоимости доставок")


def record(marked: list, day) -> int:
    """Записывает доставленные заказы (строки таблицы заказов) в таблицу стоимости. Возвращает число строк."""
    if not enabled() or not marked:
        return 0
    ws = _ws()
    date = day.strftime("%d.%m.%Y")
    counts: dict = {}  # (тип, «ключ») -> сколько кроватей уже записано; для города ключ — адрес, для остальных — пусто

    def key(kind: str, address: str, order_no: str) -> tuple:
        if kind != "Город":
            return (kind, "")
        addr = " ".join((address or "").lower().split())
        return (kind, addr or f"№{order_no}")  # адреса нет — считаем отдельной доставкой

    for v in ws.get_all_values()[1:]:
        if len(v) >= 3 and v[0] == date:
            k = key(v[1], v[2], v[5] if len(v) > 5 else "")
            counts[k] = counts.get(k, 0) + 1
    rows = []
    for o in marked:
        kind, base = delivery_type(o)
        k = key(kind, o.get("address") or "", o.get("order_no", ""))
        extra = config.COST_CITY_EXTRA if kind == "Город" else config.COST_EXTRA
        price = base if counts.get(k, 0) == 0 else extra
        counts[k] = counts.get(k, 0) + 1
        rows.append([date, kind, o.get("address") or "", price, "", o.get("order_no", "")])
    resp = ws.append_rows(rows, value_input_option="RAW")
    rng = ((resp or {}).get("updates") or {}).get("updatedRange", "")  # «Доставки!A5:F7»
    try:
        first = int(rng.split("!")[-1].split(":")[0].lstrip("ABCDEFGHIJ"))
        _write_totals(ws, first, first + len(rows) - 1)
    except ValueError:
        log.warning("Не понял, куда записались строки (%s), формулу итога не добавил", rng)
    return len(rows)
