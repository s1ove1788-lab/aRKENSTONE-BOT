"""
Слежение за новыми заказами Яндекс Маркета.
Раз в N минут бот спрашивает Маркет о заказах «в обработке» и сообщает о тех, которых ещё не видел.
Персональные данные покупателей не читаются и в уведомления не попадают.
"""
import logging
from datetime import datetime, timedelta

import requests

import config
import logic
import sheets
from pdf_parser import Order, COLOR_CODES, parse_article

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
        name = logic.article_name(art, it.get("offerName") or "")
        items.append(f"{name} ×{it.get('count', 1)}")
    shipments = d.get("shipments") or []
    ship = short_date(shipments[0].get("shipmentDate")) if shipments else "—"
    dates = d.get("dates") or {}
    return (f"№ {o.get('id')} · {channel_guess(d)}\n"
            f"{', '.join(items) or 'товары не указаны'}\n"
            f"{region_text(d)}\n"
            f"{'Отгрузка ' + ship + ' · ' if ship != '—' else ''}"
            f"доставка {date_range(dates.get('fromDate'), dates.get('toDate'))}")


def _created(o: dict):
    try:
        return datetime.strptime(o.get("creationDate", ""), "%d-%m-%Y %H:%M:%S")
    except ValueError:
        return datetime.min


# ------------------------------------------------- номера отгрузок и таблица
def _d(date_str: str):
    try:
        return datetime.strptime(date_str, "%d-%m-%Y").date()
    except (TypeError, ValueError):
        return None


def shipment_map(campaign_id: int, date_from, date_to) -> dict:
    """{id заказа: номер отгрузки}. Метод Маркета для схемы FBS. Любая ошибка — пустой ответ (бот не падает)."""
    result: dict = {}
    body = {"dateFrom": date_from.strftime("%d-%m-%Y"), "dateTo": date_to.strftime("%d-%m-%Y")}
    token = None
    try:
        for _ in range(5):
            params = {"page_token": token} if token else None
            r = requests.put(f"{BASE}/v2/campaigns/{campaign_id}/first-mile/shipments",
                             headers={"Api-Key": config.YM_TOKEN}, params=params, json=body, timeout=30)
            if r.status_code != 200:
                log.warning("Маркет не отдал отгрузки (%s): %s", r.status_code, r.text[:300])
                break
            res = r.json().get("result") or {}
            for sh in res.get("shipments") or []:
                for oid in sh.get("orderIds") or []:
                    result[str(oid)] = str(sh.get("id"))
            token = (res.get("paging") or {}).get("nextPageToken")
            if not token:
                break
    except Exception:
        log.exception("Ошибка запроса отгрузок у Маркета")
    return result


def is_sgt(o: dict) -> bool:
    return (o.get("delivery") or {}).get("deliveryPartnerType") == "YANDEX_MARKET"


def is_city(o: dict) -> bool:
    """Доставка силами магазина (ваш «ЯМ ГОРОД»)."""
    return (o.get("delivery") or {}).get("deliveryPartnerType") == "SHOP"


def city_address(addr: dict) -> str:
    """Как пишет менеджер: «Вознесенский проспект, 55, 6 подъезд, 51 домофон, 4 этаж, кв. 51»."""
    addr = addr or {}
    g = lambda k: str(addr.get(k) or "").strip()
    house = g("house")
    if g("block"):
        house += f" к{g('block')}"
    if g("building"):
        house += f" стр {g('building')}"
    parts = []
    if g("city") and g("city").lower() != "санкт-петербург":
        parts.append(g("city"))
    parts += [g("street"), house]
    if g("entrance"):
        parts.append(f"{g('entrance')} подъезд")
    if g("entryphone"):
        parts.append(f"{g('entryphone')} домофон")
    if g("floor"):
        parts.append(f"{g('floor')} этаж")
    if g("apartment"):
        parts.append(f"кв. {g('apartment')}")
    return ", ".join(p for p in parts if p)


_buyer_warned = False


def buyer_phone(campaign_id: int, order_id, addr: dict) -> str:
    """Телефон получателя: из адреса заказа, иначе отдельным запросом о покупателе."""
    phone = str((addr or {}).get("phone") or "").strip()
    if phone:
        return phone
    global _buyer_warned
    try:
        r = _get(f"/v2/campaigns/{campaign_id}/orders/{order_id}/buyer")
        if r.status_code != 200:
            if not _buyer_warned:  # тело ответа не пишем в лог: там могут быть данные клиента
                _buyer_warned = True
                log.warning("Маркет не отдал телефон покупателя (код %s). Проверьте права токена.", r.status_code)
            return ""
        j = r.json()
        data = j.get("result") if isinstance(j.get("result"), dict) else j
        return str(data.get("phone") or "").strip()
    except Exception:
        log.warning("Ошибка запроса телефона покупателя")
        return ""


