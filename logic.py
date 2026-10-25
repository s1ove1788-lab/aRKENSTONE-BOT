"""Чистая логика без обращений к сети: даты, списки для курьеров, тексты карточек."""
import re
from collections import Counter
from datetime import date, timedelta

MONTHS = {"янв": 1, "фев": 2, "мар": 3, "апр": 4, "мая": 5, "май": 5, "июн": 6,
          "июл": 7, "авг": 8, "сен": 9, "окт": 10, "ноя": 11, "дек": 12}

_RANGE = re.compile(r"(\d{1,2})\s*[-–—]\s*(\d{1,2})\s*[.\s]?\s*([а-яё]{3,})", re.I)
_NUM = re.compile(r"(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?")
_SINGLE = re.compile(r"(\d{1,2})\s*[.\s]\s*([а-яё]{3,})", re.I)

CITY_CHANNELS = {"ЯМ ГОРОД", "WB ГОРОД", "ОЗ"}


# ------------------------------------------------------------------ даты
def _mk(day: int, month: int, today: date, year=None):
    y = year or today.year
    try:
        d = date(y, month, day)
        if year is None:  # года в таблице нет — берём ближайший подходящий
            if d < today - timedelta(days=180):
                d = date(y + 1, month, day)
            elif d > today + timedelta(days=180):
                d = date(y - 1, month, day)
        return d
    except ValueError:
        return None


def parse_range(text: str, today: date):
    """'26.окт' -> (26 окт, 26 окт); '22-26 окт' -> (22 окт, 26 окт); непонятное -> None."""
    t = (text or "").strip().lower()
    m = _RANGE.search(t)
    if m and MONTHS.get(m.group(3)[:3]):
        month = MONTHS[m.group(3)[:3]]
        a, b = _mk(int(m.group(1)), month, today), _mk(int(m.group(2)), month, today)
        if a and b:
            return (a, b) if a <= b else (b, a)
    m = _NUM.search(t)
    if m:
        year = int(m.group(3)) if m.group(3) else None
        if year is not None and year < 100:
            year += 2000
        d = _mk(int(m.group(1)), int(m.group(2)), today, year)
        if d:
            return (d, d)
    m = _SINGLE.search(t)
    if m and MONTHS.get(m.group(2)[:3]):
        d = _mk(int(m.group(1)), MONTHS[m.group(2)[:3]], today)
        if d:
            return (d, d)
    return None


def is_city(o: dict) -> bool:
    ch = o["channel"].upper()
    return ch in CITY_CHANNELS or "ГОРОД" in ch or o["kind"].lower() == "город"


def key_text(o: dict) -> str:
    """Дата, по которой работает курьер/производство.
    Картон (по России): дата отгрузки. Город: дата доставки."""
    first, second = (o["delivery"], o["ship"]) if is_city(o) else (o["ship"], o["delivery"])
    return first if parse_range(first, date(2000, 1, 1)) or not second else second


def key_range(o: dict, today: date):
    return parse_range(key_text(o), today)


# ------------------------------------------------------------- выборки
def courier_items(orders: list, today: date) -> list:
    """[(заказ, просрочен?)] — то, что курьеру нужно сделать сегодня + недоставленное за прошлые дни."""
    res = []
    for o in orders:
        if o["status"]:
            continue
        r = key_range(o, today)
        if r is None:
            res.append((o, False))  # дату не поняли — лучше показать, чем потерять
        elif r[0] <= today <= r[1]:
            res.append((o, False))
        elif r[1] < today:
            res.append((o, True))
    return res


def russia_groups(items: list) -> list:
    groups: dict = {}
    for o, late in items:
        if is_city(o):
            continue
        key = f"{o['channel']}|{o['phone']}"
        g = groups.setdefault(key, {"key": key, "channel": o["channel"], "shipment": o["phone"],
                                    "orders": [], "late": False})
        g["orders"].append(o)
        g["late"] = g["late"] or late
    return sorted(groups.values(), key=lambda g: (not g["late"], g["channel"], g["shipment"]))


def city_items(items: list) -> list:
    res = [(o, late) for o, late in items if is_city(o)]
    return sorted(res, key=lambda it: (not it[1], it[0]["address"]))


# ------------------------------------------------------------- тексты
def fmt_size(o: dict) -> str:
    return o["size"].replace("×", "/")


def fmt_color(c: str) -> str:
    return {"чёрный": "чёрная", "черный": "чёрная", "белый": "белая"}.get(c.lower(), c.lower())


def fmt_phone(p: str) -> str:
    """'89211111111 доб. 8727' -> '89211111111, 8727'."""
    return re.sub(r"\s*доб\.?\s*", ", ", p, flags=re.I).strip()


def item_line(o: dict) -> str:
    return f"{fmt_size(o)} {fmt_color(o['color'])}".strip()


def short(text: str, n: int = 55) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"


def city_card(o: dict) -> str:
    return (f"📍 {o['address']}\n\n"
            f"{item_line(o)} тел.: {fmt_phone(o['phone'])}\n\n"
            f"{o['channel']} · № {o['order_no']}")


def russia_card(g: dict) -> str:
    head = f"{g['channel']}"
    if g["shipment"]:
        head += f" · отгрузка № {g['shipment']}"
    head += f" · {len(g['orders'])} шт."
    lines = [f"• {name} — {cnt} шт." for name, cnt in sorted(Counter(item_line(o) for o in g["orders"]).items())]
    return head + "\n\n" + "\n".join(lines)


def production_plan(orders: list, today: date) -> str:
    buckets: dict = {}
    total = 0
    for o in orders:
        if o["status"]:
            continue
        r = key_range(o, today)
        if r is not None and r[1] < today:
            continue  # прошло и не доставлено — это забота курьеров, в план не включаем
        sort_key = r[0] if r else date.max
        label = key_text(o) or "дата не указана"
        buckets.setdefault((sort_key, label), Counter())[item_line(o)] += 1
        total += 1
    if not buckets:
        return f"📋 План производства (на {today:%d.%m})\n\nБудущих доставок нет."
    lines = [f"📋 План производства (на {today:%d.%m})", ""]
    for (_, label), cnt in sorted(buckets.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        parts = ", ".join(f"{name} ×{n}" for name, n in sorted(cnt.items()))
        lines.append(f"{label} — {parts}")
    lines += ["", f"Всего: {total} шт."]
    return "\n".join(lines)
