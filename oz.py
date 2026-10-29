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


def _hide_values(obj):
    """Оставляет только структуру (названия полей), а все значения заменяет на ***."""
    if isinstance(obj, dict):
        return {k: _hide_values(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_hide_values(v) for v in obj]
    return None if obj in (None, "") else "***"


def mask(obj, key: str = ""):
    if key.lower() in PII_WHOLE and obj not in (None, "", {}, []):
        return _hide_values(obj)  # покупатель: видны названия полей, значений нет
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


def fetch_posting(posting_number: str):
    """Подробности одного отправления (/v3/posting/fbs/get) — в списке телефон покупателя может быть пустым,
    а здесь он иногда есть. None при любой ошибке (это только дополнительная попытка)."""
    try:
        r = _post("/v3/posting/fbs/get", {"posting_number": posting_number,
                                          "with": {"analytics_data": True, "financial_data": False}})
        if r.status_code != 200:
            log.info("Озон: /v3/posting/fbs/get вернул %s: %s", r.status_code, r.text[:200])
            return None
        return r.json().get("result") or None
    except requests.RequestException:
        log.exception("Озон: не получилось запросить подробности отправления")
        return None


def _find_phone(p: dict) -> str:
    c = p.get("customer") or {}
    a = p.get("addressee") or {}
    for v in (c.get("phone"), (c.get("address") or {}).get("phone"), a.get("phone")):
        if v:
            return str(v).strip()
    return ""


def enrich(p: dict) -> dict:
    """Для городского заказа без телефона в списке пробуем взять его из подробностей отправления."""
    if channel_guess(p) == "ПЭК" or _find_phone(p):
        return p
    full = fetch_posting(str(p.get("posting_number")))
    if full:
        phone = _find_phone(full)
        if phone:
            p = dict(p)
            p["customer"] = dict(p.get("customer") or {}, phone=phone)
    return p


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


def _customer(p: dict) -> tuple:
    """(адрес, телефон) покупателя из отправления; пустые строки, если Озон их не отдал.
    В уведомления эти данные не попадают — только в таблицу."""
    c = p.get("customer") or {}
    addr = c.get("address") or {}
    city = (addr.get("city") or "").strip()
    region = (addr.get("region") or "").strip()
    tail = (addr.get("address_tail") or "").strip()
    head = city or region
    address = ", ".join(x for x in (head, tail) if x)
    phone = _find_phone(p)
    return address, phone


def _range_text(d1, d2) -> str:
    """Как у менеджера: «30.окт», «28-30 окт», «30.окт–02.ноя»."""
    def one(d):
        return f"{d.day:02d}.{MONTHS[d.month - 1]}"
    if not d1:
        return "—"
    if not d2 or d1 == d2:
        return one(d1)
    if d1.month == d2.month:
        return f"{d1.day}-{d2.day} {MONTHS[d1.month - 1]}"
    return f"{one(d1)}–{one(d2)}"


def _city_dates(p: dict) -> tuple:
    """Для городского заказа: (дата отгрузки «от дня заказа до дня доставки», дата доставки)."""
    a = p.get("analytics_data") or {}
    d1, d2 = _local(a.get("delivery_date_begin")), _local(a.get("delivery_date_end"))
    ship = _local(p.get("shipment_date"))
    start = _local(p.get("in_process_at")) or ship
    end = d2 or d1 or ship
    return _range_text(start, end), _range_text(d1 or ship, d2 if d1 else None)


def to_table_orders(p: dict) -> list:
    """Отправление Озона -> строки таблицы (по одной на каждую кровать; номер заказа второй и следующих — «№…#2», «#3»).
    ПЭК: канал «ОЗ ПЭК», тип «пэк», номер отгрузки = трек ПЭК, адрес менеджер вписывает сам.
    Город (своя доставка): канал «ОЗ», тип «город», адрес и телефон покупателя берём у Озона.
    Пустой список — если автоматически добавлять нельзя (незнакомый артикул)."""
    beds = []  # (артикул, разобранный артикул)
    for it in p.get("products") or []:
        art = it.get("offer_id") or ""
        a = parse_article(art)
        if not a:
            return []
        beds += [(art, a)] * max(1, int(it.get("quantity") or 1))
    if not beds or len(beds) > 20:
        return []
    pn = str(p.get("posting_number"))
    pek = channel_guess(p) == "ПЭК"
    if pek:
        ship = _local(p.get("shipment_date"))
        ship_text = f"{ship.day:02d}.{MONTHS[ship.month - 1]}" if ship else "—"
        delivery = _delivery_text(p)
        phone, address = str(p.get("tracking_number") or ""), ""
    else:
        address, phone = _customer(p)
        address = address or region_text(p)
        ship_text, delivery = _city_dates(p)
    rows = []
    for i, (art, a) in enumerate(beds):
        rows.append(Order(
            kind="пэк" if pek else "город", channel="ОЗ ПЭК" if pek else "ОЗ",
            ship_date=ship_text, delivery_date=delivery,
            size=f"{a['width']}×{a['legs']}", color=COLOR_CODES.get(a["color_code"], ""),
            order_no=pn if i == 0 else f"{pn}#{i + 1}", article=art,
            shipment_or_phone=phone, address=address))
    return rows


def backfill() -> tuple:
    """Дописывает в таблицу активные заказы Озона, которых там ещё нет (например, пришли до включения автозаписи).
    Возвращает (найдено активных, добавлено, не удалось добавить автоматически)."""
    active = fetch_unfulfilled()
    rows, manual = [], 0
    for p in active:
        got = to_table_orders(enrich(p))
        if got:
            rows += got
        else:
            manual += 1
    added = sheets.add_orders(rows) if rows else []
    return len(active), len(added), manual


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
STATUS_RU = {
    "awaiting_registration": "ждёт регистрации", "acceptance_in_progress": "идёт приёмка",
    "awaiting_approve": "ждёт подтверждения", "awaiting_packaging": "ждёт сборки",
    "awaiting_deliver": "ждёт отгрузки", "delivering": "в пути", "driver_pickup": "у водителя",
    "delivered": "доставлен", "cancelled": "отменён", "not_accepted": "не принят",
    "sent_by_seller": "отправлен", "arbitration": "арбитраж", "client_arbitration": "спор с покупателем",
}


def _short(p: dict) -> str:
    items = ", ".join(
        f"{logic.article_name(it.get('offer_id') or '', it.get('name') or '')} ×{it.get('quantity', 1)}"
        for it in (p.get("products") or [])) or "товары не указаны"
    st = STATUS_RU.get(p.get("status"), p.get("status") or "?")
    return (f"• № {p.get('posting_number')} · {channel_guess(p)} · {st}\n"
            f"  {items}\n  {region_text(p)} · отгрузка {_date(p.get('shipment_date'))}")


def _fields_report(title: str, p: dict) -> str:
    """Строка вида «В списке заказов: имя ✓ · адрес ✓ · телефон ✗» (сами данные не показываются)."""
    c = p.get("customer") or {}
    addr = c.get("address") or {}
    name = bool(c.get("name") or (p.get("addressee") or {}).get("name"))
    address = bool(addr.get("address_tail") or addr.get("city"))
    phone = bool(_find_phone(p))
    mark = lambda ok: "✓" if ok else "✗"
    return f"{title}: имя {mark(name)} · адрес {mark(address)} · телефон {mark(phone)}"


def discovery(mode: str = "") -> list:
    """Сообщение для /oz: коротко и читаемо. «/oz raw» — ещё и компактная проверка, какие данные отдаёт Озон;
    «/oz json» — полные «сырые» данные (для разработчика)."""
    try:
        postings = fetch_unfulfilled(only_active=False)
    except Exception as e:
        log.warning("Разведка Озона: %s", e)
        return [f"⚠️ {e}"]
    if not postings:
        return ["Озон ответил без ошибок, но отправлений за этот период нет."]
    by_status: dict = {}
    for p in postings:
        by_status[p.get("status")] = by_status.get(p.get("status"), 0) + 1
    active = [p for p in postings if p.get("status") in ACTIVE_STATUSES]
    lines = [f"📦 Озон за последние 30 дней: {len(postings)} отправлений",
             f"Ждут отгрузки: {len(active)}"]
    lines += [f"{STATUS_RU.get(s, s)}: {n}" for s, n in sorted(by_status.items(), key=lambda x: str(x[0]))
              if s not in ACTIVE_STATUSES]
    shown = sorted(active, key=lambda p: p.get("in_process_at") or "", reverse=True)[:5] or \
        sorted(postings, key=lambda p: p.get("in_process_at") or "", reverse=True)[:3]
    lines += ["", "Ждущие отгрузки:" if active else "Последние заказы (ждущих отгрузки сейчас нет):"]
    lines += [_short(p) for p in shown]
    row = next((r[0] for r in (to_table_orders(enrich(p)) for p in shown[:1]) if r), None)
    if row:
        lines += ["", "Так заказ запишется в таблицу:",
                  f"{row.channel} · {row.kind} · отгрузка {row.ship_date} · доставка {row.delivery_date}\n"
                  f"{row.size} {row.color} · {row.order_no} · {row.article}" +
                  (f" · трек {row.shipment_or_phone}" if row.kind == "пэк"
                   else f" · адрес {'✓' if row.address else '✗'} · телефон {'✓' if row.shipment_or_phone else '✗'}")]
    out = ["\n".join(lines)]
    if mode in ("raw", "json"):
        first = sorted(postings, key=lambda p: p.get("status") not in ACTIVE_STATUSES)[0]
        full = fetch_posting(str(first.get("posting_number"))) if channel_guess(first) != "ПЭК" else None
        out[0] += "\n\n🔎 Что отдаёт Озон по покупателю (первый заказ):\n" + _fields_report("В списке заказов", first)
        if full:
            out[0] += "\n" + _fields_report("В подробностях заказа", full)
        elif channel_guess(first) != "ПЭК":
            out[0] += "\nПодробности заказа получить не удалось (смотрите лог бота)."
    if mode == "json":
        text = json.dumps(mask(sorted(postings, key=lambda p: p.get("status") not in ACTIVE_STATUSES)[:2]),
                          ensure_ascii=False, indent=1)
        out.append("Сырые данные (значения покупателя скрыты):\n" + (text if len(text) < 3300 else text[:3300] + "\n… (обрезано)"))
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
            to_add, by_posting = [], {}
            for p in fresh:
                got = to_table_orders(enrich(p))
                pn = str(p["posting_number"])
                if got:
                    to_add += got
                    by_posting[pn] = got
                else:
                    tags[pn] = "➕ в таблицу не добавлен (незнакомый артикул или слишком много позиций), внесите вручную"
            try:
                new_rows = sheets.add_orders(to_add) if to_add else []
                added = len(new_rows)
                added_nos = {x.order_no for x in new_rows}
                for pn, got in by_posting.items():
                    first = got[0]
                    if first.order_no not in added_nos:
                        tags[pn] = "уже был в таблице"
                    elif first.channel == "ОЗ":
                        miss = [x for x, ok in (("телефон", bool(first.shipment_or_phone)),
                                                ("адрес", "," in (first.address or ""))) if not ok]
                        tags[pn] = "✅ добавлен в таблицу" + (f" ({len(got)} шт.)" if len(got) > 1 else "") + (
                            f"\n⚠️ Озон не отдал: {', '.join(miss)} — впишите в таблицу вручную" if miss else "")
                    else:
                        tags[pn] = "✅ добавлен в таблицу, адрес впишите вручную (Озон его не отдаёт)"
            except Exception:
                log.exception("Не удалось записать заказы Озона в таблицу")
                for pn in by_posting:
                    tags[pn] = "⚠️ в таблицу записать не удалось, внесите вручную"
        notify(build_message(fresh, tags))
        log.info("Озон: новых отправлений %d, добавлено в таблицу %d", len(fresh), added)
    return {"total": len(current), "new": len(fresh), "baseline": False, "sample": sample}
