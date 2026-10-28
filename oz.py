"""
Озон (Seller API), только чтение: разведка структуры данных и слежение за новыми отправлениями.
Персональные данные покупателей не читаются в уведомления и не выводятся в разведке.
Названия методов и полей написаны по документации Озона, но не проверялись на вашем кабинете:
при ошибке /oz покажет ответ Озона, и мы поправим запрос.
"""
import json
import logging
from datetime import datetime, timedelta, timezone

import requests

import config
import logic
import sheets
from pdf_parser import COLOR_CODES, Order, parse_article
from zoneinfo import ZoneInfo

log = logging.getLogger("orders-bot")
BASE = "https://api-seller.ozon.ru"
BASELINE = "_baseline_"
MONTHS = ["янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]
MAX_IN_MESSAGE = 15
MONTHS_GEN = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
              "сентября", "октября", "ноября", "декабря"]
PII_WHOLE = {"addressee", "customer"}  # заменяются целиком
PII_KEYS = {"phone", "address_tail", "comment", "zip_code", "latitude", "longitude", "email", "name_"}


def _post(path: str, body: dict):
    return requests.post(BASE + path, headers={"Client-Id": config.OZ_CLIENT_ID, "Api-Key": config.OZ_API_KEY},
                         json=body, timeout=40)


def mask(obj, key: str = ""):
    if key.lower() in PII_WHOLE and obj not in (None, "", {}, []):
        return "***"
    if isinstance(obj, dict):
        return {k: mask(v, k) for k, v in obj.items()}
    if isinstance(obj, list):
        return [mask(v, key) for v in obj]
    if key.lower() in PII_KEYS and obj not in (None, ""):
        return "***"
    return obj


# --------------------------------------------------------------- запросы
ACTIVE_STATUSES = {"awaiting_registration", "acceptance_in_progress", "awaiting_approve",
                   "awaiting_packaging", "awaiting_deliver"}  # ещё не отгружены


def fetch_unfulfilled(limit: int = 100, max_pages: int = 10, only_active: bool = True) -> list:
    """Отправления FBS/rFBS за последние 30 дней и ближайшие 30. Метод /v4/posting/fbs/list с курсором
    (v3 Озон отключил). Фильтр по статусу в запросе Озон игнорирует, поэтому активные (ещё не отгруженные)
    отбираем сами. Бросает исключение при ошибке."""
    now = datetime.now(timezone.utc)
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    out, seen, cursor = [], set(), ""
    for _ in range(max_pages):
        body = {"sort_dir": "ASC", "limit": limit, "cursor": cursor,
                "filter": {"since": (now - timedelta(days=30)).strftime(fmt),
                           "to": (now + timedelta(days=30)).strftime(fmt)},
                "with": {"analytics_data": True}}
        r = _post("/v4/posting/fbs/list", body)
        if r.status_code != 200:
            raise RuntimeError(f"Озон ответил {r.status_code}: {r.text[:300]}")
        data = r.json()
        res = data.get("result") if isinstance(data.get("result"), dict) else data
        part = res.get("postings") or []
        for p in part:
            num = str(p.get("posting_number"))
            if num in seen or (only_active and p.get("status") not in ACTIVE_STATUSES):
                continue
            seen.add(num)
            out.append(p)
        cursor = res.get("cursor") or ""
        if not res.get("has_next") or not cursor or not part:
            break
    return out


# ------------------------------------------------------------ оформление
def _date(s: str) -> str:
    """'2026-10-25T21:00:00Z' -> '26.окт' (по московскому времени, как в таблице)."""
    d = _local(s)
    return f"{d.day:02d}.{MONTHS[d.month - 1]}" if d else "—"


def region_text(p: dict) -> str:
    a = p.get("analytics_data") or {}
    region, city = (a.get("region") or "").strip(), (a.get("city") or "").strip()
    if region and city and region != city:
        return f"{region} ({city})"
    return region or city or "—"


def channel_guess(p: dict) -> str:
    """Перевозчик ПЭК (по данным Озона) → «ПЭК», иначе доставка по городу своя → «ОЗ»."""
    dm = p.get("delivery_method") or {}
    a = p.get("analytics_data") or {}
    prov = f"{dm.get('tpl_provider') or ''} {a.get('tpl_provider') or ''} {dm.get('name') or ''}".lower()
    return "ПЭК" if "пэк" in prov else "ОЗ"


def format_posting(p: dict) -> str:
    items = []
    for it in p.get("products") or []:
        name = logic.article_name(it.get("offer_id") or "", it.get("name") or "")
        items.append(f"{name} ×{it.get('quantity', 1)}")
    method = ((p.get("delivery_method") or {}).get("name") or "").strip()
    return (f"№ {p.get('posting_number')} · {channel_guess(p)}\n"
            f"{', '.join(items) or 'товары не указаны'}\n"
            f"{region_text(p)}\n"
            f"Отгрузка до {_date(p.get('shipment_date'))}" + (f" · {method}" if method else ""))


