"""
Бот заказов для MAX.
Роли: менеджер (грузит PDF, видит таблицы), курьер (доставки на сегодня),
производство (план и остатки). Данные хранятся в Google Таблице.
"""
import json
import logging
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import config
import logic
try:  # необязательные модули: если файла нет в репозитории, бот просто работает без этой функции
    import ai
except ImportError:
    ai = None
try:
    import costs
except ImportError:
    costs = None
import max_client as mx
import sheets
import ym
import ym_watch
import oz
from max_client import cb, link
from pdf_parser import parse_article, parse_pdf

if ai is None:
    class ai:  # заглушка: ИИ-помощник выключен
        reset = staticmethod(lambda uid: None)
    config.AI_KEY = ""
if costs is None:
    class costs:  # заглушка: таблица стоимости доставок выключена
        record = staticmethod(lambda marked, day: 0)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
log = logging.getLogger("orders-bot")

users: dict = {}    # id -> {"name": ..., "role": "manager" | "courier" | "prod"}
groups: list = []   # id групповых чатов, куда слать уведомления
pending: dict = {}  # id -> ("stock", код): чего бот ждёт от человека текстом
screens: dict = {}  # id -> id сообщения-«экрана» с меню, которое бот правит на месте
RELOAD_EVERY = 300  # раз в 5 минут перечитываем список пользователей из таблицы


def now_date():
    return datetime.now(ZoneInfo(config.TIMEZONE)).date()


# -------------------------------------------------------------- контекст
class Ctx:
    """Куда и как отвечать человеку. Бот держит одно «экранное» сообщение и правит его на месте,
    чтобы чат не засорялся. fresh=True — прислать экран новым сообщением (внизу чата)."""

    def __init__(self, uid: int, callback_id=None):
        self.uid = uid
        self.cid = callback_id
        self.target = {"type": "user", "id": uid}
        self.fresh = False

    def show(self, text: str, rows=None, notification=None) -> None:
        old = screens.get(self.uid)
        if self.cid:
            if mx.answer(self.cid, text=text, rows=rows, notification=notification):
                return
        elif old and not self.fresh and config.CLEAN_CHAT and mx.edit(old, text, rows, self.target):
            return
        new = mx.send(self.target, text, rows)
        if new:
            screens[self.uid] = new
            if old and old != new and config.CLEAN_CHAT:
                mx.delete(old, self.target)  # старое меню убираем, чтобы не копились

    def toast(self, text: str) -> None:
        if self.cid:
            mx.answer(self.cid, notification=text)


# ----------------------------------------------------------------- экраны
CHANGE_ROLE = [cb("🔁 Сменить роль", "role:change")]
MENU_BTN = [cb("🏠 Меню", "menu")]


def allowed(uid: int, role: str) -> bool:
    """Роль доступна только тем, чей ID записан в настройках (MANAGER_IDS / COURIER_IDS / PROD_IDS)."""
    ids = {"manager": config.MANAGER_IDS, "courier": config.COURIER_IDS, "prod": config.PROD_IDS}.get(role)
    return ids is not None and uid in ids


def known(uid) -> bool:
    """Есть ли человек хотя бы в одном списке доступа (менеджеры, курьеры, производство)."""
    return bool(uid) and (uid in config.MANAGER_IDS or uid in config.COURIER_IDS or uid in config.PROD_IDS)


def no_access_text(uid) -> str:
    return (f"⛔ Это закрытый бот компании. У вас нет доступа.\n"
            f"Ваш ID: {uid} — если вам нужен доступ, передайте его администратору.")


def deny(ctx: Ctx, key: str) -> None:
    ctx.show(f"Роль «{config.ROLE_LABELS.get(key, key)}» выдаёт администратор бота.\n"
             f"Ваш ID: {ctx.uid} — передайте его администратору.\n\nПока можно выбрать другую роль, "
             "если она вам доступна:", role_rows(ctx.uid), "Нет доступа")


def role_rows(uid=None) -> list:
    """Кнопки ролей; если известен человек — только те, что ему доступны."""
    rows = [(("manager", "👔 Менеджер")), ("courier", "🚚 Курьер"), ("prod", "🏭 Отдел производства")]
    return [[cb(label, f"role:{key}")] for key, label in rows if uid is None or allowed(uid, key)]


def role_choice(ctx: Ctx, intro: str = "", notification=None) -> None:
    text = (intro + "\n\n" if intro else "") + "Кто вы? Выберите роль:"
    ctx.show(text, role_rows(ctx.uid), notification)


