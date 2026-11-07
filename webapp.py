"""Приложение для сотрудников (телефон, браузер, APK) + навык Алисы на одном веб-сервере.

Как входят в приложение:
  1. Человек открывает приложение. Телефон сам создаёт секретный ключ и хранит его у себя.
  2. Из ключа сервер считает короткий КОД вида «K7QM-2XPA» и показывает его на экране.
  3. Человек присылает код менеджеру. Менеджер добавляет его во вкладке «Менеджер» приложения
     или командой в MAX: /app K7QM-2XPA курьер Иван
  4. Всё: телефон пускает в нужную роль. Уволился — менеджер удаляет код, и доступ пропадает.
Сам ключ никуда не пересылается: по коду войти нельзя, код только «узнаёт» телефон.
Список телефонов хранится на листе «Приложение» в той же Google Таблице.
"""
import base64
import hashlib
import hmac
import json
import logging
import re
import secrets
import threading
import time
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

import config
import logic
import sheets

log = logging.getLogger("orders-bot")

APP_TAB = "Приложение"
APP_HEADERS = ["Код", "Имя", "Роль", "Добавлен", "Кто добавил"]
# Роли приложения: те же, что в MAX, плюс директора (их в MAX нет, поэтому список свой)
ROLE_LABELS = {**config.ROLE_LABELS, "techdir": "Тех. директор", "gendir": "Ген. директор"}
ROLE_WORDS = {"техдиректор": "techdir", "тех.директор": "techdir", "технический": "techdir",
              "гендиректор": "gendir", "ген.директор": "gendir", "генеральный": "gendir", "директор": "gendir",
              "менеджер": "manager", "курьер": "courier", "производство": "prod", "цех": "prod",
              "кладовщик": "keeper", "склад": "keeper"}
HERE = Path(__file__).resolve().parent
STATIC = {  # что отдаём браузеру как есть
    "/": ("app.html", "text/html; charset=utf-8"),
    "/app": ("app.html", "text/html; charset=utf-8"),
    "/manifest.webmanifest": ("app/manifest.webmanifest", "application/manifest+json"),
    "/icon-192.png": ("app/icon-192.png", "image/png"),
    "/icon-512.png": ("app/icon-512.png", "image/png"),
}

# Нужен, чтобы телефон предлагал «Установить приложение». Хранится здесь, а не файлом .js:
# увидев .js в проекте, Bothost решает, что бот на Node.js, и запускает не то.
SW_JS = b"""self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", e => e.waitUntil(self.clients.claim()));
self.addEventListener("fetch", () => {});
"""

_deps: dict = {}  # функции бота: deliver, move, notify, shipped, stock, frames, inv_apply, inv_create, returns, pdf
# Кто видит «Расчёт доставок» в приложении: коды телефонов через запятую в переменной PAY_CODES на Bothost.
# Пусто — не видит никто. Переменную меняет только владелец хостинга, из приложения её не изменить.
def pay_codes() -> set:
    import os
    return {c for c in (norm_code(x) for x in os.getenv("PAY_CODES", "").split(",")) if c}


RETURN_KINDS = {"ya": "Яндекс", "oz": "Озон", "wb": "Вайлдберриз"}
MAX_PDF = 20 * 1024 * 1024
_lock = threading.Lock()  # записи в таблицу из приложения — по одной, чтобы не мешали друг другу
_devices = {"t": 0.0, "data": {}}
_secret = {"key": b""}


def now_date() -> date:
    return datetime.now(ZoneInfo(config.TIMEZONE)).date()


# ------------------------------------------------------------ коды телефонов
def _server_key() -> bytes:
    """Секрет сервера для кодов. Свой в переменной APP_SECRET или созданный один раз и сохранённый в «Настройках»."""
    if not _secret["key"]:
        import os
        key = os.getenv("APP_SECRET", "").strip()
        if not key:
            key = sheets.get_setting("app_secret", max_age=0)
            if not key:
                key = secrets.token_hex(24)
                sheets.set_setting("app_secret", key)
        _secret["key"] = key.encode()
    return _secret["key"]


def code_for(token: str) -> str:
    digest = hmac.new(_server_key(), token.encode(), hashlib.sha256).digest()
    raw = base64.b32encode(digest).decode()[:8]
    return f"{raw[:4]}-{raw[4:]}"


def norm_code(text: str) -> str:
    raw = re.sub(r"[^A-Z2-7]", "", (text or "").upper().replace("О", "O").replace("К", "K").replace("М", "M")
                 .replace("А", "A").replace("Р", "P").replace("Е", "E").replace("С", "C").replace("Т", "T")
                 .replace("Х", "X").replace("В", "B").replace("Н", "H")
                 .replace("0", "O").replace("1", "I").replace("8", "B"))  # путают ноль с буквой О и т.п.
    return f"{raw[:4]}-{raw[4:8]}" if len(raw) == 8 else ""


def _app_ws():
    return sheets._tab(APP_TAB, APP_HEADERS)


def devices(force: bool = False) -> dict:
    """{КОД: {"name", "role", "row"}} — кого пустили в приложение."""
    if force or time.time() - _devices["t"] > 30:
        labels = {v.lower(): k for k, v in ROLE_LABELS.items()}
        data = {}
        for i, v in enumerate(_app_ws().get_all_values()[1:], start=2):
            code = norm_code(v[0] if v else "")
            role = labels.get((v[2] if len(v) > 2 else "").strip().lower()) or ROLE_WORDS.get(
                (v[2] if len(v) > 2 else "").strip().lower())
            if code and role:
                data[code] = {"name": (v[1] if len(v) > 1 else "").strip(), "role": role, "row": i}
        _devices.update(t=time.time(), data=data)
    return _devices["data"]