def to_table_order(o: dict, shipment: str = "", phone: str = ""):
    """Заказ Маркета -> строка таблицы (как у менеджера). None, если автоматически добавлять нельзя."""
    items = o.get("items") or []
    if len(items) != 1 or items[0].get("count", 1) != 1:
        return None  # несколько позиций — вручную
    art = items[0].get("offerId") or items[0].get("shopSku") or ""
    a = parse_article(art)
    if not a:
        return None
    d = o.get("delivery") or {}
    dates = d.get("dates") or {}
    common = dict(
        size=f"{a['width']}×{a['legs']}",
        color=COLOR_CODES.get(a["color_code"], ""),
        order_no=str(o.get("id")),
        article=art,
    )
    if is_sgt(o):
        shipments = d.get("shipments") or []
        return Order(
            kind="картон", channel="ЯМ СГТ", address=region_text(d), shipment_or_phone=shipment,
            ship_date=short_date(shipments[0].get("shipmentDate")) if shipments else "",
            delivery_date=date_range(dates.get("fromDate"), dates.get("toDate")), **common)
    if is_city(o):
        created = (o.get("creationDate") or "").split(" ")[0]
        end = dates.get("toDate") or dates.get("fromDate")
        return Order(
            kind="город", channel="ЯМ ГОРОД", address=city_address(d.get("address")), shipment_or_phone=phone,
            ship_date=date_range(created, end),  # как у менеджера: «27-29 окт» (с даты заказа до даты доставки)
            delivery_date=date_range(dates.get("fromDate"), dates.get("toDate")), **common)
    return None


def build_message(found: list, tags: dict = None, shipments: dict = None) -> str:
    tags, shipments = tags or {}, shipments or {}
    head = "🛒 Новый заказ на Яндекс Маркете" if len(found) == 1 else f"🛒 Новых заказов на Яндекс Маркете: {len(found)}"
    blocks = []
    for o in found[:MAX_IN_MESSAGE]:
        text = format_order(o)
        sh = shipments.get(str(o.get("id")))
        if sh:
            text += f" · отгрузка № {sh}"
        if tags.get(str(o.get("id"))):
            text += "\n" + tags[str(o.get("id"))]
        blocks.append(text)
    if len(found) > MAX_IN_MESSAGE:
        blocks.append(f"…и ещё {len(found) - MAX_IN_MESSAGE}")
    return head + "\n\n" + "\n\n".join(blocks)


def _window(orders: list):
    days = [_d(((o.get("delivery") or {}).get("shipments") or [{}])[0].get("shipmentDate")) for o in orders]
    days = [x for x in days if x]
    today = datetime.now().date()
    return (min(days + [today]) - timedelta(days=1), max(days + [today]) + timedelta(days=1))


def backfill_shipments(current: dict) -> int:
    """Если в таблице у СГТ-заказа нет номера отгрузки (Маркет присваивает его не сразу) — дописываем."""
    empty = {r["order_no"]: r["row"] for r in sheets.read_orders(force=True)
             if r["channel"] == "ЯМ СГТ" and not r["phone"] and r["order_no"] in current}
    if not empty:
        return 0
    by_campaign: dict = {}
    for oid in empty:
        by_campaign.setdefault(current[oid][0], []).append(current[oid][1])
    filled = 0
    for cid, orders in by_campaign.items():
        smap = shipment_map(cid, *_window(orders))
        for o in orders:
            sh = smap.get(str(o["id"]))
            if sh:
                sheets.set_shipment_cell(empty[str(o["id"])], sh)
                filled += 1
    return filled


