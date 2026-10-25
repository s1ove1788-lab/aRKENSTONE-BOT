"""
Яндекс Маркет API.
Получение и красивый вывод заказов.
Персональные данные покупателей не выводятся.
"""

import logging

import requests

import config


log = logging.getLogger("orders-bot")

BASE = "https://api.partner.market.yandex.ru"


# ============================================================
# API ЯНДЕКС МАРКЕТА
# ============================================================

def _get(path: str, params=None):
    """
    GET-запрос к API Яндекс Маркета.
    """

    return requests.get(
        BASE + path,
        headers={
            "Api-Key": config.YM_TOKEN
        },
        params=params,
        timeout=30
    )


# ============================================================
# ФОРМАТИРОВАНИЕ СТАТУСОВ
# ============================================================

def _status_name(status: str, substatus: str = "") -> str:
    """
    Преобразует технический статус Яндекс Маркета
    в понятный человеку.
    """

    statuses = {
        "PROCESSING": "🟡 В обработке",
        "DELIVERY": "🚚 В доставке",
        "DELIVERED": "✅ Доставлен",
        "CANCELLED": "❌ Отменён",
        "UNPAID": "💳 Не оплачен",
        "PENDING": "⏳ Ожидает обработки",
    }

    substatuses = {
        "READY_TO_SHIP": "Готов к отгрузке",
        "STARTED": "Заказ в обработке",
        "DELIVERY_SERVICE_RECEIVED": "Получен службой доставки",
        "DELIVERY_SERVICE_DELIVERED": "Доставлен",
        "USER_CHANGED_MIND": "Покупатель передумал",
    }

    if substatus in substatuses:
        detail = substatuses[substatus]

        base_status = statuses.get(status, "📦")
        emoji = base_status.split(" ", 1)[0]

        return f"{emoji} {detail}"

    return statuses.get(
        status,
        f"📦 {status or 'Неизвестный статус'}"
    )


# ============================================================
# ФОРМАТИРОВАНИЕ ДАТЫ
# ============================================================

def _format_date(date_str: str) -> str:
    """
    Пример:

    25-10-2026 18:31:49

    превращается в:

    25.10.2026 18:31
    """

    if not date_str:
        return "—"

    try:
        date_part, time_part = date_str.split(" ", 1)

        day, month, year = date_part.split("-")

        return f"{day}.{month}.{year} {time_part[:5]}"

    except Exception:
        return date_str


# ============================================================
# ФОРМАТИРОВАНИЕ ДЕНЕГ
# ============================================================

def _format_money(value) -> str:
    """
    Пример:

    18725

    превращается в:

    18 725 ₽
    """

    try:
        return (
            f"{float(value):,.0f}"
            .replace(",", " ")
            + " ₽"
        )

    except (TypeError, ValueError):
        return "—"


# ============================================================
# ФОРМАТИРОВАНИЕ ОДНОГО ЗАКАЗА
# ============================================================