def add_device(code: str, role: str, name: str, who: str) -> None:
    ws = _app_ws()
    row = [code, name, ROLE_LABELS[role], datetime.now(ZoneInfo(config.TIMEZONE)).strftime("%d.%m.%Y %H:%M"), who]
    have = devices(force=True)
    if code in have:
        r = have[code]["row"]
        ws.update(values=[row], range_name=f"A{r}:E{r}", value_input_option="RAW")
    else:
        ws.append_row(row, value_input_option="RAW")
    devices(force=True)


def remove_device(code: str) -> bool:
    have = devices(force=True)
    if code not in have:
        return False
    _app_ws().delete_rows(have[code]["row"])
    devices(force=True)
    return True


# ------------------------------------------------------------ чат сотрудников
# Сообщения лежат на листе «Чат». Сервер читает лист один раз при запуске и дальше держит их в памяти:
# телефоны спрашивают «есть новое?» каждые несколько секунд, и если бы каждый раз читать таблицу,
# Google быстро упёрся бы в лимит. В таблицу только дописываем новые сообщения.
CHAT_TAB = "Чат"
CHAT_HEADERS = ["Время", "Код", "Имя", "Роль", "Сообщение", "Изменено"]
CHAT_KEEP = 500        # сколько последних сообщений держим в памяти и отдаём телефону
CHAT_MAX_LEN = 1000    # длина одного сообщения
# rev — номер последнего изменения в чате. Телефон присылает свой rev и получает только то,
# что появилось или поменялось после него (новые и отредактированные сообщения).
_chat = {"loaded": False, "msgs": [], "rev": 0, "epoch": str(int(time.time()))}
_chat_lock = threading.Lock()


def _chat_ws():
    if CHAT_TAB not in sheets._tabs:  # у листа, созданного раньше, всего 5 столбцов — добавим место под «Изменено»
        try:
            ws = sheets.get_book().worksheet(CHAT_TAB)
            if ws.col_count < len(CHAT_HEADERS):
                ws.add_cols(len(CHAT_HEADERS) - ws.col_count)
        except Exception:
            pass
    return sheets._tab(CHAT_TAB, CHAT_HEADERS)


def _chat_bump(m: dict) -> None:
    _chat["rev"] += 1
    m["rev"] = _chat["rev"]


def _chat_load() -> None:
    if _chat["loaded"]:
        return
    labels = {v.lower(): k for k, v in ROLE_LABELS.items()}
    msgs = []
    for i, v in enumerate(_chat_ws().get_all_values()[1:], start=2):
        v = list(v) + [""] * 6
        text = v[4].strip()
        if not text:
            continue
        msgs.append({"id": i, "t": v[0].strip(), "code": norm_code(v[1]), "name": v[2].strip() or "сотрудник",
                     "role": labels.get(v[3].strip().lower(), ""), "text": text, "edited": v[5].strip()})
    msgs = msgs[-CHAT_KEEP:]
    for m in msgs:
        _chat_bump(m)
    _chat.update(loaded=True, msgs=msgs)


def chat_list(since: int, code: str) -> dict:
    with _chat_lock:
        _chat_load()
        msgs = [m for m in _chat["msgs"] if m["rev"] > since]
        rev = _chat["rev"]
    return {"epoch": _chat["epoch"], "rev": rev,
            "msgs": [{"id": m["id"], "t": m["t"], "name": m["name"], "role": m["role"],
                      "label": ROLE_LABELS.get(m["role"], ""), "text": m["text"],
                      "edited": bool(m.get("edited")), "mine": m["code"] == code}
                     for m in msgs]}


def _clean(text: str) -> str:
    text = re.sub(r"\n{3,}", "\n\n", (text or "").replace("\r", "")).strip()[:CHAT_MAX_LEN]
    if not text:
        raise ValueError("Пустое сообщение")
    return text


def chat_send(text: str, me: dict, code: str) -> dict:
    text = _clean(text)
    now = datetime.now(ZoneInfo(config.TIMEZONE)).strftime("%d.%m.%Y %H:%M")
    name = me["name"] or "сотрудник"
    with _chat_lock:
        _chat_load()
        # RAW: таблица сохраняет текст как есть, «=2+2» или «+7…» не превратятся в формулу
        res = _chat_ws().append_row([now, code, name, ROLE_LABELS[me["role"]], text, ""],
                                    value_input_option="RAW")
        # номер строки, куда легло сообщение, — по нему потом находим сообщение для правки
        hit = re.search(r"!A(\d+)", str(((res or {}).get("updates") or {}).get("updatedRange") or ""))
        new_id = int(hit.group(1)) if hit else ((_chat["msgs"][-1]["id"] + 1) if _chat["msgs"] else 2)
        m = {"id": new_id, "t": now, "code": code, "name": name, "role": me["role"], "text": text, "edited": ""}
        _chat_bump(m)
        _chat["msgs"].append(m)
        del _chat["msgs"][:-CHAT_KEEP]
    return {"ok": True, "id": new_id}


def chat_edit(msg_id: int, text: str, code: str) -> dict:
    """Правка своего сообщения. Чужие сообщения менять нельзя."""
    text = _clean(text)
    with _chat_lock:
        _chat_load()
        m = next((x for x in _chat["msgs"] if x["id"] == msg_id), None)
        if not m:
            raise ValueError("Сообщение слишком старое или удалено")
        if m["code"] != code:
            raise PermissionError("Можно менять только свои сообщения")
        if m["text"] == text:
            return {"ok": True}
        ws = _chat_ws()
        row = ws.row_values(msg_id) + [""] * 6
        if norm_code(row[1]) != code or row[0].strip() != m["t"]:  # строки в таблице сдвинули вручную — не трогаем чужую
            raise ValueError("В таблице чата поменялись строки, правка не сохранена")
        now = datetime.now(ZoneInfo(config.TIMEZONE)).strftime("%d.%m.%Y %H:%M")
        ws.update(values=[[text, now]], range_name=f"E{msg_id}:F{msg_id}", value_input_option="RAW")
        m.update(text=text, edited=now)
        _chat_bump(m)
    return {"ok": True}


