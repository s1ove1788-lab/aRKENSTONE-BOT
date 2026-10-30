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


def day_items(orders: list, day: date, today: date) -> list:
    """[(заказ, False)] на конкретный будущий день (только просмотр для курьера). Без хвостов и «непонятных дат»."""
    res = []
    for o in orders:
        if o["status"]:
            continue
        r = key_range(o, today)
        if r and r[0] <= day <= r[1]:
            res.append((o, False))
    return res


def qty(o: dict) -> int:
    """Сколько кроватей в заказе (столбец «Количество»; пусто = 1)."""
    try:
        return max(1, int(o.get("qty") or 1))
    except (TypeError, ValueError):
        return 1


def count_items(orders: list) -> Counter:
    """{'160/30 чёрная': число кроватей} с учётом количества в заказах."""
    c: Counter = Counter()
    for o in orders:
        c[item_line(o)] += qty(o)
    return c


WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]


def day_preview(items: list, day: date, label: str) -> str:
    """Текст «что будет завтра/послезавтра»: по России — отгрузками, город — адресами."""
    head = f"📅 {label}, {day:%d.%m} ({WEEKDAYS[day.weekday()]})"
    ru, city = russia_groups(items), city_items(items)
    if not ru and not city:
        return head + "\n\nДоставок пока нет."
    lines, budget = [head], 38
    if ru:
        lines += ["", "🚚 По России:"]
        for g in ru:
            if budget <= 0:
                break
            title = g["channel"] + (f" №{g['shipment']}" if g["shipment"] else "")
            cnt = count_items(g["orders"])
            lines.append(f"• {title} — {sum(qty(o) for o in g['orders'])} шт.: " + ", ".join(f"{n} ×{c}" for n, c in sorted(cnt.items())))
            budget -= 1
    if city:
        lines += ["", "🏙 Город:"]
        for i, (o, _) in enumerate(city):
            if budget <= 0:
                lines.append(f"…и ещё {len(city) - i}")
                break
            lines.append(f"• {short(o['address'], 60)} — {item_line(o)}" + (f" ×{qty(o)}" if qty(o) > 1 else ""))
            budget -= 1
    return "\n".join(lines)


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
            f"{item_line(o)}" + (f" ×{qty(o)}" if qty(o) > 1 else "") + f" тел.: {fmt_phone(o['phone'])}\n\n"
            f"{o['channel']} · № {o['order_no']}")


def russia_card(g: dict) -> str:
    head = f"{g['channel']}"
    if g["shipment"]:
        head += f" · отгрузка № {g['shipment']}"
    head += f" · {sum(qty(o) for o in g['orders'])} шт."
    lines = [f"• {name} — {cnt} шт." for name, cnt in sorted(count_items(g["orders"]).items())]
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
        buckets.setdefault((sort_key, label), Counter())[item_line(o)] += qty(o)
        total += qty(o)
    if not buckets:
        return f"📋 План производства (на {today:%d.%m})\n\nБудущих доставок нет."
    lines = [f"📋 План производства (на {today:%d.%m})", ""]
    for (_, label), cnt in sorted(buckets.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        lines.append(f"📅 {label} — {sum(cnt.values())} шт.")
        lines += [f"• {name} ×{n}" for name, n in sorted(cnt.items())]
        lines.append("")
    lines.append(f"Всего: {total} шт.")
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


def code_name(code: str, legs: str = "") -> str:
    width, color = code.split(".")
    return f"{width}/{legs or 30} " + {"04": "чёрная", "06": "белая"}.get(color, color)


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
                plan[code] += qty(o)
    return plan


def today_total(orders: list, today: date) -> int:
    """Сколько кроватей по таблице нужно отгрузить сегодня (всего за день)."""
    return sum(ship_plan(orders, today).values())


def today_banner(orders: list, today: date, done: int = 0) -> str:
    """Строка над меню. done — сколько уже отмечено производством как отгруженное (из них вычитаем)."""
    n = max(0, today_total(orders, today) - done)
    return f"❗❗❗ Отгрузок на сегодня: {n} шт." if n else "✅ Отгрузок на сегодня нет"


def _parse_ship_line(line: str) -> dict:
    s = line.lower().replace("ё", "е")
    m = next((m for m in re.finditer(r"(?<!\d)(\d{3})(?!\d)", s) if int(m.group(1)) in _WIDTHS), None)
    if not m:
        return {"line": line, "error": "не нашёл размер (например: 160/30 ч)"}
    width = int(m.group(1))
    # число сразу после ширины — высота ножек (25, 30 или 35), а не количество: «140 35 ч» = 140/35 чёрная
    lm = re.match(r"\s*[/\\xх×*\-]?\s*(25|30|35)(?!\d)", s[m.end():])
    legs = lm.group(1) if lm else ""
    after = s[m.end() + lm.end():] if lm else s[m.end():]
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
    return {"line": line, "code": f"{width}.{color}", "qty": qty, "legs": legs}


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
              "160/30 ч — одна чёрная; 180/35 б 2 — две белых (ножки 25, 30 или 35).",
              "Несколько позиций — каждая с новой строки."]
    return "\n".join(lines)