def _format_order(
    order: dict,
    campaign_name: str,
    model: str
) -> str:
    """
    Красивое представление одного заказа.
    """

    order_id = (
        order.get("id")
        or order.get("externalOrderId")
        or "—"
    )

    status = order.get("status", "")
    substatus = order.get("substatus", "")

    lines = [
        f"📦 Заказ №{order_id}",
        f"Статус: {_status_name(status, substatus)}",
        f"Создан: {_format_date(order.get('creationDate'))}",
    ]

    # --------------------------------------------------------
    # ТОВАРЫ
    # --------------------------------------------------------

    items = order.get("items") or []

    for item in items:

        name = (
            item.get("offerName")
            or item.get("offerId")
            or "Товар"
        )

        sku = (
            item.get("shopSku")
            or item.get("offerId")
            or "—"
        )

        count = item.get("count", 1)

        price = (
            item.get("buyerPrice")
            if item.get("buyerPrice") is not None
            else item.get("price")
        )

        lines.extend(
            [
                "",
                f"🛏 {name}",
                f"Артикул: {sku}",
                f"Количество: {count} шт.",
                f"💰 Цена: {_format_money(price)}",
            ]
        )

    # --------------------------------------------------------
    # ДОСТАВКА
    # --------------------------------------------------------

    delivery = order.get("delivery") or {}

    partner = delivery.get(
        "deliveryPartnerType"
    )

    dates = delivery.get("dates") or {}

    region = delivery.get("region") or {}

    city = region.get("name")

    if partner == "YANDEX_MARKET":
        delivery_name = "Яндекс Маркет"

    elif partner == "SHOP":
        delivery_name = "Магазин"

    else:
        delivery_name = partner or "—"

    lines.extend(
        [
            "",
            f"🚚 Доставка: {delivery_name}",
        ]
    )

    if city:
        lines.append(f"📍 {city}")

    # --------------------------------------------------------
    # ДАТА ОТГРУЗКИ
    # --------------------------------------------------------

    shipments = delivery.get("shipments") or []

    if shipments:

        shipment_date = shipments[0].get(
            "shipmentDate"
        )

        if shipment_date:
            lines.append(
                f"📅 Отгрузка: {shipment_date}"
            )

    # --------------------------------------------------------
    # ДАТА ДОСТАВКИ
    # --------------------------------------------------------

    from_date = dates.get("fromDate")
    to_date = dates.get("toDate")

    if from_date:

        if from_date == to_date:

            lines.append(
                f"📆 Доставка: {from_date}"
            )

        elif to_date:

            lines.append(
                f"📆 Доставка: "
                f"{from_date} — {to_date}"
            )

        else:

            lines.append(
                f"📆 Доставка: {from_date}"
            )

    # --------------------------------------------------------
    # ИТОГОВАЯ СУММА
    # --------------------------------------------------------

    total = order.get("buyerTotal")

    if total is not None:

        lines.append(
            f"💵 Итого: {_format_money(total)}"
        )

    return "\n".join(lines)


# ============================================================
# ПОЛУЧЕНИЕ ЗАКАЗОВ
# ============================================================

def _get_orders(
    campaign_id: int,
    status: str = None,
    page_size: int = 20
):
    """
    Получает заказы конкретной кампании.
    """

    params = {
        "pageSize": page_size
    }

    if status:
        params["status"] = status

    try:

        r = _get(
            f"/v2/campaigns/{campaign_id}/orders",
            params
        )

    except requests.RequestException as e:

        log.exception(
            "Ошибка соединения с Яндекс Маркетом"
        )

        return None, f"Ошибка соединения с API: {e}"

    if r.status_code != 200:

        log.error(
            "Ошибка получения заказов кампании %s: %s %s",
            campaign_id,
            r.status_code,
            r.text[:500]
        )

        return None, (
            f"Ошибка API: {r.status_code}"
        )

    try:

        data = r.json()

    except ValueError:

        return None, (
            "Маркет вернул некорректный JSON"
        )

    return data.get("orders") or [], None


# ============================================================
# ГЛАВНАЯ ФУНКЦИЯ /YM
# ============================================================