def _parse_role(text: str):
    t = (text or "").strip().lower()
    for word, key in ROLE_WORDS.items():
        if t.startswith(word[:4]) or t.replace(" ", "") == word:
            return key
    return t if t in ROLE_LABELS else None


def app_command(raw: str, who: str) -> str:
    """Команда менеджера в MAX: /app КОД роль Имя · /app — список · /app удалить КОД."""
    parts = raw.split()[1:]
    usage = ("📱 Приложение для сотрудников\n\nДобавить телефон:\n/app КОД роль Имя\n"
             "например: /app K7QM-2XPA курьер Иван\n\nРоли: менеджер, курьер, производство, кладовщик, техдиректор, гендиректор\n"
             "Убрать: /app удалить КОД")
    try:
        if not parts:
            have = devices(force=True)
            if not have:
                return usage + "\n\nПока никого не добавлено."
            lines = [f"• {c} — {d['name'] or 'без имени'}, {ROLE_LABELS[d['role']]}" for c, d in have.items()]
            return usage + "\n\nСейчас в приложении:\n" + "\n".join(lines)
        if parts[0].lower().startswith("удал") and len(parts) > 1:
            code = norm_code(parts[1])
            return f"🗑 {code} удалён из приложения." if code and remove_device(code) else "Такого кода нет в списке."
        if len(parts) > 1 and not norm_code(parts[0]) and norm_code(parts[0] + parts[1]):
            parts = [parts[0] + parts[1]] + parts[2:]  # код написали через пробел: «K7QM 2XPA»
        code = norm_code(parts[0])
        role = _parse_role(parts[1]) if len(parts) > 1 else None
        if not code or not role:
            return "Не понял. " + usage
        name = " ".join(parts[2:])[:40]
        add_device(code, role, name, who)
        return f"✅ {code} добавлен: {name or 'без имени'}, {ROLE_LABELS[role]}.\nПусть нажмёт «Проверить» в приложении."
    except Exception:
        log.exception("Приложение: команда /app")
        return "⚠️ Не получилось записать в таблицу, попробуйте ещё раз."


# ------------------------------------------------------------ данные экранов
def _qty_text(o: dict) -> str:
    return f" ×{logic.qty(o)}" if logic.qty(o) > 1 else ""


def _rows(items: list) -> list:
    """Строки таблицы доставок: одна отгрузка по России или один городской адрес — одна строка."""
    rows = []
    for g in logic.russia_groups(items):
        cnt = logic.count_items(g["orders"])
        rows.append({
            "kind": "ru", "key": g["key"], "late": g["late"], "channel": g["channel"],
            "place": "Отгрузка" + (f" №{g['shipment']}" if g["shipment"] else ""),
            "beds": [f"{name}" + (f" ×{n}" if n > 1 else "") for name, n in sorted(cnt.items())],
            "qty": sum(cnt.values()), "orders": [o["order_no"] for o in g["orders"]],
        })
    for o, late in logic.city_items(items):
        rows.append({
            "kind": "city", "key": o["order_no"], "late": late, "channel": o["channel"],
            "place": o["address"] or "адрес не указан", "address": o["address"],
            "beds": [logic.item_line(o)], "qty": logic.qty(o), "orders": [o["order_no"]],
            "phone": logic.fmt_phone(o["phone"]),
            "tel": re.sub(r"[^\d+]", "", (o["phone"] or "").split("доб")[0]),
        })
    return rows


def courier_data(today: date, pay: bool = False) -> dict:
    orders = sheets.read_orders()
    days = []
    for n, label in ((0, "Сегодня"), (1, "Завтра"), (2, "Послезавтра")):
        day = today + timedelta(days=n)
        items = logic.courier_items(orders, today) if n == 0 else logic.day_items(orders, day, today)
        rows = _rows(items)
        days.append({"label": label, "date": f"{day:%d.%m} ({logic.WEEKDAYS[day.weekday()]})", "today": n == 0,
                     "rows": rows, "qty": sum(r["qty"] for r in rows)})
    return {"days": days, "pay": pay,
            "returns": {"kinds": RETURN_KINDS, "codes": [{"code": c, "name": logic.code_name(c)} for c in config.STOCK_CODES]}}


def courier_pay(today: date) -> dict:
    try:
        import costs
    except ImportError:
        return {"text": "Расчёт доставок не подключён.", "url": ""}
    if not costs.enabled():
        return {"text": "Расчёт доставок пока не настроен. Попросите менеджера создать таблицу.", "url": ""}
    return {"text": costs.summary(today), "url": costs.sheet_url()}


def _find(today: date, kind: str, key: str) -> list:
    items = logic.courier_items(sheets.read_orders(force=True), today)
    if kind == "ru":
        g = next((g for g in logic.russia_groups(items) if g["key"] == key), None)
        return g["orders"] if g else []
    return [o for o, _ in items if o["order_no"] == key and logic.is_city(o)][:1]


def _shipped_today(today: date) -> int:
    """Сколько кроватей производство уже отметило отгруженными сегодня."""
    try:
        return _deps["shipped_done"](today)
    except Exception:
        return 0