def home(ctx: Ctx, notification=None) -> None:
    user = users.get(ctx.uid)
    if user and not allowed(ctx.uid, user["role"]):
        users.pop(ctx.uid, None)  # доступ убрали из настроек — роль больше не действует
        return deny(ctx, user["role"])
    if not user:
        return role_choice(ctx, notification=notification)
    role = user["role"]
    if role == "manager":
        rows = []
        try:
            rows.append([link("📊 Таблица заказов", sheets.sheet_url("orders"))])
        except Exception:
            log.exception("Не смог получить ссылку на таблицу заказов")
        rows.append([cb("📦 Таблица остатков", "p:cur")])
        rows.append(CHANGE_ROLE)
        text = ("👔 Меню менеджера\n\nЧтобы добавить заказы, просто пришлите мне PDF-файл в этот чат.\n"
                "Таблицы открываются в Google Таблицах — там их можно редактировать.")
    elif role == "courier":
        rows = [[cb("🚚 Доставки", "c:home")], CHANGE_ROLE]
        text = "🚚 Меню курьера"
    else:
        rows = [[cb("📋 План производства", "p:plan")],
                [cb("📦 Остатки", "p:stock")], CHANGE_ROLE]
        text = "🏭 Меню производства"
    if config.AI_KEY and role in config.AI_ROLES:
        rows.insert(len(rows) - 1, [cb("💬 ИИ-помощник", "ai:start")])  # перед «Сменить роль»
    ctx.show(text, rows, notification)


# ----------------------------------------------------------------- ИИ-помощник
AI_STOP = [[cb("⬅️ Закончить разговор", "ai:end")]]


def ai_start(ctx: Ctx) -> None:
    ai.reset(ctx.uid)
    pending[ctx.uid] = ("ai", None)
    ctx.show("💬 ИИ-помощник\n\nПросто напишите вопрос обычным текстом, например:\n"
             "• что везём завтра по России?\n"
             "• сколько заказов 160 чёрных в работе?\n"
             "• каких остатков не хватает под заказы?\n\n"
             "Я только читаю таблицу и ничего не меняю.", AI_STOP)


def ai_end(ctx: Ctx) -> None:
    ai.reset(ctx.uid)
    pending.pop(ctx.uid, None)
    home(ctx)


def ai_answer(ctx: Ctx, question: str) -> None:
    ctx.show("⏳ Думаю…", AI_STOP)
    try:
        answer = ai.ask(ctx.uid, question, sheets.read_orders(force=True), sheets.latest_stock(), now_date())
    except RuntimeError as e:
        return ctx.show(f"⚠️ {e}", AI_STOP)
    except Exception:
        log.exception("Ошибка ИИ-помощника")
        return ctx.show("⚠️ Что-то пошло не так, попробуйте ещё раз.", AI_STOP)
    ctx.show(answer, AI_STOP)


def choose_role(ctx: Ctx, name: str, key: str) -> None:
    if key == "change":
        return role_choice(ctx)
    if key not in config.ROLE_LABELS:
        return
    if not allowed(ctx.uid, key):
        return deny(ctx, key)
    users[ctx.uid] = {"name": name, "role": key}
    pending.pop(ctx.uid, None)
    try:
        sheets.save_user(ctx.uid, name, key)
    except Exception:
        log.exception("Не смог записать пользователя в таблицу")
    home(ctx, notification=f"Роль: {config.ROLE_LABELS[key]}")


# ----------------------------------------------------------------- курьер
def _items(today):
    return logic.courier_items(sheets.read_orders(), today)


def courier_home(ctx: Ctx, today, notification=None) -> None:
    items = _items(today)
    orders = sheets.read_orders()
    ru = [it for it in items if not logic.is_city(it[0])]
    city = [it for it in items if logic.is_city(it[0])]
    rows = []
    if ru:
        rows.append([cb(f"По России: {len(ru)} шт.", "c:ru")])
    if city:
        rows.append([cb(f"Город: {len(city)} шт.", "c:city")])
    for n, label in ((1, "Завтра"), (2, "Послезавтра")):
        cnt = len(logic.day_items(orders, today + timedelta(days=n), today))
        rows.append([cb(f"📅 {label}" + (f": {cnt} шт." if cnt else ""), f"c:d:{n}")])
    rows += [[cb("🔄 Обновить", "c:home")], MENU_BTN]
    if not items:
        return ctx.show(f"На сегодня ({today:%d.%m}) доставок нет 🎉", rows, notification)
    ctx.show(f"🚚 Доставки на {today:%d.%m}", rows, notification)