def _local(s: str):
    """'2026-10-09T21:00:00Z' -> дата по времени таблицы (Москва). None, если не разобрать."""
    try:
        d = datetime.strptime((s or "")[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        return d.astimezone(ZoneInfo(config.TIMEZONE)).date()
    except ValueError:
        return None


def _delivery_text(p: dict) -> str:
    """Как у менеджера: «7-8 ноября»."""
    a = p.get("analytics_data") or {}
    d1, d2 = _local(a.get("delivery_date_begin")), _local(a.get("delivery_date_end"))
    if not d1:
        return "—"
    if not d2 or d2 == d1:
        return f"{d1.day} {MONTHS_GEN[d1.month - 1]}"
    if d1.month == d2.month:
        return f"{d1.day}-{d2.day} {MONTHS_GEN[d1.month - 1]}"
    return f"{d1.day} {MONTHS_GEN[d1.month - 1]} – {d2.day} {MONTHS_GEN[d2.month - 1]}"


def to_table_order(p: dict):
    """Отправление Озона -> строка таблицы (формат менеджера: «ОЗ ПЭК», тип «пэк», номер отгрузки = трек ПЭК).
    None — если автоматически добавлять нельзя (городской заказ, несколько позиций, незнакомый артикул)."""
    if channel_guess(p) != "ПЭК":
        return None
    items = p.get("products") or []
    if len(items) != 1 or int(items[0].get("quantity") or 1) != 1:
        return None
    art = items[0].get("offer_id") or ""
    a = parse_article(art)
    if not a:
        return None
    ship = _local(p.get("shipment_date"))
    return Order(
        kind="пэк", channel="ОЗ ПЭК",
        ship_date=f"{ship.day:02d}.{MONTHS[ship.month - 1]}" if ship else "—",
        delivery_date=_delivery_text(p),
        size=f"{a['width']}×{a['legs']}", color=COLOR_CODES.get(a["color_code"], ""),
        order_no=str(p.get("posting_number")), article=art,
        shipment_or_phone=str(p.get("tracking_number") or ""), address="")


def build_message(found: list, tags: dict = None) -> str:
    tags = tags or {}
    head = "🛒 Новый заказ на Озоне" if len(found) == 1 else f"🛒 Новых заказов на Озоне: {len(found)}"
    blocks = []
    for p in found[:MAX_IN_MESSAGE]:
        t = format_posting(p)
        if tags.get(str(p.get("posting_number"))):
            t += "\n" + tags[str(p.get("posting_number"))]
        blocks.append(t)
    if len(found) > MAX_IN_MESSAGE:
        blocks.append(f"…и ещё {len(found) - MAX_IN_MESSAGE}")
    return head + "\n\n" + "\n\n".join(blocks)


# ----------------------------------------------------------------- разведка
def discovery() -> list:
    """Сообщения для /oz: сколько отправлений, по статусам, и по два примера с закрытыми персональными данными."""
    try:
        postings = fetch_unfulfilled(only_active=False)
        postings.sort(key=lambda p: p.get("status") not in ACTIVE_STATUSES)
    except Exception as e:
        log.warning("Разведка Озона: %s", e)
        return [f"⚠️ {e}"]
    if not postings:
        return ["Озон ответил без ошибок, но отправлений за этот период нет."]
    by_status: dict = {}
    for p in postings:
        by_status[p.get("status")] = by_status.get(p.get("status"), 0) + 1
    out = ["Озон: отправлений за период — %d (ещё не отгружены: %d)\n%s" % (
        len(postings), sum(1 for p in postings if p.get("status") in ACTIVE_STATUSES), "\n".join(f"• {s}: {n}" for s, n in sorted(by_status.items(), key=lambda x: str(x[0]))))]
    text = json.dumps(mask(postings[:2]), ensure_ascii=False, indent=1)
    out.append("Примеры (имена, телефоны и адреса скрыты):\n" + (text if len(text) < 3300 else text[:3300] + "\n… (обрезано)"))
    return out


# ----------------------------------------------------------------- опрос
def poll(notify) -> dict:
    current = {str(p["posting_number"]): p for p in fetch_unfulfilled() if p.get("posting_number")}
    seen = sheets.oz_seen()
    now = datetime.now().strftime("%d.%m.%Y %H:%M")
    sample = format_posting(max(current.values(), key=lambda p: p.get("in_process_at") or "")) if current else ""
    if BASELINE not in seen:
        rows = [[pn, now, "", "было до запуска"] for pn in current] + [[BASELINE, now, "", "первый запуск"]]
        sheets.oz_add(rows)
        log.info("Озон: первый запуск, запомнил %d отправлений без уведомлений", len(current))
        return {"total": len(current), "new": 0, "baseline": True, "sample": sample}
    fresh = sorted((p for pn, p in current.items() if pn not in seen), key=lambda p: p.get("in_process_at") or "")
    if fresh:
        sheets.oz_add([[p["posting_number"], now, "", "новый"] for p in fresh])  # сначала запоминаем
        tags, added = {}, 0
        if config.YM_AUTOADD:
            to_add = []
            for p in fresh:
                row = to_table_order(p)
                pn = str(p["posting_number"])
                if row:
                    to_add.append(row)
                else:
                    tags[pn] = "➕ в таблицу не добавлен (городской заказ, несколько позиций или незнакомый артикул), внесите вручную"
            try:
                new_rows = sheets.add_orders(to_add) if to_add else []
                added = len(new_rows)
                added_nos = {x.order_no for x in new_rows}
                for r in to_add:
                    tags[r.order_no] = ("✅ добавлен в таблицу, адрес впишите вручную (Озон его не отдаёт)"
                                        if r.order_no in added_nos else "уже был в таблице")
            except Exception:
                log.exception("Не удалось записать заказы Озона в таблицу")
                for r in to_add:
                    tags[r.order_no] = "⚠️ в таблицу записать не удалось, внесите вручную"
        notify(build_message(fresh, tags))
        log.info("Озон: новых отправлений %d, добавлено в таблицу %d", len(fresh), added)
    return {"total": len(current), "new": len(fresh), "baseline": False, "sample": sample}
