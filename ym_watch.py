"""
Слежение за новыми заказами Яндекс Маркета.
Раз в N минут бот спрашивает Маркет о заказах «в обработке» и сообщает о тех, которых ещё не видел.
Персональные данные покупателей не читаются и в уведомления не попадают.
"""
import logging
from datetime import datetime

import requests

import config
import logic
import sheets

log = logging.getLogger("orders-bot")
BASE = "https://api.partner.market.yandex.ru"
BASELINE = "_baseline_"  # отметка в листе «Маркет»: первый запуск уже был
MONTHS = ["янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]
MAX_IN_MESSAGE = 15


def _get(path: str, params=None):
    return requests.get(BASE + path, headers={"Api-Key": config.YM_TOKEN}, params=params, timeout=30)


# --------------------------------------------------------------- запросы
def campaigns() -> list:
    r = _get("/v2/campaigns")
    r.raise_for_status()
    return r.json().get("campaigns") or []


def fetch_processing(campaign_id: int) -> list:
    """Все заказы кампании в статусе PROCESSING (по умолчанию Маркет отдаёт последние 30 дней)."""
    orders, token, seen_tokens = [], None, set()
    for _ in range(20):  # не больше 20 страниц по 50 заказов
        params = {"status": "PROCESSING", "limit": 50}
        if token:
            params["pageToken"] = token
        r = _get(f"/v2/campaigns/{campaign_id}/orders", params)
        r.raise_for_status()
        data = r.json()
        orders += data.get("orders") or []
        token = (data.get("paging") or {}).get("nextPageToken")
        if not token or token in seen_tokens:
            break
        seen_tokens.add(token)
    return orders


# ------------------------------------------------------------ оформление
def _dm(date_str: str):
    try:
        d, m, _ = date_str.split("-")
        return int(d), int(m)
    except Exception:
        return None


def short_date(date_str: str) -> str:
    """'27-10-2026' -> '27.окт'."""
    p = _dm(date_str or "")
    return f"{p[0]:02d}.{MONTHS[p[1] - 1]}" if p else "—"


def date_range(a: str, b: str) -> str:
    pa, pb = _dm(a or ""), _dm(b or "")
    if not pa:
        return "—"
    if not pb or pa == pb:
        return short_date(a)
    if pa[1] == pb[1]:
        return f"{pa[0]}-{pb[0]} {MONTHS[pa[1] - 1]}"
    return f"{short_date(a)}–{short_date(b)}"


def region_text(delivery: dict) -> str:
    """Как в вашей таблице: «Городской округ Липецк (Липецк)»."""
    reg = (delivery or {}).get("region") or {}
    name = reg.get("name") or ""
    parent = (reg.get("parent") or {}).get("name") or ""
    if parent and name and parent != name:
        return f"{parent} ({name})"
    return name or "—"


def channel_guess(delivery: dict) -> str:
    """Догадка по схеме доставки: через Маркет — «ЯМ СГТ», своя доставка — «ЯМ ГОРОД»."""
    return {"YANDEX_MARKET": "ЯМ СГТ", "SHOP": "ЯМ ГОРОД"}.get((delivery or {}).get("deliveryPartnerType"), "ЯМ")


def format_order(o: dict) -> str:
    d = o.get("delivery") or {}
    items = []
    for it in o.get("items") or []:
        art = it.get("offerId") or it.get("shopSku") or ""
        code = logic.order_code({"article": art, "size": "", "color": ""})
        name = logic.code_name(code) if code else (it.get("offerName") or art or "товар")
        items.append(f"{name} ×{it.get('count', 1)}")
    shipments = d.get("shipments") or []
    ship = short_date(shipments[0].get("shipmentDate")) if shipments else "—"
    dates = d.get("dates") or {}
    return (f"№ {o.get('id')} · {channel_guess(d)}\n"
            f"{', '.join(items) or 'товары не указаны'}\n"
            f"{region_text(d)}\n"
            f"Отгрузка {ship} · доставка {date_range(dates.get('fromDate'), dates.get('toDate'))}")


def _created(o: dict):
    try:
        return datetime.strptime(o.get("creationDate", ""), "%d-%m-%Y %H:%M:%S")
    except ValueError:
        return datetime.min


def build_message(found: list) -> str:
    head = "🛒 Новый заказ на Яндекс Маркете" if len(found) == 1 else f"🛒 Новых заказов на Яндекс Маркете: {len(found)}"
    blocks = [format_order(o) for o in found[:MAX_IN_MESSAGE]]
    if len(found) > MAX_IN_MESSAGE:
        blocks.append(f"…и ещё {len(found) - MAX_IN_MESSAGE}")
    return head + "\n\n" + "\n\n".join(blocks)


# ----------------------------------------------------------------- опрос
def poll(notify) -> dict:
    """Один проход. notify(text) — как разослать уведомление.
    Возвращает {'total': заказов в обработке, 'new': новых, 'baseline': был ли первый запуск, 'sample': пример}."""
    current: dict = {}
    for c in campaigns():
        for o in fetch_processing(c["id"]):
            current[str(o["id"])] = (c["id"], o)

    seen = sheets.market_seen()
    now = datetime.now().strftime("%d.%m.%Y %H:%M")
    sample = ""
    if current:
        sample = format_order(max((o for _, o in current.values()), key=_created))

    if BASELINE not in seen:
        # первый запуск: запоминаем всё, что уже есть, и никого не беспокоим
        rows = [[oid, now, str(cid), "было до запуска"] for oid, (cid, _) in current.items()]
        rows.append([BASELINE, now, "", "первый запуск"])
        sheets.market_add(rows)
        log.info("Яндекс Маркет: первый запуск, запомнил %d заказов без уведомлений", len(current))
        return {"total": len(current), "new": 0, "baseline": True, "sample": sample}

    fresh = sorted(((oid, cid, o) for oid, (cid, o) in current.items() if oid not in seen),
                   key=lambda x: _created(x[2]))
    if fresh:
        sheets.market_add([[oid, now, str(cid), "новый"] for oid, cid, _ in fresh])  # сначала запоминаем
        notify(build_message([o for _, _, o in fresh]))
        log.info("Яндекс Маркет: новых заказов %d", len(fresh))
    return {"total": len(current), "new": len(fresh), "baseline": False, "sample": sample}