def courier_day(ctx: Ctx, today, n: int) -> None:
    day = today + timedelta(days=n)
    label = "Завтра" if n == 1 else "Послезавтра"
    text = logic.day_preview(logic.day_items(sheets.read_orders(), day, today), day, label)
    other = (2, "Послезавтра") if n == 1 else (1, "Завтра")
    ctx.show(text, [[cb(f"📅 {other[1]}", f"c:d:{other[0]}")], [cb("⬅️ Назад", "c:home")]])


def courier_ru(ctx: Ctx, today, notification=None) -> None:
    groups_ = logic.russia_groups(_items(today))
    if not groups_:
        return courier_home(ctx, today, notification)
    rows = []
    for g in groups_[:28]:
        no = f" №{g['shipment']}" if g["shipment"] else ""
        rows.append([cb(f"{'⏰ ' if g['late'] else ''}{g['channel']}{no} · {len(g['orders'])} шт.",
                        f"c:g:{g['key']}")])
    rows.append([cb("⬅️ Назад", "c:home")])
    text = "🚚 По России — выберите отгрузку:"
    if any(g["late"] for g in groups_):
        text += "\n⏰ — не доставлено в прошлые дни"
    ctx.show(text, rows, notification)


def courier_city(ctx: Ctx, today, notification=None) -> None:
    items = logic.city_items(_items(today))
    if not items:
        return courier_home(ctx, today, notification)
    rows = [[cb(f"{'⏰ ' if late else ''}{logic.short(o['address'])}", f"c:o:{o['order_no']}")]
            for o, late in items[:28]]
    rows.append([cb("⬅️ Назад", "c:home")])
    text = "🏙 Город — выберите адрес:"
    if any(late for _, late in items):
        text += "\n⏰ — не доставлено в прошлые дни"
    ctx.show(text, rows, notification)


def _decision_rows(kind: str, ident: str, back: str) -> list:
    return [[cb("✅ Доставлен сегодня", f"c:done:{kind}:{ident}")],
            [cb("❌ Не доставлен", f"c:fail:{kind}:{ident}")],
            [cb("⬅️ К списку", back)]]


def courier(ctx: Ctx, payload: str) -> None:
    today = now_date()
    parts = payload.split(":", 3)
    action = parts[1] if len(parts) > 1 else ""
    if action == "home":
        return courier_home(ctx, today)
    if action == "d" and len(parts) >= 3 and parts[2] in ("1", "2"):
        return courier_day(ctx, today, int(parts[2]))
    if action == "ru":
        return courier_ru(ctx, today)
    if action == "city":
        return courier_city(ctx, today)

    if action in ("g", "o") and len(parts) >= 3:
        kind, ident = action, payload.split(":", 2)[2]
    elif action in ("done", "fail") and len(parts) == 4:
        kind, ident = parts[2], parts[3]
    else:
        return home(ctx)

    items = _items(today)
    if kind == "g":
        found = next((g for g in logic.russia_groups(items) if g["key"] == ident), None)
        orders = found["orders"] if found else []
        back = courier_ru
    else:
        o = next((o for o, _ in items if o["order_no"] == ident and logic.is_city(o)), None)
        orders = [o] if o else []
        found = o
        back = courier_city
    if not found:
        return back(ctx, today, "Уже обработано")

    if action in ("g", "o"):
        text = logic.russia_card(found) if kind == "g" else logic.city_card(found)
        return ctx.show(text, _decision_rows(kind, ident, "c:ru" if kind == "g" else "c:city"))
    if action == "done":
        marked = sheets.mark_delivered([o["order_no"] for o in orders])
        try:
            record_shipments(marked, (users.get(ctx.uid) or {}).get("name") or "курьер", today)
        except Exception:
            log.exception("Не смог записать отгрузку в таблицу отгрузок")
        try:
            costs.record(marked, today)
        except Exception:
            log.exception("Не смог записать стоимость доставки")
        return back(ctx, today, "✅ Записано: доставлено")
    return back(ctx, today, "Оставлено в списке")