def prod_data(today: date) -> dict:
    orders = sheets.read_orders()
    stock = sheets.latest_stock()
    need = logic.need_counts(orders)
    rows = []
    for code in config.STOCK_CODES:
        have = stock[code][0] if code in stock else None
        rows.append({"code": code, "name": logic.code_name(code), "have": have, "need": need.get(code, 0)})
    try:
        frames = [{"width": w, "n": n} for w, n in sorted(sheets.frames_get().items())]
    except Exception:
        log.exception("Приложение: каркасы")
        frames = []
    total = logic.today_total(orders, today)
    done = _shipped_today(today)
    # сколько каркасов сварить: нужно по заказам − уже покрашено − уже сварено (по ширине, оба цвета)
    fr = {f["width"]: f["n"] for f in frames}
    weld = []
    for w in sheets.frame_widths():
        need_w = sum(x["need"] for x in rows if x["code"].startswith(f"{w}."))
        have_w = sum(x["have"] or 0 for x in rows if x["code"].startswith(f"{w}."))
        weld.append({"width": w, "need": need_w, "painted": have_w, "frames": fr.get(w, 0),
                     "todo": max(0, need_w - have_w - fr.get(w, 0))})
    return {"plan": logic.production_plan(orders, today), "stock": rows, "frames": frames, "weld": weld,
            "today": max(0, total - done), "shipped": done, "blanks": blanks_data(), "legs": legs_data(), "legs_painted": legs_data("painted")}


def legs_data(kind: str = "welded") -> list:
    try:
        return [{"height": h, **({"n": n} if isinstance(n, int) else {"black": n.get("04", 0), "white": n.get("06", 0)})}
                for h, n in sheets.legs_get(kind).items()]
    except Exception:
        log.exception("Приложение: ножки (%s)", kind)
        return []


def blanks_data() -> dict:
    try:
        b = sheets.blanks_get()
    except Exception:
        log.exception("Приложение: лист «%s»", config.BLANKS_TAB)
        return {"ok": False}
    enough = {w: logic.blanks_enough(parts) for w, parts in b["sizes"].items()}
    return {"ok": True, "per": b["per"], "common": b["common"], "bom": logic.BLANKS_BOM,
            "sizes": {str(w): v for w, v in b["sizes"].items()}, "common_qty": b["common_qty"],
            "enough": [{"width": w, "n": n, "short": t} for w, (n, t) in enough.items()]}


def keeper_data() -> dict:
    res = {}
    for kind in ("raw", "tmc"):
        try:
            res[kind] = [{"row": i["row"], "name": i["name"], "qty": logic.fmt_qty(i["qty"]), "unit": i["unit"],
                          "min": logic.fmt_qty(i["min"]) if i["min"] else "",
                          "low": bool(i["min"] and i["qty"] < i["min"])} for i in sheets.inv_get(kind)]
        except Exception:
            log.exception("Приложение: склад %s", kind)
            res[kind] = []
    return res


def open_orders(today: date) -> dict:
    """Недоставленные заказы для таблицы в приложении, по дате доставки/отгрузки."""
    rows = []
    for o in sheets.read_orders():
        if o["status"]:
            continue
        r = logic.key_range(o, today)
        city = logic.is_city(o)
        rows.append({
            "sort": str(r[0]) if r else "9999", "date": f"{r[1]:%d.%m}" if r else (logic.key_text(o) or "—"),
            "late": bool(r and r[1] < today), "today": bool(r and r[0] <= today <= r[1]),
            "city": city, "channel": o["channel"], "bed": logic.item_line(o), "qty": logic.qty(o),
            "order_no": o["order_no"], "place": (o["address"] if city else (f"Отгрузка №{o['phone']}" if o["phone"] else "")) or "—",
            "phone": logic.fmt_phone(o["phone"]) if city else "", "article": o["article"],
        })
    rows.sort(key=lambda x: (x["sort"], not x["city"], x["channel"], x["place"]))
    return {"rows": rows, "url": sheets.sheet_url("orders")}


def manager_data(today: date, pay: bool = False) -> dict:
    orders = sheets.read_orders()
    open_ = [o for o in orders if not o["status"]]
    links = []
    try:
        links.append({"title": "📦 Таблица остатков в Google", "url": sheets.sheet_url("stock"), "hidden": True})
    except Exception:
        log.exception("Приложение: ссылки на таблицы")
    try:
        import costs
        if pay and costs.enabled():
            links.append({"title": "💰 Расчёт доставок", "url": costs.sheet_url()})
    except ImportError:
        pass
    return {
        "links": links,
        "today": max(0, logic.today_total(orders, today) - _shipped_today(today)),
        "open": sum(logic.qty(o) for o in open_),
        "city": sum(logic.qty(o) for o in open_ if logic.is_city(o)),
        "devices": [{"code": c, "name": d["name"], "role": d["role"], "label": ROLE_LABELS[d["role"]]}
                    for c, d in devices(force=True).items()],
    }


# ------------------------------------------------------------ табель цеха (тех. директор)
# «Работники цеха» — люди, которым ведут учёт часов. Им не нужно приложение: тех. директор заводит их вручную.
# «Табель» — по строке на человека и день: сколько часов отработал.
# «Отчёты табеля» — итоги за период, которые тех. директор отправил ген. директору (снимок на момент отправки).
WORKERS_TAB = "Работники цеха"
WORKERS_HEADERS = ["ID", "Имя", "Участок", "Добавлен", "Кто добавил", "Статус"]
TABEL_TAB = "Табель"
TABEL_HEADERS = ["Дата", "ID", "Имя", "Часы", "Кто внёс", "Изменено"]
REPORTS_TAB = "Отчёты табеля"
REPORTS_HEADERS = ["Отправлено", "Кто отправил", "С", "По", "Всего часов", "Данные"]
FIRED = "Уволен"


def _stamp() -> str:
    return datetime.now(ZoneInfo(config.TIMEZONE)).strftime("%d.%m.%Y %H:%M")


