"""Чистая логика без обращений к сети: даты, списки для курьеров, тексты карточек."""
import re
from collections import Counter
from datetime import date, timedelta

MONTHS = {"янв": 1, "фев": 2, "мар": 3, "апр": 4, "мая": 5, "май": 5, "июн": 6,
          "июл": 7, "авг": 8, "сен": 9, "окт": 10, "ноя": 11, "дек": 12}

_RANGE = re.compile(r"(\d{1,2})\s*[-–—]\s*(\d{1,2})\s*[.\s]?\s*([а-яё]{3,})", re.I)
_NUM = re.compile(r"(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?")
_SINGLE = re.compile(r"(\d{1,2})\s*[.\s]\s*([а-яё]{3,})", re.I)

import config
from pdf_parser import parse_article

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
    """[(заказ, просрочен?)] для курьера.
    По России (СГТ, ПЭК и т.п.): только заказы с датой на сегодня, никаких хвостов с прошлых дней.
    Город: сегодняшние + недоставленные за прошлые дни (с пометкой «просрочено»)."""
    res = []
    for o in orders:
        if o["status"]:
            continue
        city = is_city(o)
        r = key_range(o, today)
        if r is None:
            if city:
                res.append((o, False))  # дату не поняли — лучше показать, чем потерять
        elif r[0] <= today <= r[1]:
            res.append((o, False))
        elif r[1] < today and city:
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


# ------------------------------------------------------------- отгрузка
_WIDTHS = sorted({int(c.split(".")[0]) for c in config.STOCK_CODES})


def order_code(o: dict):
    """Код размера заказа: '160.04' (ширина.цвет) — из артикула, запасной вариант из размера и цвета."""
    a = parse_article(o["article"])
    if a:
        return f"{a['width']}.{a['color_code']}"
    w = re.match(r"\d+", o["size"])
    c = {"чёрный": "04", "черный": "04", "белый": "06"}.get(o["color"].lower())
    return f"{w.group()}.{c}" if w and c else None


def code_name(code: str) -> str:
    width, color = code.split(".")
    return f"{width}/30 " + {"04": "чёрная", "06": "белая"}.get(color, color)


def ship_plan(orders: list, today: date) -> Counter:
    """Что по таблице нужно отгрузить сегодня: {'160.04': 1}."""
    plan: Counter = Counter()
    for o in orders:
        st = o["status"].lower()
        if st and not st.startswith("доставлен"):  # «Отмена» и т.п. не считаем
            continue
        r = key_range(o, today)
        if r and r[0] <= today <= r[1]:
            code = order_code(o)
            if code:
                plan[code] += 1
    return plan


def _parse_ship_line(line: str) -> dict:
    s = line.lower().replace("ё", "е")
    m = next((m for m in re.finditer(r"(?<!\d)(\d{3})(?!\d)", s) if int(m.group(1)) in _WIDTHS), None)
    if not m:
        return {"line": line, "error": "не нашёл размер (например: 160/30 ч)"}
    width = int(m.group(1))
    after = re.sub(r"^\s*[/\\x×*\-]?\s*30(?!\d)", "", s[m.end():], count=1)  # «/30» — высота
    rest = s[:m.start()] + " " + after
    color = None
    for w in re.findall(r"[а-я]+", rest):
        if w.startswith("ч"):
            color = "04"
            break
        if w.startswith("б"):
            color = "06"
            break
    if color is None:
        mc = re.search(r"(?<!\d)0([46])(?!\d)", rest)
        if mc:
            color = "0" + mc.group(1)
            rest = rest[:mc.start()] + " " + rest[mc.end():]
    if color is None:
        return {"line": line, "error": "не понял цвет (ч — чёрная, б — белая)"}
    nums = re.findall(r"\d+", rest)
    qty = int(nums[0]) if nums else 1
    if not 1 <= qty <= 999:
        return {"line": line, "error": "странное количество"}
    return {"line": line, "code": f"{width}.{color}", "qty": qty}


def parse_shipments(text: str) -> list:
    """'160/30 ч' -> код 160.04, 1 шт; '180/30 б 2' -> 180.06, 2 шт. Несколько позиций — по строкам."""
    return [_parse_ship_line(x.strip()) for x in re.split(r"[\n;]+", text or "") if x.strip()]


def _fmt_counts(c: dict) -> str:
    return ", ".join(f"{code_name(k)} ×{v}" for k, v in sorted(c.items()))


def ship_text(today: date, plan: dict, shipped: dict, results=None) -> str:
    lines = [f"📤 Отгрузка на {today:%d.%m}", ""]
    lines.append("К отгрузке: " + (_fmt_counts(plan) if plan else "по таблице на сегодня ничего нет"))
    lines.append("Отгружено: " + (_fmt_counts(shipped) if shipped else "пока ничего"))
    if plan:
        rem = {k: v - shipped.get(k, 0) for k, v in plan.items() if v - shipped.get(k, 0) > 0}
        lines.append("Осталось: " + (_fmt_counts(rem) if rem else "всё отгружено 🎉"))
    if results:
        lines += ["", *results]
    lines += ["", "Пришлите сообщением, что отгрузили:",
              "160/30 ч — одна чёрная; 180/30 б 2 — две белых.",
              "Несколько позиций — каждая с новой строки."]
    return "\n".join(lines)


def article_name(article: str, fallback: str = "") -> str:
    """'01.35.140.200.04' -> '140/35 чёрная' (ширина/высота ножек и цвет из артикула)."""
    a = parse_article(article)
    if not a:
        return fallback or article or "товар"
    color = {"04": "чёрная", "06": "белая"}.get(a["color_code"], a["color_code"])
    return f"{a['width']}/{a['legs']} {color}"