# ------------------------------------------------------------ производство
def production(ctx: Ctx, payload: str) -> None:
    parts = payload.split(":", 2)
    action = parts[1] if len(parts) > 1 else ""
    if action in ("plan", "stock", "cur"):
        pending.pop(ctx.uid, None)

    if action == "plan":
        text = logic.production_plan(sheets.read_orders(), now_date())
        return ctx.show(text, [[cb("📦 Остатки", "p:stock")], MENU_BTN])

    if action == "stock":
        codes = config.STOCK_CODES
        rows = [[cb(c, f"p:s:{c}") for c in codes[i:i + 2]] for i in range(0, len(codes), 2)]
        rows += [[cb("📊 Текущие остатки", "p:cur")], MENU_BTN]
        return ctx.show("📦 Остатки\nВыберите размер (число — ширина; 04 — чёрная, 06 — белая):", rows)

    if action == "s" and len(parts) == 3 and parts[2] in config.STOCK_CODES:
        pending[ctx.uid] = ("stock", parts[2])
        return ctx.show(f"Остаток {parts[2]}: напишите числом, сколько штук сейчас (например, 5).",
                        [[cb("↩️ Отмена", "p:stock")]])

    if action == "cur":
        stock = sheets.latest_stock()
        lines = ["📊 Текущие остатки", ""]
        for code in config.STOCK_CODES:
            if code in stock:
                qty, when = stock[code]
                lines.append(f"{code} — {qty} шт. ({when.split(' ')[0]})")
            else:
                lines.append(f"{code} — нет данных")
        return ctx.show("\n".join(lines), [[cb("✏️ Внести остаток", "p:stock")], MENU_BTN])

    home(ctx)


def save_stock_from_text(ctx: Ctx, name: str, text: str) -> None:
    m = re.fullmatch(r"\s*(\d{1,4})\s*(шт\.?)?\s*", text, re.I)
    if not m:
        return ctx.show("Нужно просто число, например 5. Или нажмите «Отмена».",
                        [[cb("↩️ Отмена", "p:stock")]])
    code = pending.pop(ctx.uid)[1]
    qty = int(m.group(1))
    sheets.add_stock(code, qty, name)
    ctx.show(f"✅ Записал: {code} — {qty} шт.",
             [[cb("✏️ Внести ещё", "p:stock")], [cb("📊 Текущие остатки", "p:cur")], MENU_BTN])


def record_shipments(marked: list, who: str, today) -> None:
    """Курьер отметил «доставлен» -> каждая кровать попадает в таблицу «Отгрузки», остаток списывается (если он внесён)."""
    if not marked:
        return
    stock = sheets.latest_stock()
    for o in marked:
        code = logic.order_code(o)
        if not code:
            log.warning("Отгрузка: не определил размер заказа %s, в «Отгрузки» не записан", o.get("order_no"))
            continue
        before = stock[code][0] if code in stock else None
        after = before - 1 if before is not None and before >= 1 else None
        a = parse_article(o.get("article") or "")
        sheets.add_shipment(code, 1, f"{who} (курьер)", before, after, today,
                            o.get("order_no", ""), (a or {}).get("legs", ""), o.get("channel", ""))
        if after is not None:
            sheets.add_stock(code, after, f"{who} (доставка)")
            stock[code] = (after, "")


# ---------------------------------------------------------- нажатия кнопок
def on_payload(ctx: Ctx, name: str, payload: str) -> None:
    if payload.startswith("role:"):
        return choose_role(ctx, name, payload[5:])
    user = users.get(ctx.uid)
    if not user:
        return role_choice(ctx)
    if not allowed(ctx.uid, user["role"]):
        users.pop(ctx.uid, None)
        return deny(ctx, user["role"])
    if payload == "menu":
        pending.pop(ctx.uid, None)
        ai.reset(ctx.uid)
        return home(ctx)
    if payload in ("ai:start", "ai:end") and config.AI_KEY and user["role"] in config.AI_ROLES:
        return ai_start(ctx) if payload == "ai:start" else ai_end(ctx)
    if payload.startswith("c:") and user["role"] == "courier":
        return courier(ctx, payload)
    if payload.startswith("p:"):
        # производство — всё; менеджер — только остатки (просмотр и внесение)
        section = payload.split(":")[1]
        if user["role"] == "prod" or (user["role"] == "manager" and section in ("stock", "s", "cur")):
            return production(ctx, payload)
    home(ctx, notification="Эта кнопка вам недоступна")


def handle_callback(u: dict) -> None:
    cbk = u.get("callback") or {}
    cid, uid = cbk.get("callback_id"), (cbk.get("user") or {}).get("user_id")
    if not cid or not uid:
        return
    user = cbk.get("user") or {}
    if not known(uid):
        return mx.answer(cid, notification="Нет доступа")
    name = user.get("name") or user.get("first_name") or ""
    msg = u.get("message") or cbk.get("message") or {}
    mid = (msg.get("body") or {}).get("mid")
    if mid:
        screens[uid] = mid  # сообщение с кнопкой, на которую нажали, — это и есть текущий экран
    try:
        on_payload(Ctx(uid, cid), name, cbk.get("payload") or "")
    except Exception:
        log.exception("Ошибка при обработке кнопки %r", cbk.get("payload"))
        mx.answer(cid, notification="⚠️ Ошибка, попробуйте ещё раз")


