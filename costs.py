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
HEADERS = ["Дата доставки", "Тип", "Адрес", "Цена", "Итог дня", "Номер заказа", "Размер", "Дата отгрузки"]
_state = {"ws": None, "sep": ","}


def sheet_id() -> str:
    """Код таблицы: из настройки COST_SHEET_ID, иначе тот, что бот сам создал (хранится на листе «Настройки»)."""
    if config.COST_SHEET_ID:
        return config.COST_SHEET_ID
    try:
        return sheets.get_setting("cost_sheet_id")
    except Exception:
        log.exception("Не смог прочитать настройки")
        return ""


def enabled() -> bool:
    return bool(sheet_id())


def sheet_url() -> str:
    sid = sheet_id()
    return f"https://docs.google.com/spreadsheets/d/{sid}/edit" if sid else ""


def create(email: str) -> str:
    """Создаёт новую закрытую таблицу «Стоимость доставок» и открывает её одному человеку (редактор).
    Возвращает ссылку. Таблица принадлежит сервисному аккаунту бота, больше ни у кого доступа нет."""
    book = sheets.get_client().create("Стоимость доставок")
    book.share(email, perm_type="user", role="writer", notify=True)
    try:
        book.sheet1.update_title(TAB)
    except Exception:
        pass
    sheets.set_setting("cost_sheet_id", book.id)
    _state["ws"] = None
    return f"https://docs.google.com/spreadsheets/d/{book.id}/edit"


def bot_email() -> str:
    """Почта сервисного аккаунта бота — её нужно добавить редактором в таблицу."""
    try:
        return sheets.get_client().http_client.auth.service_account_email
    except Exception:
        try:
            return sheets.get_client().auth.service_account_email
        except Exception:
            return ""


def connect(link: str) -> str:
    """Подключает уже существующую таблицу (ссылка или код). Её создаёт человек сам и даёт боту доступ редактора."""
    import re
    m = re.search(r"/d/([A-Za-z0-9_-]+)", link) or re.fullmatch(r"\s*([A-Za-z0-9_-]{20,})\s*", link)
    if not m:
        raise ValueError("не нашёл код таблицы в ссылке")
    sid = m.group(1)
    book = sheets.get_client().open_by_key(sid)  # упадёт, если у бота нет доступа
    sheets.set_setting("cost_sheet_id", sid)
    _state["ws"] = None
    _ws()  # создаст лист «Доставки» с шапкой
    return f"https://docs.google.com/spreadsheets/d/{book.id}/edit"


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
        book = sheets.get_client().open_by_key(sheet_id())
        try:
            ws = book.worksheet(TAB)
        except gspread.WorksheetNotFound:
            ws = book.add_worksheet(title=TAB, rows=500, cols=len(HEADERS))
        first = ws.row_values(1)
        if first and len(first) < len(HEADERS) and first[:6] == HEADERS[:6]:
            ws.update(range_name="A1:H1", values=[HEADERS], value_input_option="RAW")  # старая таблица: добавляем колонки
        if not first:
            ws.append_row(HEADERS, value_input_option="RAW")
            try:
                ws.format("A1:H1", {"textFormat": {"bold": True}})
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
        rows.append([date, kind, o.get("address") or "", price, "", o.get("order_no", ""),
                     " ".join(x for x in (o.get("size", ""), o.get("color", "")) if x), o.get("ship", "")])
    resp = ws.append_rows(rows, value_input_option="RAW")
    rng = ((resp or {}).get("updates") or {}).get("updatedRange", "")  # «Доставки!A5:F7»
    try:
        first = int(rng.split("!")[-1].split(":")[0].lstrip("ABCDEFGHIJ"))
        _write_totals(ws, first, first + len(rows) - 1)
    except ValueError:
        log.warning("Не понял, куда записались строки (%s), формулу итога не добавил", rng)
    return len(rows)


def record_returns(items: list, day) -> int:
    """items: [(описание кровати, количество)]. Каждая вернувшаяся кровать — строка «Возврат» по COST_RETURN."""
    if not enabled() or not items:
        return 0
    ws = _ws()
    date = day.strftime("%d.%m.%Y")
    rows = []
    for name, qty in items:
        rows += [[date, "Возврат", "", config.COST_RETURN, "", f"возврат: {name}", name, ""] for _ in range(qty)]
    resp = ws.append_rows(rows, value_input_option="RAW")
    rng = ((resp or {}).get("updates") or {}).get("updatedRange", "")
    try:
        first = int(rng.split("!")[-1].split(":")[0].lstrip("ABCDEFGHIJ"))
        _write_totals(ws, first, first + len(rows) - 1)
    except ValueError:
        pass
    return len(rows)


def _money(n: int) -> str:
    return f"{n:,}".replace(",", " ") + " ₽"


def summary(today) -> str:
    """Текст для курьера: сколько заработано сегодня, вчера, за неделю и за месяц."""
    from datetime import datetime, timedelta
    ws = _ws()
    per_day: dict = {}
    today_types: dict = {}
    for v in ws.get_all_values()[1:]:
        if len(v) < 4:
            continue
        try:
            d = datetime.strptime(v[0].strip(), "%d.%m.%Y").date()
            price = int(float(v[3].replace(" ", "").replace(",", ".")))
        except ValueError:
            continue
        per_day[d] = per_day.get(d, 0) + price
        if d == today:
            n, total = today_types.get(v[1], (0, 0))
            today_types[v[1]] = (n + 1, total + price)
    lines = ["💰 Расчёт доставок", "", f"Сегодня ({today:%d.%m}): {_money(per_day.get(today, 0))}"]
    for kind, (n, total) in sorted(today_types.items()):
        lines.append(f"• {kind}: {n} шт. — {_money(total)}")
    yest = today - timedelta(days=1)
    week = sum(v for d, v in per_day.items() if today - timedelta(days=6) <= d <= today)
    month = sum(v for d, v in per_day.items() if (d.year, d.month) == (today.year, today.month))
    lines += ["", f"Вчера: {_money(per_day.get(yest, 0))}", f"За 7 дней: {_money(week)}",
              f"За {today:%m.%Y}: {_money(month)}"]
    return "\n".join(lines)


def backfill(today) -> tuple:
    """Досчитывает стоимость по уже доставленным заказам из таблицы заказов (статус «Доставлен ДД.ММ ЧЧ:ММ»).
    Заказы, которые уже есть в таблице стоимости, пропускает. Возвращает (добавлено, пропущено, дней)."""
    import re
    from datetime import date
    ws = _ws()
    have = {v[5].strip() for v in ws.get_all_values()[1:] if len(v) > 5 and v[5].strip()}
    by_day: dict = {}
    skipped = 0
    for o in sheets.read_orders(force=True):
        m = re.match(r"доставлен\s+(\d{1,2})\.(\d{1,2})(?:\s+(\d{1,2}):(\d{2}))?", (o["status"] or "").lower())
        if not m:
            continue
        if o["order_no"] in have:
            skipped += 1
            continue
        d, mo = int(m.group(1)), int(m.group(2))
        try:
            day = date(today.year, mo, d)
        except ValueError:
            continue
        if day > today:  # декабрьская доставка, а сейчас январь
            day = date(today.year - 1, mo, d)
        t = (int(m.group(3) or 0), int(m.group(4) or 0))
        by_day.setdefault(day, []).append((t, o))
    added = 0
    for day in sorted(by_day):
        marked = [o for _, o in sorted(by_day[day], key=lambda x: (x[0], x[1]["row"]))]
        added += record(marked, day)
    return added, skipped, len(by_day)