def discovery() -> list:
    """
    Красивый вывод активных заказов Яндекс Маркета.

    Показывает:

    - магазины;
    - модель FBS / DBS;
    - активные заказы;
    - товар;
    - SKU;
    - количество;
    - цену;
    - город;
    - дату отгрузки;
    - дату доставки;
    - статус;
    - общую сумму.

    Персональные данные покупателей
    намеренно не выводятся.
    """

    # --------------------------------------------------------
    # ПОЛУЧАЕМ КАМПАНИИ
    # --------------------------------------------------------

    try:

        r = _get("/v2/campaigns")

    except requests.RequestException as e:

        log.exception(
            "Ошибка запроса к Яндекс Маркету"
        )

        return [
            "❌ Не удалось подключиться "
            "к Яндекс Маркету.\n"
            f"Ошибка: {e}"
        ]

    if r.status_code != 200:

        log.error(
            "Яндекс Маркет вернул %s: %s",
            r.status_code,
            r.text[:500]
        )

        return [
            f"❌ Маркет ответил {r.status_code}.\n"
            f"{r.text[:300]}"
        ]

    try:

        data = r.json()

    except ValueError:

        return [
            "❌ Яндекс Маркет вернул "
            "некорректный JSON."
        ]

    camps = data.get("campaigns") or []

    if not camps:

        return [
            "❌ Маркет не вернул ни одной кампании.\n"
            "Проверьте права API-токена."
        ]

    # --------------------------------------------------------
    # ФОРМИРУЕМ ОТВЕТ
    # --------------------------------------------------------

    out = []

    out.append("🛒 ЯНДЕКС МАРКЕТ")
    out.append("")

    total_orders = 0
    total_sum = 0

    # --------------------------------------------------------
    # ОБХОДИМ МАГАЗИНЫ
    # --------------------------------------------------------

    for campaign in camps:

        campaign_id = campaign.get("id")

        campaign_name = (
            campaign.get("name")
            or campaign.get("domain")
            or f"Кампания {campaign_id}"
        )

        model = (
            campaign.get("placementType")
            or "—"
        )

        if model == "FBS":

            model_name = "FBS"

        elif model == "DBS":

            model_name = "DBS"

        else:

            model_name = model

        # ----------------------------------------------------
        # ЗАГОЛОВОК МАГАЗИНА
        # ----------------------------------------------------

        out.append(
            "━━━━━━━━━━━━━━━━━━━━"
        )

        out.append(
            f"🏪 {campaign_name}\n"
            f"Модель: {model_name}"
        )

        out.append("")

        # ----------------------------------------------------
        # ПОЛУЧАЕМ ЗАКАЗЫ
        # ----------------------------------------------------

        orders, error = _get_orders(
            campaign_id,
            page_size=20
        )

        if error:

            out.append(
                f"❌ {error}"
            )

            out.append("")

            continue

        # ----------------------------------------------------
        # УБИРАЕМ ОТМЕНЁННЫЕ
        # ----------------------------------------------------

        active_orders = [
            order
            for order in orders
            if order.get("status") != "CANCELLED"
        ]

        if not active_orders:

            out.append(
                "📭 Активных заказов нет."
            )

            out.append("")

            continue

        # ----------------------------------------------------
        # КОЛИЧЕСТВО
        # ----------------------------------------------------

        out.append(
            f"📦 Активных заказов: "
            f"{len(active_orders)}"
        )

        out.append("")

        campaign_sum = 0

        # ----------------------------------------------------
        # ВЫВОДИМ ЗАКАЗЫ
        # ----------------------------------------------------

        for order in active_orders:

            out.append(
                _format_order(
                    order,
                    campaign_name,
                    model_name
                )
            )

            out.append("")

            out.append(
                "────────────────────"
            )

            out.append("")

            total_orders += 1

            try:

                total = float(
                    order.get("buyerTotal") or 0
                )

                campaign_sum += total
                total_sum += total

            except (TypeError, ValueError):

                pass

        # ----------------------------------------------------
        # СУММА ПО КАМПАНИИ
        # ----------------------------------------------------

        out.append(
            "💰 Сумма заказов кампании: "
            f"{_format_money(campaign_sum)}"
        )

        out.append("")

    # --------------------------------------------------------
    # ОБЩИЙ ИТОГ
    # --------------------------------------------------------

    out.append(
        "━━━━━━━━━━━━━━━━━━━━"
    )

    out.append("📊 ИТОГО")

    out.append(
        f"📦 Активных заказов: "
        f"{total_orders}"
    )

    out.append(
        f"💰 Сумма заказов: "
        f"{_format_money(total_sum)}"
    )

    return out