def _d(x) -> date:
    """«2026-11-07» или «07.11.2026» -> дата."""
    x = str(x or "").strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return datetime.strptime(x, fmt).date()
        except ValueError:
            pass
    raise ValueError("Не понял дату")


def _hours(x):
    """Часы из ячейки или из поля ввода: «8», «7,5» -> 8 / 7.5; пусто или 0 -> None."""
    try:
        h = round(float(str(x).replace(",", ".").strip()), 2)
    except (TypeError, ValueError):
        return None
    if h <= 0:
        return None
    if h > 24:
        raise ValueError("В сутках не больше 24 часов")
    return int(h) if h == int(h) else h


def workers(include_fired: bool = False) -> list:
    out = []
    for i, v in enumerate(sheets._tab(WORKERS_TAB, WORKERS_HEADERS).get_all_values()[1:], start=2):
        v = list(v) + [""] * 6
        if not v[0].strip() or not v[1].strip():
            continue
        fired = v[5].strip().lower() == FIRED.lower()
        if fired and not include_fired:
            continue
        out.append({"id": v[0].strip(), "name": v[1].strip(), "area": v[2].strip(), "row": i, "fired": fired})
    return sorted(out, key=lambda w: (w["area"].lower(), w["name"].lower()))


def worker_add(name: str, area: str, who: str) -> None:
    name, area = name.strip()[:40], area.strip()[:30]
    if not name:
        raise ValueError("Впишите имя")
    if any(w["name"].lower() == name.lower() for w in workers()):
        raise ValueError("Такой сотрудник уже есть")
    sheets._tab(WORKERS_TAB, WORKERS_HEADERS).append_row(
        [secrets.token_hex(3), name, area, _stamp(), who, ""], value_input_option="RAW")


def worker_fire(wid: str) -> None:
    """Не удаляем строку: часы за прошлые дни должны остаться в отчётах. Просто прячем из списка."""
    w = next((x for x in workers() if x["id"] == wid), None)
    if not w:
        raise ValueError("Сотрудник не найден")
    sheets._tab(WORKERS_TAB, WORKERS_HEADERS).update(values=[[FIRED]], range_name=f"F{w['row']}",
                                                     value_input_option="RAW")


def _tabel() -> list:
    out = []
    for i, v in enumerate(sheets._tab(TABEL_TAB, TABEL_HEADERS).get_all_values()[1:], start=2):
        v = list(v) + [""] * 6
        try:
            day = _d(v[0])
        except ValueError:
            continue
        try:
            h = _hours(v[3])
        except ValueError:
            h = None
        out.append({"row": i, "day": day, "id": v[1].strip(), "name": v[2].strip(), "hours": h})
    return out


def tech_data(day: date) -> dict:
    ws_list = workers()
    rows = _tabel()
    got = {r["id"]: r["hours"] for r in rows if r["day"] == day}
    month = [r for r in rows if r["day"].year == day.year and r["day"].month == day.month and r["hours"]]
    return {"day": day.isoformat(), "today": now_date().isoformat(),
            "workers": [{"id": w["id"], "name": w["name"], "area": w["area"], "hours": got.get(w["id"])} for w in ws_list],
            "filled": sum(1 for w in ws_list if got.get(w["id"])),
            "month_hours": round(sum(r["hours"] for r in month), 2)}


def tech_day_save(day: date, hours: dict, who: str) -> int:
    """Записывает часы за день: {ID: часы}. Пусто или 0 — «не работал» (часы в строке стираются)."""
    if day > now_date():
        raise ValueError("Нельзя заполнить табель на будущее")
    if day < now_date() - timedelta(days=400):
        raise ValueError("Слишком старая дата")
    known = {w["id"]: w for w in workers(include_fired=True)}
    clean = {}
    for wid, h in (hours or {}).items():
        if wid in known:
            clean[wid] = _hours(h)
    ws = sheets._tab(TABEL_TAB, TABEL_HEADERS)
    have = {r["id"]: r for r in _tabel() if r["day"] == day}
    now, ds, ups, new = _stamp(), day.strftime("%d.%m.%Y"), [], []
    for wid, h in clean.items():
        r = have.get(wid)
        if r:
            if r["hours"] != h:
                ups.append({"range": f"C{r['row']}:F{r['row']}", "values": [[known[wid]["name"], "" if h is None else h, who, now]]})
        elif h is not None:
            new.append([ds, wid, known[wid]["name"], h, who, now])
    if ups:
        ws.batch_update(ups, value_input_option="RAW")
    if new:
        ws.append_rows(new, value_input_option="RAW")
    return len(ups) + len(new)


def period_summary(d1: date, d2: date) -> dict:
    if d2 < d1:
        d1, d2 = d2, d1
    if (d2 - d1).days > 92:
        raise ValueError("Период не больше трёх месяцев")
    known = {w["id"]: w for w in workers(include_fired=True)}
    per = {}
    for r in _tabel():
        if r["hours"] and d1 <= r["day"] <= d2:
            w = known.get(r["id"]) or {"name": r["name"] or "?", "area": ""}
            p = per.setdefault(r["id"], {"name": w["name"], "area": w["area"], "hours": 0, "days": 0, "by_day": {}})
            p["hours"] = round(p["hours"] + r["hours"], 2)
            p["days"] += 1
            p["by_day"][r["day"].strftime("%d.%m")] = r["hours"]
    people = sorted(per.values(), key=lambda p: (p["area"].lower(), p["name"].lower()))
    for p in people:
        p["by_day"] = dict(sorted(p["by_day"].items(), key=lambda kv: kv[0][3:] + kv[0][:2]))
    return {"from": d1.isoformat(), "to": d2.isoformat(), "total": round(sum(p["hours"] for p in people), 2),
            "people": people}


