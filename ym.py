def _status_name(status: str, substatus: str = "") -> str:
    """Человекочитаемый статус заказа."""
    statuses = {
        "PROCESSING": "🟡 В обработке",
        "DELIVERY": "🚚 В доставке",
        "DELIVERED": "✅ Доставлен",
        "CANCELLED": "❌ Отменён",
        "UNPAID": "💳 Не оплачен",
        "PENDING": "⏳ Ожидает обработки",
    }

    # Более точные подстатусы
    substatuses = {
        "READY_TO_SHIP": "Готов к отгрузке",
        "STARTED": "Заказ в обработке",
        "DELIVERY_SERVICE_RECEIVED": "Получен службой доставки",
        "DELIVERY_SERVICE_DELIVERED": "Доставлен",
        "USER_CHANGED_MIND": "Покупатель передумал",
    }

    if substatus in substatuses:
        detail = substatuses[substatus]
        emoji = statuses.get(status, "📦").split(" ", 1)[0]
        return f"{emoji} {detail}"

    return statuses.get(status, f"📦 {status}")


def _format_date(date_str: str) -> str:
    """21-10-2026 17:23:12 -> 21.10.2026 17:23"""
    if not date_str:
        return "—"

    try:
        date_part, time_part = date_str.split(" ")
        day, month, year = date_part.split("-")
        return f"{day}.{month}.{year} {time_part[:5]}"
    except Exception:
        return date_str


def _format_money(value) -> str:
    """14563.0 -> 14 563 ₽"""
    try:
        return f"{float(value):,.0f}".replace(",", " ") + " ₽"
    except (TypeError, ValueError):
        return "—"


def _format_order(order: dict, campaign_name: str, model: str) -> str:
    """Красивое представление одного заказа."""

    order_id = order.get("id") or order.get("externalOrderId") or "—"
    status = order.get("status", "")
    substatus = order.get("substatus", "")

    lines = [
        f"📦 Заказ №{order_id}",
        f"Статус: {_status_name(status, substatus)}",
        f"Создан: {_format_date(order.get('creationDate'))}",
    ]

    # Товары
    items = order.get("items") or []

    for item in items:
        name = item.get("offerName") or "Товар"
        sku = item.get("shopSku") or item.get("offerId") or "—"
        count = item.get("count", 1)
        price = item.get("buyerPrice", item.get("price"))

        lines.extend([
            "",
            f"🛏 {name}",
            f"Артикул: {sku}",
            f"Количество: {count} шт.",
            f"💰 Цена: {_format_money(price)}",
        ])

    # Доставка
    delivery = order.get("delivery") or {}

    partner = delivery.get("deliveryPartnerType")
    dates = delivery.get("dates") or {}
    region = delivery.get("region") or {}

    city = region.get("name")

    if partner == "YANDEX_MARKET":
        delivery_name = "Яндекс Маркет"
    elif partner == "SHOP":
        delivery_name = "Магазин"
    else:
        delivery_name = partner or "—"

    lines.extend([
        "",
        f"🚚 Доставка: {delivery_name}",
    ])

    if city:
        lines.append(f"📍 {city}")

    shipment_date = None
    shipments = delivery.get("shipments") or []

    if shipments:
        shipment_date = shipments[0].get("shipmentDate")

    if shipment_date:
        lines.append(f"📅 Отгрузка: {shipment_date}")

    from_date = dates.get("fromDate")
    to_date = dates.get("toDate")

    if from_date:
        if from_date == to_date:
            lines.append(f"📆 Доставка: {from_date}")
        else:
            lines.append(f"📆 Доставка: {from_date} — {to_date}")

    # Для FBS полезно показать сумму заказа
    total = order.get("buyerTotal")

    if total is not None:
        lines.append(f"💵 Итого: {_format_money(total)}")

    return "\n".join(lines)


def _get_orders(campaign_id: int, status: str = None, page_size: int = 20):
    """Получить заказы кампании."""
    params = {
        "pageSize": page_size
    }

    if status:
        params["status"] = status

    r = _get(f"/v2/campaigns/{campaign_id}/orders", params)

    if r.status_code != 200:
        log.error(
            "Ошибка получения заказов кампании %s: %s %s",
            campaign_id,
            r.status_code,
            r.text[:500]
        )
        return None, f"Ошибка API: {r.status_code}"

    try:
        data = r.json()
    except Exception:
        return None, "Маркет вернул некорректный JSON"

    return data.get("orders") or [], None


def discovery() -> list:
    """
    Красивый вывод активных заказов Яндекс Маркета.

    Показывает:
    - кампании;
    - активные заказы;
    - товар;
    - SKU;
    - количество;
    - цену;
    - город;
    - дату отгрузки;
    - статус.

    Персональные данные покупателя не выводятся.
    """

    # Получаем магазины
    r = _get("/v2/campaigns")

    if r.status_code != 200:
        return [
            f"❌ Маркет ответил {r.status_code}: {r.text[:300]}"
        ]

    try:
        camps = r.json().get("campaigns") or []
    except Exception:
        return ["❌ Маркет вернул некорректный JSON"]

    if not camps:
        return [
            "❌ Маркет не вернул ни одной кампании.\n"
            "Проверьте права API-токена."
        ]

    out = []

    out.append("🛒 ЯНДЕКС МАРКЕТ")
    out.append("")

    total_orders = 0
    total_sum = 0

    for campaign in camps:

        campaign_id = campaign.get("id")
        campaign_name = (
            campaign.get("name")
            or campaign.get("domain")
            or f"Кампания {campaign_id}"
        )

        model = campaign.get("placementType") or "—"

        if model == "FBS":
            model_name = "FBS"
        elif model == "DBS":
            model_name = "DBS"
        else:
            model_name = model

        out.append("━━━━━━━━━━━━━━━━━━━━")
        out.append(
            f"🏪 {campaign_name}\n"
            f"Модель: {model_name}"
        )
        out.append("")

        # Получаем все заказы.
        # Потом сами отфильтруем отменённые.
        orders, error = _get_orders(
            campaign_id,
            page_size=20
        )

        if error:
            out.append(f"❌ {error}")
            out.append("")
            continue

        # Убираем отменённые заказы
        active_orders = [
            order for order in orders
            if order.get("status") != "CANCELLED"
        ]

        if not active_orders:
            out.append("📭 Активных заказов нет.")
            out.append("")
            continue

        out.append(
            f"📦 Активных заказов: {len(active_orders)}"
        )
        out.append("")

        campaign_sum = 0

        for order in active_orders:

            out.append(
                _format_order(
                    order,
                    campaign_name,
                    model_name
                )
            )

            out.append("")
            out.append("────────────────────")
            out.append("")

            total_orders += 1

            try:
                total = float(order.get("buyerTotal") or 0)
                campaign_sum += total
                total_sum += total
            except (TypeError, ValueError):
                pass

        out.append(
            f"💰 Сумма заказов кампании: "
            f"{_format_money(campaign_sum)}"
        )
        out.append("")

    out.append("━━━━━━━━━━━━━━━━━━━━")
    out.append("📊 ИТОГО")
    out.append(f"📦 Активных заказов: {total_orders}")
    out.append(f"💰 Сумма заказов: {_format_money(total_sum)}")

    return out