def article_name(article: str, fallback: str = "") -> str:
    """'01.35.140.200.04' -> '140/35 чёрная' (ширина/высота ножек и цвет из артикула)."""
    a = parse_article(article)
    if not a:
        return fallback or article or "товар"
    color = {"04": "чёрная", "06": "белая"}.get(a["color_code"], a["color_code"])
    return f"{a['width']}/{a['legs']} {color}"


def parse_frames(text: str, widths: list) -> list:
    """Каркасы: «120 8», «120 - 8 шт.», «160 -2» (минус = убрать). Возвращает [(ширина, изменение)] или {'error': ...}."""
    res = []
    for line in re.split(r"[\n;,]+", text or ""):
        line = line.strip()
        if not line:
            continue
        m = re.fullmatch(r"(\d{2,3})\s+[-−](\d{1,3})\s*(?:шт\.?)?", line, re.I)
        sign = -1
        if not m:
            m = re.fullmatch(r"(\d{2,3})\s*(?:[-–—:=]|\s)\s*(\d{1,3})\s*(?:шт\.?)?", line, re.I)
            sign = 1
        if not m:
            return [{"error": f"не понял «{line}» — пишите так: 120 8"}]
        w, n = int(m.group(1)), int(m.group(2))
        if w not in widths:
            return [{"error": f"размера {w} нет. Есть: {', '.join(map(str, widths))}"}]
        res.append((w, sign * n))
    return res or [{"error": "пусто — напишите, например: 120 8"}]


def fmt_qty(x: float) -> str:
    return str(int(x)) if float(x).is_integer() else f"{x:.3f}".rstrip("0").rstrip(".")


def inv_find(items: list, query: str):
    """Ищет позицию склада по названию: точное совпадение, иначе единственное, которое содержит введённое.
    Возвращает (позиция, None) или (None, текст ошибки)."""
    q = " ".join(query.lower().split())
    exact = [i for i in items if i["name"].lower() == q]
    if exact:
        return exact[0], None
    part = [i for i in items if q and q in i["name"].lower()]
    if len(part) == 1:
        return part[0], None
    if len(part) > 1:
        return None, f"«{query}» подходит к нескольким: " + ", ".join(i["name"] for i in part[:6]) + ". Напишите точнее."
    return None, f"Позиции «{query}» нет в таблице. Добавьте её кнопкой «🆕 Новая позиция»."


def parse_inv(text: str, items: list) -> list:
    """«труба 50», «краска 2,5» (по строке на позицию) -> [(позиция, число)] или [{'error': ...}]."""
    res = []
    for line in re.split(r"[\n;]+", text or ""):
        line = line.strip()
        if not line:
            continue
        m = re.fullmatch(r"(.+?)[\s:=-]+(\d+(?:[.,]\d+)?)\s*[а-яa-z.]*", line, re.I)
        if not m:
            return [{"error": f"не понял «{line}» — пишите так: труба 50"}]
        item, err = inv_find(items, m.group(1))
        if err:
            return [{"error": err}]
        res.append((item, float(m.group(2).replace(",", "."))))
    return res or [{"error": "пусто — напишите, например: труба 50"}]