def report_send(d1: date, d2: date, who: str) -> dict:
    summ = period_summary(d1, d2)
    if not summ["people"]:
        raise ValueError("За этот период в табеле нет часов")
    sheets._tab(REPORTS_TAB, REPORTS_HEADERS).append_row(
        [_stamp(), who, _d(summ["from"]).strftime("%d.%m.%Y"), _d(summ["to"]).strftime("%d.%m.%Y"), summ["total"],
         json.dumps(summ, ensure_ascii=False)], value_input_option="RAW")
    return summ


def reports() -> list:
    out = []
    for i, v in enumerate(sheets._tab(REPORTS_TAB, REPORTS_HEADERS).get_all_values()[1:], start=2):
        v = list(v) + [""] * 6
        try:
            data = json.loads(v[5])
        except ValueError:
            continue
        data.update(id=i, sent=v[0].strip(), who=v[1].strip())
        out.append(data)
    return out[::-1][:60]


# --------------------------------------------------------------- запросы
ROLE_TABS = {"manager": ["courier", "prod", "keeper", "manager", "chat"], "courier": ["courier", "chat"],
             "prod": ["prod", "chat"], "keeper": ["keeper", "chat"],
             "techdir": ["tech", "prod", "keeper", "chat"],
             "gendir": ["courier", "prod", "keeper", "manager", "reports", "chat"]}


def _int(x, default: int) -> int:
    try:
        return int(float(str(x).replace(",", ".")))
    except (TypeError, ValueError):
        return default


def _num(x) -> float:
    try:
        return max(0.0, float(str(x or 0).replace(",", ".").replace(" ", "")))
    except ValueError:
        return 0.0


def blanks_post(body: dict, who: str) -> tuple:
    """Добавить (sign=in) или убрать заготовки: {width: 160 или "common", sign, items: [{name, qty}]}."""
    b = sheets.blanks_get()
    common = str(body.get("width")) == "common"
    width = None if common else _int(body.get("width"), 0)
    names = b["common"] if common else b["per"]
    if not common and width not in b["sizes"]:
        return 400, {"error": "Выберите размер"}
    sign = 1 if body.get("sign") == "in" else -1
    changes = []
    for it in body.get("items") or []:
        n = _int(it.get("qty"), 0)
        if it.get("name") in names and n > 0:
            changes.append((width, it["name"], sign * n))
    if not changes:
        return 400, {"error": "Впишите количество хотя бы у одной детали"}
    res = sheets.blanks_add(changes)
    sheets.blanks_refresh()
    where = "общие" if common else str(width)
    lines = [f"{name}: {before} → {after}" for _, name, before, after in res]
    log.info("Приложение: заготовки %s %s (%s): %s", "приход" if sign > 0 else "расход", where, who, "; ".join(lines))
    return 200, {"ok": True, "message": f"✅ Заготовки {where}, {'добавлено' if sign > 0 else 'убрано'}:\n" + "\n".join(lines)}


def upload_pdf(token: str, filename: str, data: bytes) -> tuple:
    me = devices().get(code_for(token)) if re.fullmatch(r"[A-Za-z0-9_-]{32,128}", token or "") else None
    if not me or me["role"] != "manager":
        return 403, {"error": "Загружать файлы может только менеджер"}
    if not data.startswith(b"%PDF"):
        return 400, {"error": "Нужен именно PDF-файл"}
    said = []
    with _lock:
        ok = _deps["pdf"]("", filename or "file.pdf", lambda text, rows=None: said.append(text),
                          f"{me['name'] or 'менеджер'} (приложение)", None, data)
    msg = "\n".join(t for t in said if not t.startswith("Читаю файл"))
    return (200, {"ok": True, "message": msg}) if ok else (400, {"error": msg or "Не получилось прочитать файл"})


