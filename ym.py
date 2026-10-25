"""Яндекс Маркет (API партнёра), только чтение. Пока — «разведка»: показать структуру данных."""
import json
import logging

import requests

import config

log = logging.getLogger("orders-bot")
BASE = "https://api.partner.market.yandex.ru"

# Поля с персональными данными: значения заменяем на ***, остаётся только название поля
PII_KEYS = {
    "phone", "lastname", "firstname", "middlename", "recipient", "street", "house", "apartment",
    "entrance", "entryphone", "intercom", "floor", "block", "building", "postcode", "email",
    "comment", "notes", "subway", "gps", "lat", "lon", "latitude", "longitude", "address",
}


def mask(obj, key: str = ""):
    if isinstance(obj, dict):
        return {k: mask(v, k) for k, v in obj.items()}
    if isinstance(obj, list):
        return [mask(v, key) for v in obj]
    if key.lower() in PII_KEYS and obj not in (None, ""):
        return "***"
    return obj


def _get(path: str, params=None):
    return requests.get(BASE + path, headers={"Api-Key": config.YM_TOKEN},
                        params=params, timeout=30)


def _short(obj, limit: int = 3300) -> str:
    text = json.dumps(mask(obj), ensure_ascii=False, indent=1)
    return text if len(text) <= limit else text[:limit] + "\n… (обрезано)"


def discovery() -> list:
    """Список сообщений для чата: кампании и по паре заказов с замаскированными данными."""
    r = _get("/v2/campaigns")
    if r.status_code != 200:
        return [f"Маркет ответил {r.status_code}: {r.text[:300]}"]
    camps = r.json().get("campaigns") or []
    if not camps:
        return ["Маркет не вернул ни одной кампании (магазина). Проверьте права токена."]
    lines = ["Магазины (кампании) на Маркете:"]
    for c in camps:
        lines.append(f"• id {c.get('id')} · {c.get('domain') or c.get('name') or '—'} · "
                     f"модель {c.get('placementType')} · кабинет {(c.get('business') or {}).get('id')}")
    out = ["\n".join(lines)]
    for c in camps[:3]:
        for label, params in (("в обработке", {"status": "PROCESSING", "pageSize": 2}),
                              ("любые", {"pageSize": 2})):
            r = _get(f"/v2/campaigns/{c['id']}/orders", params)
            if r.status_code != 200:
                out.append(f"Заказы кампании {c['id']} ({label}): ответ {r.status_code}\n{r.text[:400]}")
                continue
            orders = (r.json().get("orders") or [])[:2]
            if not orders:
                out.append(f"Заказы кампании {c['id']} ({label}): пусто")
                continue
            out.append(f"Заказы кампании {c['id']} ({label}), данные покупателя скрыты:\n" + _short(orders))
    return out
