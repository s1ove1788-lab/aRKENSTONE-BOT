"""
Чтение PDF-таблицы заказов, которую формирует менеджер.

Ожидаемые 9 колонок (слева направо):
 0  номер по порядку
 1  тип доставки (картон / город)
 2  дата отгрузки
 3  дата доставки
 4  размер и цвет текстом (например "160 30 ЧЕРНЫЙ")
 5  номер заказа + артикул (в две строки)
 6  номер отгрузки (у картона) или телефон (у города)
 7  канал (ЯМ СГТ, ЯМ ГОРОД, ОЗ, ПЭК, WB ГОРОД, WB СГТ)
 8  адрес / регион
"""
import re
from dataclasses import dataclass

import pdfplumber

# Внутренние обозначения -> как будем писать в таблицу.
# Сюда можно добавлять новые варианты написания.
CHANNEL_ALIASES = {
    "ЯС СГТ": "ЯМ СГТ",      # опечатка менеджера
    "ЯС ГОРОД": "ЯМ ГОРОД",
    "WB СГТ": "WB СГТ",
    "WB ГОРОД": "WB ГОРОД",
    "ПЭК": "ПЭК",
    "ОЗ": "ОЗ",
}

# Код цвета из артикула (последняя часть, например ...04)
COLOR_CODES = {
    "04": "Чёрный",
    "06": "Белый",
}

# Артикул: что-то вроде DY.30.160.04 или 01.30.140.06
# Артикул: DY.30.160.04 (ножки 30, ширина 160, цвет 04) или 01.35.140.200.04 (ножки 35, ширина 140, длина 200, цвет 04)
ARTICLE_RE = re.compile(r"\b[A-Za-zА-Яа-я0-9]{1,4}\.(\d{1,3})\.(\d{2,3})(?:\.(\d{2,3}))?\.(\d{2})\b")


def parse_article(text: str):
    """-> {'article', 'legs', 'width', 'color_code'} или None."""
    m = ARTICLE_RE.search(text or "")
    if not m:
        return None
    return {"article": m.group(0), "legs": m.group(1), "width": m.group(2), "color_code": m.group(4)}
# Номер заказа: цифры, возможно с дефисами (63457991875 или 40915084-0092-1)
ORDER_RE = re.compile(r"\b\d[\d\-]{5,}\b")


@dataclass
class Order:
    kind: str            # картон / город
    ship_date: str
    delivery_date: str
    size: str            # например 160×30
    color: str
    order_no: str
    article: str
    shipment_or_phone: str
    channel: str
    address: str


def _clean(cell) -> str:
    if cell is None:
        return ""
    return re.sub(r"\s+", " ", str(cell)).strip()


def normalize_channel(raw: str) -> str:
    key = _clean(raw).upper()
    return CHANNEL_ALIASES.get(key, key)


def _parse_row(cells) -> Order | None:
    """Возвращает Order или None, если строка не похожа на заказ."""
    if len(cells) < 9:
        return None

    order_cell = str(cells[5] or "")
    article_m = ARTICLE_RE.search(order_cell)
    # номер заказа ищем в тексте ячейки, убрав из неё артикул
    order_text = order_cell.replace(article_m.group(0), " ") if article_m else order_cell
    order_m = ORDER_RE.search(order_text)
    if not order_m:
        return None  # шапка таблицы, пустая строка и т.п.

    size = ""
    color = ""
    article = ""
    if article_m:
        article = article_m.group(0)
        height, width, color_code = article_m.group(1), article_m.group(2), article_m.group(4)
        size = f"{width}×{height}"
        color = COLOR_CODES.get(color_code, "")
    if not color:
        # запасной вариант: цвет словом из 5-й колонки
        txt = _clean(cells[4]).upper()
        if "ЧЕРН" in txt or "ЧЁРН" in txt:
            color = "Чёрный"
        elif "БЕЛ" in txt:
            color = "Белый"
    if not size:
        nums = re.findall(r"\d+", _clean(cells[4]))
        if len(nums) >= 2:
            size = f"{nums[0]}×{nums[1]}"

    return Order(
        kind=_clean(cells[1]).lower(),
        ship_date=_clean(cells[2]),
        delivery_date=_clean(cells[3]),
        size=size,
        color=color,
        order_no=order_m.group(0),
        article=article,
        shipment_or_phone=_clean(cells[6]),
        channel=normalize_channel(cells[7]),
        address=_clean(cells[8]),
    )


def parse_pdf(path: str) -> list[Order]:
    """Читает все страницы PDF и возвращает список заказов.

    Бросает ValueError, если в файле нет текста (скан/картинка)
    или не нашлось ни одного заказа.
    """
    orders: list[Order] = []
    seen: set[str] = set()
    has_text = False

    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            if (page.extract_text() or "").strip():
                has_text = True
            for table in page.extract_tables():
                for row in table:
                    order = _parse_row(row)
                    if order and order.order_no not in seen:
                        seen.add(order.order_no)
                        orders.append(order)

    if not has_text:
        raise ValueError(
            "В PDF нет текста — похоже, это скан или фотография таблицы. "
            "Нужен PDF, сохранённый прямо из Excel/Word (где текст можно выделить мышкой)."
        )
    if not orders:
        raise ValueError(
            "Не нашёл в PDF ни одного заказа. Проверьте, что формат таблицы не изменился."
        )
    return orders