# ------------------------------------------------------ PDF и уведомления
def format_order(o) -> str:
    what = " ".join(x for x in (o.size, o.color) if x)
    return (f"• {o.channel} · {what} · {o.address}\n"
            f"  отгрузка {o.ship_date}, доставка {o.delivery_date} · № {o.order_no}")


def clear_all() -> tuple:
    """Удаляет из чатов всё, что бот помнит (свои сообщения и сообщения людей), кроме текущих экранов-меню.
    Потом обновляет меню у всех. Возвращает (удалено, не удалось)."""
    removed = failed = 0
    for key, mids in list(mx.tracked.items()):
        kind, _, raw = key.partition(":")
        try:
            target = {"type": kind, "id": int(raw)}
        except ValueError:
            continue
        keep = screens.get(target["id"]) if kind == "user" else None
        for mid in list(mids):
            if mid == keep:
                continue
            if mx.delete(mid, target):
                removed += 1
            else:
                failed += 1
                mx.forget(target, [mid])  # безнадёжные (старые) больше не трогаем
    for uid in list(users):
        try:
            ctx = Ctx(uid)
            if uid not in pending:
                home(ctx)
        except Exception:
            log.exception("Не смог обновить меню после /clear")
    return removed, failed


def broadcast(text: str, skip_uid=None) -> None:
    """Рассылает текст всем, кто зарегистрирован (роли из NOTIFY_ROLES), и в подписанные группы."""
    for uid, u in list(users.items()):
        if u["role"] not in config.NOTIFY_ROLES or not allowed(uid, u["role"]):
            continue
        mx.send({"type": "user", "id": uid}, text)
        if config.CLEAN_CHAT and uid != skip_uid and uid not in pending:
            ctx = Ctx(uid)
            ctx.fresh = True  # уведомление пришло ниже меню — поднимаем меню вниз
            home(ctx)
    for c in groups:
        mx.send({"type": "chat", "id": c}, text)


def notify_all(new: list, who: str, skip_uid=None) -> None:
    lines = [f"🆕 Новых заказов: {len(new)}" + (f" (загрузил(а): {who})" if who else "")]
    lines += [format_order(o) for o in new]
    broadcast("\n".join(lines), skip_uid)


# ---------------------------------------------------------- Яндекс Маркет
ym_state = {"last": 0.0, "fails": 0}


oz_state = {"last": 0.0, "fails": 0}


def ozon_check():
    """Один проход слежения за Озоном. None — если не получилось."""
    try:
        res = oz.poll(broadcast)
        oz_state["fails"] = 0
        return res
    except Exception:
        log.exception("Ошибка проверки Озона")
        oz_state["fails"] += 1
        if oz_state["fails"] == 3:
            for uid, u in list(users.items()):
                if u["role"] == "manager":
                    mx.send({"type": "user", "id": uid},
                            "⚠️ Не получается получить заказы с Озона (3 проверки подряд). "
                            "Возможно, ключ отозван или у Озона сбой. Подробности в логах бота.")
        return None


def market_check():
    """Один проход слежения за Маркетом. None — если не получилось."""
    try:
        res = ym_watch.poll(broadcast)
        ym_state["fails"] = 0
        return res
    except Exception:
        log.exception("Ошибка проверки Яндекс Маркета")
        ym_state["fails"] += 1
        if ym_state["fails"] == 3:  # три раза подряд — сообщаем менеджерам один раз
            for uid, u in list(users.items()):
                if u["role"] == "manager":
                    mx.send({"type": "user", "id": uid},
                            "⚠️ Не получается получить заказы с Яндекс Маркета (3 проверки подряд). "
                            "Возможно, токен отозван или у Маркета сбой. Подробности в логах бота.")
        return None


def process_pdf(url: str, filename: str, say, who: str, uid=None) -> bool:
    """Читает PDF и пишет заказы в таблицу. say(text, rows) — как отвечать. True — файл обработан."""
    say(f"Читаю файл «{filename}»…")
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "orders.pdf")
        try:
            mx.download(url, path)
            orders = parse_pdf(path)
        except ValueError as e:
            say(f"⚠️ {e}", [MENU_BTN])
            return False
        except Exception:
            log.exception("Ошибка при чтении PDF")
            say("⚠️ Не получилось скачать или прочитать файл. Попробуйте отправить ещё раз.", [MENU_BTN])
            return False
    try:
        new = sheets.add_orders(orders)
    except Exception:
        log.exception("Ошибка при записи в Google Таблицу")
        say("⚠️ Заказы прочитал, но записать в таблицу не вышло. Сообщите администратору.", [MENU_BTN])
        return False
    if not new:
        say(f"Все {len(orders)} заказов из файла уже есть в таблице, ничего не добавлял.", [MENU_BTN])
        return True
    notify_all(new, who, skip_uid=uid)
    summary = f"✅ Добавлено в таблицу: {len(new)}"
    if len(orders) - len(new):
        summary += f"\nПропущено (уже были): {len(orders) - len(new)}"
    say(summary, [MENU_BTN])
    return True