# ----------------------------------------------------------------- опрос
def poll(notify) -> dict:
    """Один проход. notify(text) — как разослать уведомление.
    Возвращает {'total', 'new', 'added' (записано в таблицу), 'baseline', 'sample'}."""
    current: dict = {}
    for c in campaigns():
        for o in fetch_processing(c["id"]):
            current[str(o["id"])] = (c["id"], o)

    seen = sheets.market_seen()
    now = datetime.now().strftime("%d.%m.%Y %H:%M")
    sample = ""
    if current:
        sample = format_order(max((o for _, o in current.values()), key=_created))

    in_table = sheets.table_order_ids()  # сверка: что есть в Маркете, но ещё не внесено в таблицу
    fresh = sorted(((oid, cid, o) for oid, (cid, o) in current.items() if oid not in seen and oid not in in_table),
                   key=lambda x: _created(x[2]))
    added = 0
    if fresh:
        sheets.market_add([[oid, now, str(cid), "новый"] for oid, cid, _ in fresh])  # сначала запоминаем
        tags, smap = {}, {}
        if config.YM_AUTOADD:
            to_add = []
            by_campaign: dict = {}
            for oid, cid, o in fresh:
                by_campaign.setdefault(cid, []).append(o)
            for cid, orders in by_campaign.items():
                smap.update(shipment_map(cid, *_window(orders)))
            for oid, cid, o in fresh:
                phone = buyer_phone(cid, oid, (o.get("delivery") or {}).get("address")) if is_city(o) else ""
                row = to_table_order(o, smap.get(oid, ""), phone)
                if row:
                    to_add.append(row)
                    if row.channel == "ЯМ ГОРОД" and (not phone or not row.address or "," not in row.address):
                        tags[oid] = "⚠️ адрес или телефон получены не полностью — проверьте строку в таблице"
                else:
                    tags[oid] = "➕ в таблицу не добавлен (несколько позиций или незнакомый артикул), внесите вручную"
            try:
                new_rows = sheets.add_orders(to_add) if to_add else []
                added = len(new_rows)
                added_nos = {x.order_no for x in new_rows}
                for r in to_add:
                    base = "✅ добавлен в таблицу" if r.order_no in added_nos else "уже был в таблице"
                    tags[r.order_no] = (tags[r.order_no] + "\n" + base) if r.order_no in tags else base
            except Exception:
                log.exception("Не удалось записать заказы Маркета в таблицу")
                for r in to_add:
                    tags[r.order_no] = "⚠️ в таблицу записать не удалось, внесите вручную"
        recon = any(_created(o) < datetime.now() - timedelta(hours=3) for _, _, o in fresh)
        head = ("🔄 Сверка с таблицей: нашёл в Маркете заказы, которых в таблице не было.\n\n" if recon else "")
        notify(head + build_message([o for _, _, o in fresh], tags, smap))
        log.info("Яндекс Маркет: новых заказов %d, добавлено в таблицу %d", len(fresh), added)

    if config.YM_AUTOADD:
        try:
            n = backfill_shipments(current)
            if n:
                log.info("Яндекс Маркет: дописал номер отгрузки в %d строк", n)
        except Exception:
            log.exception("Не удалось дописать номера отгрузок")
    return {"total": len(current), "new": len(fresh), "added": added, "baseline": False, "sample": sample}


def diagnose(order_id: str) -> str:
    """Почему заказ не попал в таблицу: что о нём знает Маркет, видел ли его бот и можно ли добавить автоматически.
    Персональные данные не выводятся."""
    order_id = order_id.strip()
    lines = [f"🔎 Заказ № {order_id}"]
    found = None
    for c in campaigns():
        r = _get(f"/v2/campaigns/{c['id']}/orders/{order_id}")
        if r.status_code == 200:
            found = (c["id"], (r.json().get("order") or {}))
            break
        lines.append(f"Кампания {c['id']}: ответ {r.status_code}")
    if not found:
        lines.append("Маркет не отдал этот заказ ни по одной кампании (проверьте номер и права токена).")
        return "\n".join(lines)
    cid, o = found
    d = o.get("delivery") or {}
    lines.append(f"Статус в Маркете: {o.get('status')} / {o.get('substatus') or '—'}")
    lines.append(f"Создан: {o.get('creationDate') or '—'}")
    lines.append(f"Схема доставки: {d.get('deliveryPartnerType') or '—'}")
    items = o.get("items") or []
    lines.append("Товары: " + (", ".join(f"{it.get('offerId') or it.get('shopSku') or '?'} ×{it.get('count', 1)}" for it in items) or "нет"))
    if o.get("status") != "PROCESSING":
        lines.append("➡️ Бот следит только за статусом PROCESSING («в обработке»). В другом статусе он заказ не увидит.")
    seen = sheets.market_seen()
    if order_id in seen:
        why = ""
        for v in sheets._tab(config.MARKET_TAB, sheets.MARKET_HEADERS).get_all_values()[1:]:
            if v and v[0].strip() == order_id:
                why = f" (запись: {' | '.join(x for x in v[1:] if x)})"
        lines.append("Бот этот заказ уже видел" + why)
    else:
        lines.append("Бот этот заказ ещё не видел (на следующей проверке он должен появиться).")
    row = to_table_order(o, "", "")
    if row is None:
        lines.append("➡️ Автоматически добавить нельзя: несколько позиций или незнакомый артикул — только вручную.")
    else:
        lines.append(f"Автодобавление возможно: {row.channel} · {row.size} · {row.color}")
    if not config.YM_AUTOADD:
        lines.append("➡️ Автодобавление выключено (YM_AUTOADD).")
    in_table = any(r["order_no"] == order_id for r in sheets.read_orders(force=True))
    lines.append("В таблице заказов: " + ("есть" if in_table else "нет"))
    return "\n".join(lines)