def api(method: str, path: str, token: str, body: dict) -> tuple:
    """(код ответа, данные)."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", token or ""):
        return 400, {"error": "Нет ключа телефона"}
    code = code_for(token)
    me = devices().get(code)
    if path == "/api/me":
        if not me:
            return 200, {"code": code, "ok": False}
        return 200, {"code": code, "ok": True, "name": me["name"], "role": me["role"],
                     "label": ROLE_LABELS[me["role"]], "tabs": ROLE_TABS[me["role"]]}
    if not me:
        return 403, {"error": "Телефон не добавлен", "code": code}
    tabs = ROLE_TABS[me["role"]]
    pay = code in pay_codes()
    who = me["name"] or "сотрудник"
    today = now_date()

    if path == "/api/chat":
        return 200, chat_list(_int(body.get("since"), 0), code)
    if method == "POST" and path == "/api/chat/edit":
        try:
            return 200, chat_edit(_int(body.get("id"), 0), str(body.get("text") or ""), code)
        except PermissionError as e:
            return 400, {"error": str(e)}
        except ValueError as e:
            return 400, {"error": str(e)}
    if method == "POST" and path == "/api/chat/send":
        try:
            return 200, chat_send(str(body.get("text") or ""), me, code)
        except ValueError as e:
            return 400, {"error": str(e)}

    if "tech" in tabs and path.startswith("/api/tech"):
        try:
            if path == "/api/tech":
                return 200, tech_data(_d(body.get("day")) if body.get("day") else today)
            if path == "/api/tech/workers":
                return 200, {"workers": workers()}
            if path == "/api/tech/summary":
                return 200, period_summary(_d(body.get("from")), _d(body.get("to")))
            if path == "/api/tech/sent":
                return 200, {"reports": [r for r in reports() if r.get("who") == who][:20]}
            if method == "POST":
                with _lock:
                    if path == "/api/tech/day":
                        n = tech_day_save(_d(body.get("day")), body.get("hours") or {}, who)
                        return 200, {"ok": True, "changed": n}
                    if path == "/api/tech/worker":
                        worker_add(str(body.get("name") or ""), str(body.get("area") or ""), who)
                        return 200, {"ok": True}
                    if path == "/api/tech/worker/remove":
                        worker_fire(str(body.get("id") or ""))
                        return 200, {"ok": True}
                    if path == "/api/tech/send":
                        summ = report_send(_d(body.get("from")), _d(body.get("to")), who)
                        return 200, {"ok": True, "total": summ["total"]}
        except ValueError as e:
            return 400, {"error": str(e)}
    if path == "/api/reports" and "reports" in tabs:
        return 200, {"reports": reports()}

    if path == "/api/courier" and "courier" in tabs:
        return 200, courier_data(today, pay)
    if path == "/api/prod" and "prod" in tabs:
        return 200, prod_data(today)
    if path == "/api/keeper" and "keeper" in tabs:
        return 200, keeper_data()
    if path == "/api/manager/orders" and "manager" in tabs:
        return 200, open_orders(today)
    if path == "/api/manager" and "manager" in tabs:
        return 200, manager_data(today, pay)

    if method == "POST" and path in ("/api/courier/done", "/api/courier/move") and "courier" in tabs:
        kind, key = body.get("kind"), str(body.get("key") or "")
        with _lock:
            orders = _find(today, kind, key)
            if not orders:
                return 409, {"error": "Уже обработано"}
            if path.endswith("done"):
                marked = _deps["deliver"](orders, f"{who} (приложение)", today)
                return 200, {"ok": True, "message": f"✅ Доставлено: {sum(logic.qty(o) for o in marked)} шт." if marked else "Уже отмечено"}
            try:
                day = date.fromisoformat(str(body.get("date") or ""))
            except ValueError:
                return 400, {"error": "Не понял дату"}
            if day <= today:
                return 400, {"error": "Выберите дату позже сегодняшней"}
            new = _deps["move"](orders, day)
        note = f"📅 {who} (приложение) перенёс доставку на {new}: " + ", ".join(f"№ {o['order_no']}" for o in orders[:5])
        if (orders[0].get("address") or "").strip():
            note += "\n" + logic.short(orders[0]["address"], 70)
        try:
            _deps["notify"](note)
        except Exception:
            log.exception("Приложение: не смог уведомить менеджеров о переносе")
        return 200, {"ok": True, "message": f"📅 Перенесено на {new}"}

    if path == "/api/courier/pay" and "courier" in tabs and pay:
        return 200, courier_pay(today)

    if method == "POST" and path == "/api/courier/return" and "courier" in tabs:
        kind = RETURN_KINDS.get(body.get("kind"))
        code = str(body.get("code") or "")
        legs = str(body.get("legs") or "30")
        qty = _int(body.get("qty"), 1)
        if not kind or code not in config.STOCK_CODES or legs not in ("25", "30", "35") or not 1 <= qty <= 99:
            return 400, {"error": "Выберите площадку, размер, ножки и количество"}
        w, c = code.split(".")
        with _lock:
            ok, lines = _deps["returns"](f"{w}/{legs} {'ч' if c == '04' else 'б'} {qty}", kind, f"{who} (приложение)")
        return (200 if ok else 400), ({"ok": True, "message": "\n".join(lines)} if ok else {"error": "\n".join(lines)})

    if method == "POST" and path.startswith("/api/prod/") and "prod" in tabs:
        with _lock:
            if path == "/api/prod/shipped":
                return 200, {"ok": True, "message": _deps["shipped"](f"{who} (приложение)")}
            if path == "/api/prod/blanks":
                return blanks_post(body, who)
            delta = _int(body.get("delta"), 0)
            if not delta or abs(delta) > 999:
                return 400, {"error": "Укажите количество"}
            if path == "/api/prod/stock":
                code = str(body.get("code") or "")
                if code not in config.STOCK_CODES:
                    return 400, {"error": "Нет такого размера"}
                return 200, {"ok": True, "message": _deps["stock"](code, delta, f"{who} (приложение)")}
            if path == "/api/prod/legs":
                height = _int(body.get("height"), 0)
                kind = "painted" if body.get("kind") == "painted" else "welded"
                if height not in sheets.legs_get(kind):
                    return 400, {"error": "Нет такой высоты ножек"}
                color = {"black": "04", "white": "06"}.get(body.get("color"), "")
                try:
                    new = sheets.legs_add(height, delta, kind, color)
                except ValueError as e:
                    return 400, {"error": str(e)}
                title = "Окрашенные ножки" if kind == "painted" else "Ножки"
                cname = {"04": " чёрные", "06": " белые"}.get(color, "")
                msg = f"✅ {title} {height}{cname}: {'+' if delta > 0 else ''}{delta} → теперь {new} шт."
                if kind == "painted" and delta > 0:  # покрасили — столько же уходит из сваренных
                    try:
                        left = sheets.legs_add(height, -delta, "welded")
                        msg += f"\n🔩 Сваренных ножек {height} списано {delta}, осталось {left} шт."
                    except Exception:
                        log.exception("Приложение: не смог списать сваренные ножки")
                log.info("Приложение: %s %s %+d -> %d (%s)", title, height, delta, new, who)
                return 200, {"ok": True, "message": msg}
            if path == "/api/prod/frames":
                width = _int(body.get("width"), 0)
                if width not in sheets.frame_widths():
                    return 400, {"error": "Нет такого размера"}
                return 200, {"ok": True, "message": "✅ " + "\n".join(_deps["frames"]([(width, delta)], f"{who} (приложение)"))}

    if method == "POST" and path.startswith("/api/keeper/") and "keeper" in tabs:
        kind = body.get("kind")
        if kind not in config.INV_TABS:
            return 400, {"error": "Выберите раздел склада"}
        with _lock:
            if path == "/api/keeper/new":
                err = _deps["inv_create"](kind, str(body.get("name") or "")[:60], str(body.get("unit") or "")[:15],
                                          _num(body.get("min")))
                return (400, {"error": err}) if err else (200, {"ok": True, "message": "✅ Позиция добавлена"})
            if path == "/api/keeper/move":
                n = _num(body.get("qty"))
                if n <= 0:
                    return 400, {"error": "Укажите количество больше нуля"}
                item = next((i for i in sheets.inv_get(kind)
                             if i["row"] == _int(body.get("row"), 0) and i["name"] == body.get("name")), None)
                if not item:
                    return 409, {"error": "Список поменялся, обновите экран"}
                done, low = _deps["inv_apply"](kind, [(item, n)], 1 if body.get("sign") == "in" else -1)
                return 200, {"ok": True, "message": "✅ " + "\n".join(done + low)}

    if method == "POST" and path == "/api/manager/device" and "manager" in tabs:
        new_code, role = norm_code(body.get("code")), body.get("role")
        if not new_code:
            return 400, {"error": "Код должен быть из 8 знаков, как на экране телефона: K7QM-2XPA"}
        if role not in ROLE_LABELS:
            return 400, {"error": "Выберите роль"}
        with _lock:
            add_device(new_code, role, str(body.get("name") or "").strip()[:40], who)
        return 200, {"ok": True}
    if method == "POST" and path == "/api/manager/device/remove" and "manager" in tabs:
        rm = norm_code(body.get("code"))
        if rm == code:
            return 400, {"error": "Себя удалить нельзя"}
        with _lock:
            remove_device(rm)
        return 200, {"ok": True}
    return 404, {"error": "Нет такого раздела"}


class Handler(BaseHTTPRequestHandler):
    def _body(self) -> bytes:
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            out = b""
            while True:
                size = int((self.rfile.readline() or b"0").split(b";")[0].strip() or b"0", 16)
                if size == 0:
                    self.rfile.readline()
                    return out
                out += self.rfile.read(size)
                self.rfile.readline()
        return self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 100_000))

    def _send(self, code: int, data: bytes, ctype: str, cache: str = "no-store") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(data)

    def _json(self, code: int, payload: dict) -> None:
        self._send(code, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _api(self, method: str, body: dict) -> None:
        path = self.path.split("?")[0].rstrip("/")
        try:
            code, data = api(method, path, self.headers.get("X-Token") or "", body)
        except Exception as e:
            if "429" in str(e):  # Google: слишком много чтений за минуту — подождать и повторить
                log.warning("Приложение: лимит Google в %s", path)
                code, data = 503, {"error": "Таблица Google перегружена, повторите через минуту"}
            else:
                log.exception("Приложение: ошибка в %s", path)
                code, data = 500, {"error": "Не получилось открыть таблицу, попробуйте ещё раз"}
        self._json(code, data)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path.startswith("/api/"):
            from urllib.parse import parse_qsl, urlsplit
            return self._api("GET", dict(parse_qsl(urlsplit(self.path).query)))
        if path == "/sw.js":
            return self._send(200, SW_JS, "text/javascript; charset=utf-8", "no-cache")
        if path in ("/health", "/ok"):
            return self._send(200, b"ok", "text/plain")
        if path.startswith("/alice"):
            return self._send(200, b"ok", "text/plain")  # проверка «жив ли адрес»
        name, ctype = STATIC.get(path, STATIC["/"])
        try:
            data = (HERE / name).read_bytes()
        except OSError:
            return self._send(200, b"ok", "text/plain")
        self._send(200, data, ctype, "no-cache")

    def do_POST(self):
        path = self.path.split("?")[0].rstrip("/")
        if path.startswith("/alice/"):
            import alice
            if not config.ALICE_SECRET or path != f"/alice/{config.ALICE_SECRET}":
                log.warning("Алиса: запрос на неверный адрес — проверьте секрет в адресе навыка")
                return self._json(404, {"error": "not found"})
            try:
                body = json.loads(self._body() or b"{}")
                res = alice.handle(body)
                log.info("Алиса: запрос «%s» -> ответ отправлен", ((body.get("request") or {}).get("command") or "")[:60])
                return self._json(200, res)
            except Exception:
                log.exception("Алиса: не смог обработать запрос")
                return self._json(200, {"response": {"text": "Что-то пошло не так, попробуйте ещё раз.",
                                                     "end_session": False}, "version": "1.0"})
        if path == "/api/manager/pdf":
            size = int(self.headers.get("Content-Length") or 0)
            if size > MAX_PDF:
                return self._json(400, {"error": "Файл больше 20 МБ"})
            data = self.rfile.read(size)
            from urllib.parse import unquote
            try:
                code, res = upload_pdf(self.headers.get("X-Token") or "", unquote(self.headers.get("X-Filename") or ""), data)
            except Exception:
                log.exception("Приложение: загрузка PDF")
                code, res = 500, {"error": "Не получилось обработать файл"}
            return self._json(code, res)
        if path.startswith("/api/"):
            try:
                body = json.loads(self._body() or b"{}")
            except ValueError:
                body = {}
            return self._api("POST", body if isinstance(body, dict) else {})
        self._json(404, {"error": "not found"})

    def log_message(self, *a):
        pass


def start(**hooks) -> None:
    """Запускает веб-сервер в отдельном потоке: приложение на /, Алиса на /alice/<секрет>.
    hooks — функции бота, которые делают то же, что кнопки в MAX."""
    _deps.update(hooks)
    srv = ThreadingHTTPServer(("0.0.0.0", config.ALICE_PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log.info("Приложение для сотрудников: слушаю порт %d", config.ALICE_PORT)