# --------------------------------------------------------------- сообщения
def handle_message(u: dict) -> None:
    msg = u.get("message") or {}
    sender = msg.get("sender") or {}
    if sender.get("is_bot"):
        return
    sender_id = sender.get("user_id")
    name = sender.get("name") or sender.get("first_name") or ""
    recipient = msg.get("recipient") or {}
    in_group = recipient.get("chat_type") == "chat"
    reply_to = ({"type": "chat", "id": recipient.get("chat_id")} if in_group
                else {"type": "user", "id": sender_id})
    if not reply_to["id"]:
        return
    body = msg.get("body") or {}
    raw = (body.get("text") or "").strip()
    cmd = raw.lower().split("@")[0].split(" ")[0] if raw.startswith("/") else ""
    user_mid = body.get("mid")

    if cmd == "/id":
        return mx.send(reply_to, f"Ваш ID: {sender_id}")

    if not in_group and not known(sender_id):
        return mx.send(reply_to, no_access_text(sender_id))  # чужим — только отказ и их ID

    ctx = None if in_group else Ctx(sender_id)

    def say(text: str, rows=None) -> None:
        if ctx:
            ctx.show(text, rows)
            ctx.fresh = False
        else:
            mx.send(reply_to, text)

    # --- PDF от менеджера
    files = [a for a in body.get("attachments") or [] if a.get("type") == "file"]
    if files:
        if ctx:
            ctx.fresh = True  # статус покажем новым сообщением под файлом, потом файл уберём
        ok_any = False
        for att in files:
            filename = att.get("filename") or "file.pdf"
            url = (att.get("payload") or {}).get("url")
            if not filename.lower().endswith(".pdf"):
                if ctx:
                    say("Мне нужен именно PDF-файл.", [MENU_BTN])
            elif sender_id not in config.MANAGER_IDS:
                if ctx:
                    say(f"Загружать файлы может только менеджер. Ваш ID: {sender_id}", [MENU_BTN])
            elif not url:
                say("⚠️ MAX не передал ссылку на файл. Попробуйте отправить ещё раз.")
            else:
                ok_any = process_pdf(url, filename, say, name, sender_id) or ok_any
        if ctx and ok_any and config.CLEAN_CHAT and user_mid:
            mx.delete(user_mid, reply_to)  # PDF уже в таблице — убираем из чата
        return

    # --- групповые чаты: только уведомления
    if in_group:
        chat_id = reply_to["id"]
        if cmd in ("/start", "/subscribe"):
            if chat_id not in groups:
                groups.append(chat_id)
                try:
                    sheets.add_group(chat_id)
                except Exception:
                    log.exception("Не смог записать группу")
            mx.send(reply_to, "✅ Буду присылать сюда уведомления о новых заказах.")
        elif cmd == "/stop":
            if chat_id in groups:
                groups.remove(chat_id)
                try:
                    sheets.remove_group(chat_id)
                except Exception:
                    log.exception("Не смог удалить группу")
            mx.send(reply_to, "Отписал этот чат от уведомлений.")
        return

    if user_mid:
        mx.track(reply_to, user_mid)  # запоминаем, чтобы /clear мог убрать и сообщения людей

    # --- личный диалог: сообщение пользователя убираем из чата, ответ — в экране-меню
    if config.CLEAN_CHAT and user_mid:
        ctx.fresh = not mx.delete(user_mid, reply_to)  # не удалилось -> меню пришлём заново внизу

    if cmd == "/clear":  # чистка чатов бота у всех (только админ = менеджер из MANAGER_IDS)
        if sender_id not in config.MANAGER_IDS:
            return
        ctx.show("🧹 Чищу чаты…", [MENU_BTN])
        removed, failed = clear_all()
        text = f"🧹 Готово: удалено сообщений — {removed}."
        if failed:
            text += f"\nНе удалилось: {failed} (MAX не разрешает удалять старые сообщения или чужие)."
        return ctx.show(text, [MENU_BTN])

    if cmd == "/ym":  # разведка Яндекс Маркета (только менеджер)
        if sender_id not in config.MANAGER_IDS:
            return ctx.show("Команда только для менеджера.", [MENU_BTN])
        if not config.YM_TOKEN:
            return ctx.show("Не задана переменная YANDEX_MARKET_TOKEN в настройках бота.", [MENU_BTN])
        ctx.show("Запрашиваю данные Маркета…", [MENU_BTN])
        try:
            parts = ym.discovery()
        except Exception:
            log.exception("Ошибка запроса к Яндекс Маркету")
            parts = ["⚠️ Не получилось связаться с Маркетом, подробности в логах."]
        for p in parts:
            mx.send(reply_to, p)
        return

    if cmd in ("/oz", "/oznow"):  # Озон: разведка / проверка сейчас (только менеджер)
        if sender_id not in config.MANAGER_IDS:
            return ctx.show("Команда только для менеджера.", [MENU_BTN])
        if not (config.OZ_CLIENT_ID and config.OZ_API_KEY):
            return ctx.show("Не заданы переменные OZON_CLIENT_ID и OZON_API_KEY в настройках бота.", [MENU_BTN])
        if cmd == "/oz":
            ctx.show("Запрашиваю данные Озона…", [MENU_BTN])
            try:
                parts = oz.discovery(raw="raw" in raw.lower().split()[1:2])
            except Exception:
                log.exception("Ошибка запроса к Озону")
                parts = ["⚠️ Не получилось связаться с Озоном, подробности в логах."]
            for p in parts:
                mx.send(reply_to, p)
            return
        ctx.show("Проверяю Озон…", [MENU_BTN])
        res = ozon_check()
        if res is None:
            return ctx.show("⚠️ Не получилось проверить Озон. Выполните /oz, чтобы увидеть ответ Озона.", [MENU_BTN])
        text = f"Проверил Озон: необработанных отправлений — {res['total']}, новых — {res['new']}."
        if res["baseline"]:
            text += "\nЭто первый запуск: текущие отправления запомнил без уведомлений, о новых скажу сразу."
        if res["sample"] and not res["new"]:
            text += "\n\nТак будет выглядеть уведомление (последнее отправление):\n" + res["sample"]
        return ctx.show(text, [MENU_BTN])

    if cmd == "/status":  # диагностика без доступа к логам (только менеджер)
        if sender_id not in config.MANAGER_IDS:
            return
        mx.send({"type": "user", "id": sender_id}, "Меню команд: " + mx.LAST_COMMANDS_RESULT)
        return
    if cmd == "/ymnow":  # проверить Маркет прямо сейчас (только менеджер)
        if sender_id not in config.MANAGER_IDS:
            return ctx.show("Команда только для менеджера.", [MENU_BTN])
        if not config.YM_TOKEN:
            return ctx.show("Не задана переменная YANDEX_MARKET_TOKEN в настройках бота.", [MENU_BTN])
        ctx.show("Проверяю Яндекс Маркет…", [MENU_BTN])
        res = market_check()
        if res is None:
            return ctx.show("⚠️ Не получилось проверить Маркет, подробности в логах.", [MENU_BTN])
        text = f"Проверил Маркет: заказов в обработке — {res['total']}, новых — {res['new']}, записано в таблицу — {res['added']}."
        if res["baseline"]:
            text += "\nЭто первый запуск: текущие заказы запомнил без уведомлений, о новых скажу сразу."
        if res["sample"] and not res["new"]:
            text += "\n\nТак будет выглядеть уведомление (последний заказ):\n" + res["sample"]
        return ctx.show(text, [MENU_BTN])

    if cmd == "/stop":
        users.pop(sender_id, None)
        pending.pop(sender_id, None)
        try:
            sheets.delete_user(sender_id)
        except Exception:
            log.exception("Не смог удалить пользователя")
        return ctx.show("Вы вышли. Напишите /start, чтобы выбрать роль заново.")
    if cmd == "/role":
        return role_choice(ctx)
    if cmd in ("/start", "/menu"):
        pending.pop(sender_id, None)
        ai.reset(sender_id)
        if sender_id in users:
            return home(ctx)
        return role_choice(ctx, "Привет! Я бот заказов.")

    mode = pending.get(sender_id)
    if mode and raw and not cmd:
        try:
            if mode[0] == "ai":
                return ai_answer(ctx, raw)
            return save_stock_from_text(ctx, name, raw)
        except Exception:
            log.exception("Ошибка при записи отгрузки/остатка")
            return ctx.show("⚠️ Не получилось записать. Попробуйте ещё раз.", [MENU_BTN])
    if sender_id in users:
        return home(ctx)
    role_choice(ctx, "Привет! Я бот заказов.")


def handle_update(u: dict) -> None:
    kind = u.get("update_type")
    if kind == "message_callback":
        handle_callback(u)
    elif kind == "message_created":
        handle_message(u)
    elif kind == "bot_started":
        uid = (u.get("user") or {}).get("user_id")
        if uid and not known(uid):
            mx.send({"type": "user", "id": uid}, no_access_text(uid))
        elif uid:
            ctx = Ctx(uid)
            if uid in users:
                home(ctx)
            else:
                role_choice(ctx, "Привет! Я бот заказов.")


# ------------------------------------------------------------------- запуск
def reload_directory() -> None:
    users.clear()
    users.update({uid: u for uid, u in sheets.load_users().items() if allowed(uid, u["role"])})
    groups[:] = sheets.load_groups()
    log.info("Пользователей: %d, групп: %d", len(users), len(groups))


def load_marker():
    p = Path(config.STATE_FILE)
    try:
        return json.loads(p.read_text(encoding="utf-8")).get("marker") if p.exists() else None
    except Exception:
        return None


def save_marker(marker) -> None:
    try:
        Path(config.STATE_FILE).write_text(json.dumps({"marker": marker}), encoding="utf-8")
    except Exception:
        pass  # не страшно: дубли заказов всё равно отсекаются


def main() -> None:
    config.check()
    for key, ids, var in (("курьер", config.COURIER_IDS, "COURIER_IDS"), ("производство", config.PROD_IDS, "PROD_IDS")):
        if not ids:
            log.warning("%s пуст: роль «%s» никому не доступна. Впишите ID сотрудников (число из команды /id).", var, key)
    if not config.MANAGER_IDS:
        log.warning("MANAGER_IDS пуст: никто не сможет стать менеджером и загружать PDF. "
                    "Напишите боту /id и впишите свой ID в настройки.")
    me = mx.get_me()
    if me.status_code in (401, 403):
        sys.exit("MAX не принял токен. Проверьте MAX_BOT_TOKEN.")
    me.raise_for_status()
    log.info("Бот запущен: %s", me.json().get("name") or me.json())
    mx.set_commands()
    # Рабочие логи Bothost не всегда видны, поэтому итог запуска бот сам присылает менеджерам
    for _uid in config.MANAGER_IDS:
        try:
            mx.send({"type": "user", "id": _uid}, "🤖 Бот перезапущен.\nМеню команд: " + mx.LAST_COMMANDS_RESULT)
        except Exception:
            log.exception("Не смог отправить менеджеру отчёт о запуске")

    try:
        reload_directory()
    except Exception:
        log.exception("Не смог загрузить пользователей из таблицы. Проверьте SHEET_ID, "
                      "ключ Google и доступ сервисного аккаунта к таблице")
    last_reload = time.time()

    marker = load_marker()
    if config.OZ_CLIENT_ID and config.OZ_API_KEY and config.OZ_WATCH:
        log.info("Слежение за Озоном включено: проверка раз в %d мин.", config.YM_POLL_MINUTES)
    if config.YM_TOKEN and config.YM_WATCH:
        log.info("Слежение за Яндекс Маркетом включено: проверка раз в %d мин.", config.YM_POLL_MINUTES)
    elif config.YM_TOKEN:
        log.info("Слежение за Яндекс Маркетом выключено (YM_WATCH=0)")
    import requests  # локально, чтобы не тащить наверх ради одного except
    while True:
        if (config.OZ_CLIENT_ID and config.OZ_API_KEY and config.OZ_WATCH
                and time.time() - oz_state["last"] >= config.YM_POLL_MINUTES * 60):
            oz_state["last"] = time.time()
            ozon_check()
        if config.YM_TOKEN and config.YM_WATCH and time.time() - ym_state["last"] >= config.YM_POLL_MINUTES * 60:
            ym_state["last"] = time.time()
            market_check()
        try:
            r = mx.get_updates(marker)
            if r.status_code in (401, 403):
                sys.exit("MAX не принял токен. Проверьте MAX_BOT_TOKEN.")
            r.raise_for_status()
            data = r.json()
        except requests.RequestException:
            log.exception("Ошибка связи с MAX, повторю через 5 секунд")
            time.sleep(5)
            continue

        for update in data.get("updates") or []:
            try:
                handle_update(update)
            except Exception:
                log.exception("Ошибка при обработке события")
        if data.get("marker") is not None:
            marker = data["marker"]
            save_marker(marker)

        if time.time() - last_reload > RELOAD_EVERY:
            last_reload = time.time()
            sheets.safe_sync_colors()  # подхватываем ручные пометки менеджера («Отмена», «Возврат»)
            try:
                reload_directory()
            except Exception:
                log.exception("Не смог обновить список пользователей")


if __name__ == "__main__":
    main()
