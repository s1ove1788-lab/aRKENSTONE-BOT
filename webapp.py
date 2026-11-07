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
ROLE_LABELS = {**config.ROLE_LABELS, "techdir": "Тех. директор", "gendir": "Ген. директор", "admin": "Администратор"}
ROLE_WORDS = {"администратор": "admin", "админ": "admin","техдиректор": "techdir", "тех.директор": "techdir", "технический": "techdir",
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


# Администратор видит всё и один может менять роли сотрудников. Первого администратора задаёт владелец хостинга:
# переменная ADMIN_CODES на Bothost (коды телефонов через запятую). Дальше администратор может назначать
# других администраторов в приложении. Менеджер и команда /app в MAX администратора назначить не могут.
def admin_codes() -> set:
    import os
    return {c for c in (norm_code(x) for x in os.getenv("ADMIN_CODES", "").split(",")) if c}


def role_of(code: str, dev: dict) -> str:
    return "admin" if code in admin_codes() else dev["role"]


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
CHAT_HEADERS = ["Время", "Код", "Имя", "Роль", "Сообщение", "Изменено", "Ответ на", "Фото", "Удалено"]
CHAT_KEEP = 500        # сколько последних сообщений держим в памяти и отдаём телефону
CHAT_MAX_LEN = 1000    # длина одного сообщения
# rev — номер последнего изменения в чате. Телефон присылает свой rev и получает только то,
# что появилось или поменялось после него (новые и отредактированные сообщения).
_chat = {"loaded": False, "msgs": [], "rev": 0, "epoch": str(int(time.time()))}
_chat_lock = threading.Lock()
MAX_PHOTO = 8 * 1024 * 1024


def photo_dir() -> Path:
    """Куда класть фото из чата. /app/shared — постоянное хранилище Bothost (включается кнопкой на карточке бота),
    оно переживает обновления. Без него фото лежат в папке бота и могут пропасть при обновлении из Git."""
    import os
    d = os.getenv("CHAT_PHOTO_DIR") or ("/app/shared/chat_photos" if Path("/app/shared").is_dir() else str(HERE / "chat_photos"))
    p = Path(d)
    p.mkdir(parents=True, exist_ok=True)
    return p


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
        v = list(v) + [""] * 9
        text, photo = v[4].strip(), v[7].strip()
        if (not text and not photo) or v[8].strip():  # пустое или удалённое
            continue
        msgs.append({"id": i, "t": v[0].strip(), "code": norm_code(v[1]), "name": v[2].strip() or "сотрудник",
                     "role": labels.get(v[3].strip().lower(), ""), "text": text, "edited": v[5].strip(),
                     "reply": _int(v[6], 0), "photo": photo})
    msgs = msgs[-CHAT_KEEP:]
    for m in msgs:
        _chat_bump(m)
    _chat.update(loaded=True, msgs=msgs)


def chat_list(since: int, code: str) -> dict:
    with _chat_lock:
        _chat_load()
        msgs = [m for m in _chat["msgs"] if m["rev"] > since]
        if since <= 0:  # телефон загружает чат с нуля — удалённые ему не нужны
            msgs = [m for m in msgs if not m.get("deleted")]
        rev = _chat["rev"]
        by_id = {m["id"]: m for m in _chat["msgs"]}

    def quote(rid):
        if not rid:
            return None
        q = by_id.get(rid)
        if not q or q.get("deleted"):
            return {"id": rid, "name": "", "text": "Сообщение удалено", "photo": False}
        return {"id": rid, "name": q["name"], "text": q["text"][:140], "photo": bool(q.get("photo"))}

    out = []
    for m in msgs:
        if m.get("deleted"):  # телефоны, у которых это сообщение уже есть, уберут его
            out.append({"id": m["id"], "deleted": True})
            continue
        out.append({"id": m["id"], "t": m["t"], "name": m["name"], "role": m["role"],
                    "label": ROLE_LABELS.get(m["role"], ""), "text": m["text"],
                    "edited": bool(m.get("edited")), "mine": m["code"] == code,
                    "reply": quote(m.get("reply")), "photo": m.get("photo") or ""})
    return {"epoch": _chat["epoch"], "rev": rev, "msgs": out}


def _clean(text: str, empty_ok: bool = False) -> str:
    text = re.sub(r"\n{3,}", "\n\n", (text or "").replace("\r", "")).strip()[:CHAT_MAX_LEN]
    if not text and not empty_ok:
        raise ValueError("Пустое сообщение")
    return text


def chat_send(text: str, me: dict, code: str, reply: int = 0, photo: str = "") -> dict:
    text = _clean(text, empty_ok=bool(photo))
    now = datetime.now(ZoneInfo(config.TIMEZONE)).strftime("%d.%m.%Y %H:%M")
    name = me["name"] or "сотрудник"
    with _chat_lock:
        _chat_load()
        # RAW: таблица сохраняет текст как есть, «=2+2» или «+7…» не превратятся в формулу
        if reply and not any(m["id"] == reply and not m.get("deleted") for m in _chat["msgs"]):
            reply = 0
        res = _chat_ws().append_row([now, code, name, ROLE_LABELS[me["role"]], text, "", reply or "", photo],
                                    value_input_option="RAW")
        # номер строки, куда легло сообщение, — по нему потом находим сообщение для правки
        hit = re.search(r"!A(\d+)", str(((res or {}).get("updates") or {}).get("updatedRange") or ""))
        new_id = int(hit.group(1)) if hit else ((_chat["msgs"][-1]["id"] + 1) if _chat["msgs"] else 2)
        m = {"id": new_id, "t": now, "code": code, "name": name, "role": me["role"], "text": text, "edited": "",
             "reply": reply, "photo": photo}
        _chat_bump(m)
        _chat["msgs"].append(m)
        del _chat["msgs"][:-CHAT_KEEP]
        replied = next((x["code"] for x in _chat["msgs"] if reply and x["id"] == reply), None)
    _chat_push(name, text, photo, code, replied)
    return {"ok": True, "id": new_id}


def _chat_push(name: str, text: str, photo: str, author: str, replied) -> None:
    try:
        body = (("📷 " if photo else "") + (text or ("Фото" if photo else ""))).strip()
        everyone = _codes(skip=author)
        if replied and replied != author and replied in everyone:
            push_safe("chat", f"↩️ {name} ответил(а) вам", body, {replied}, "chat")
            everyone.discard(replied)
        with _push_lock:
            _push_load()
            muted = {c for c, d in _push["devs"].items() if not d["chat"]}
        push_safe("chat", f"💬 {name}", body, everyone - muted, "chat")
    except Exception:
        log.exception("Уведомления: чат")


def chat_edit(msg_id: int, text: str, code: str) -> dict:
    """Правка своего сообщения (у фото — подписи). Чужие сообщения менять нельзя."""
    with _chat_lock:
        _chat_load()
        m = next((x for x in _chat["msgs"] if x["id"] == msg_id and not x.get("deleted")), None)
        if not m:
            raise ValueError("Сообщение слишком старое или удалено")
        text = _clean(text, empty_ok=bool(m.get("photo")))
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
            cur = devices(force=True).get(code)
            if cur and role_of(code, cur) == "admin":
                return "Администратора убирает только администратор в приложении."
            return f"🗑 {code} удалён из приложения." if code and remove_device(code) else "Такого кода нет в списке."
        if len(parts) > 1 and not norm_code(parts[0]) and norm_code(parts[0] + parts[1]):
            parts = [parts[0] + parts[1]] + parts[2:]  # код написали через пробел: «K7QM 2XPA»
        code = norm_code(parts[0])
        role = _parse_role(parts[1]) if len(parts) > 1 else None
        if not code or not role:
            return "Не понял. " + usage
        name = " ".join(parts[2:])[:40]
        cur = devices(force=True).get(code)
        if role == "admin" or (cur and role_of(code, cur) == "admin"):
            return "Администратора назначает и меняет только администратор в приложении."
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


def _find(today: date, kind: str, key: str, early: bool = False) -> list:
    """Заказы для «Доставлен» / «Перенести». По России — только отгрузки на сегодня (день в день).
    Город при early=True — любой недоставленный городской заказ: его можно отметить раньше срока доставки."""
    orders = sheets.read_orders(force=True)
    items = logic.courier_items(orders, today)
    if kind == "ru":
        g = next((g for g in logic.russia_groups(items) if g["key"] == key), None)
        return g["orders"] if g else []
    found = [o for o, _ in items if o["order_no"] == key and logic.is_city(o)][:1]
    if not found and early and kind == "city":
        found = [o for o in orders if not o["status"] and o["order_no"] == key and logic.is_city(o)][:1]
    return found


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


BOARD_CLOSED_MAX = 150  # сколько доставленных и отменённых отдаём в каждую вкладку (самые свежие)


def _board_tab(o: dict, r, today: date) -> str:
    st = (o["status"] or "").strip().lower().replace("ё", "е")
    if "отмен" in st or "возврат" in st:
        return "cancel"
    if st:
        return "done"
    if r is None:
        return "today" if logic.is_city(o) else "ahead"  # дату не поняли: городской лучше показать сразу
    if r[1] < today:
        return "late"
    return "today" if r[0] <= today else "ahead"


def open_orders(today: date) -> dict:
    """Все заказы для экрана «Заказы» (как в кабинете Маркета): Город / По России и вкладки
    Сегодня · Впереди · Просрочены · Доставлены · Отмены. Отгрузки по России склеивает телефон."""
    rows, closed = [], {}
    for o in sheets.read_orders():
        r = logic.key_range(o, today)
        city = logic.is_city(o)
        tab = _board_tab(o, r, today)
        a = logic.parse_article(o["article"])
        width = int(a["width"]) if a else int((re.match(r"\d+", o["size"]) or [0])[0] or 0)
        color = (a["color_code"] if a else {"чёрный": "04", "черный": "04", "белый": "06"}.get(o["color"].lower(), ""))
        item = {
            "id": o["row"], "tab": tab, "city": city, "channel": o["channel"], "order_no": o["order_no"],
            "title": logic.article_name(o["article"], logic.item_line(o)), "qty": logic.qty(o),
            "width": width, "color": {"04": "black", "06": "white"}.get(color, ""), "article": o["article"],
            "added": o["added"], "ship": o["ship"], "delivery": o["delivery"],
            "sort": str(r[0]) if r else "", "end": str(r[1]) if r else "",
            "status": o["status"] or "", "address": o["address"] if city else "",
            "shipment": "" if city else o["phone"], "phone": logic.fmt_phone(o["phone"]) if city else "",
            "tel": re.sub(r"[^\d+]", "", (o["phone"] or "").split("доб")[0].split(",")[0]) if city else "",
        }
        if tab in ("done", "cancel"):
            closed.setdefault((tab, city), []).append(item)
        else:
            rows.append(item)
    for items in closed.values():  # из закрытых — только самые свежие, иначе за год экран станет неподъёмным
        items.sort(key=lambda x: (x["end"] or "", x["id"]), reverse=True)
        rows += items[:BOARD_CLOSED_MAX]
    rows.sort(key=lambda x: (x["sort"] or "9999", x["channel"], x["shipment"], x["address"], x["id"]))
    return {"rows": rows, "today": str(today), "url": sheets.sheet_url("orders")}


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
        "devices": [{"code": c, "name": d["name"], "role": role_of(c, d), "label": ROLE_LABELS[role_of(c, d)],
                     "fixed": c in admin_codes()}
                    for c, d in devices(force=True).items()],
    }


def chat_delete(msg_id: int, code: str, is_admin: bool, who: str) -> dict:
    """Удаляет сообщение у всех. Своё — любой сотрудник, чужое — только администратор.
    В таблице строка остаётся (номера строк не сдвигаются), но текст и фото стираются, а в «Удалено» — кто и когда."""
    with _chat_lock:
        _chat_load()
        m = next((x for x in _chat["msgs"] if x["id"] == msg_id and not x.get("deleted")), None)
        if not m:
            raise ValueError("Сообщение уже удалено или слишком старое")
        if m["code"] != code and not is_admin:
            raise PermissionError("Удалять чужие сообщения может только администратор")
        ws = _chat_ws()
        row = ws.row_values(msg_id) + [""] * 9
        if norm_code(row[1]) != m["code"] or row[0].strip() != m["t"]:
            raise ValueError("В таблице чата поменялись строки, сообщение не удалено")
        mark = f"{_stamp()} · {who}" + (" (админ)" if m["code"] != code else "")
        ws.update(values=[[""]], range_name=f"E{msg_id}", value_input_option="RAW")
        ws.update(values=[["", mark]], range_name=f"H{msg_id}:I{msg_id}", value_input_option="RAW")
        photo = m.get("photo")
        m.update(deleted=True, text="", photo="")
        _chat_bump(m)
    if photo and re.fullmatch(r"[A-Za-z0-9_-]{20,40}\.(jpg|png|webp)", photo):
        try:
            (photo_dir() / photo).unlink()
        except OSError:
            pass
    return {"ok": True}


PHOTO_KINDS = {b"\xff\xd8\xff": ".jpg", b"\x89PNG": ".png", b"RIFF": ".webp"}


def chat_photo(token: str, data: bytes, caption: str, reply: int) -> tuple:
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", token or ""):
        return 400, {"error": "Нет ключа телефона"}
    code = code_for(token)
    me = devices().get(code)
    if not me:
        return 403, {"error": "Телефон не добавлен"}
    ext = next((e for sig, e in PHOTO_KINDS.items() if data.startswith(sig)), None)
    if not ext or (ext == ".webp" and data[8:12] != b"WEBP"):
        return 400, {"error": "Это не фото (нужен JPG, PNG или WEBP)"}
    name = secrets.token_urlsafe(18) + ext  # длинное случайное имя: по ссылке фото не подобрать
    (photo_dir() / name).write_bytes(data)
    try:
        return 200, chat_send(caption, {**me, "role": role_of(code, me)}, code, reply, name)
    except ValueError as e:
        return 400, {"error": str(e)}


# ------------------------------------------------------------ уведомления на телефон (APK)
# Как это устроено:
#  • Событие (новый заказ, сообщение в чате, ответ, отчёт табеля) кладётся в очередь «входящих» нужных телефонов.
#  • Телефонам с Firebase сразу отправляется push — приходит за секунды, даже если приложение закрыто.
#  • Запасной путь для всех: APK раз в 15 минут сам забирает свои входящие (/api/push/inbox) — так уведомления
#    доходят и до телефонов без сервисов Google. Номер события растёт со временем, повтора не будет.
# Firebase включается переменной FIREBASE_CREDENTIALS_JSON на Bothost (ключ сервисного аккаунта Firebase).
PUSH_TAB = "Уведомления"
PUSH_HEADERS = ["Код", "Имя", "Firebase", "Чат", "Обновлено"]
PUSH_KEEP_SEC = 2 * 24 * 3600
_push = {"loaded": False, "devs": {}, "events": [], "seq": 0, "fcm": {"creds": None, "project": "", "off": False}}
_push_lock = threading.Lock()


def _push_ws():
    return sheets._tab(PUSH_TAB, PUSH_HEADERS)


def _push_load() -> None:
    if _push["loaded"]:
        return
    devs = {}
    for i, v in enumerate(_push_ws().get_all_values()[1:], start=2):
        v = list(v) + [""] * 5
        c = norm_code(v[0])
        if c:
            devs[c] = {"fcm": v[2].strip(), "chat": v[3].strip().lower() != "выкл", "row": i}
    _push.update(loaded=True, devs=devs)


def _push_save(code: str) -> None:
    d = _push["devs"][code]
    name = (devices().get(code) or {}).get("name", "")
    row = [code, name, d["fcm"], "вкл" if d["chat"] else "выкл", _stamp()]
    ws = _push_ws()
    if d.get("row"):
        ws.update(values=[row], range_name=f"A{d['row']}:E{d['row']}", value_input_option="RAW")
    else:
        res = ws.append_row(row, value_input_option="RAW")
        hit = re.search(r"!A(\d+)", str(((res or {}).get("updates") or {}).get("updatedRange") or ""))
        d["row"] = int(hit.group(1)) if hit else 0


def push_register(code: str, fcm: str = None, chat: bool = None) -> dict:
    with _push_lock:
        _push_load()
        d = _push["devs"].setdefault(code, {"fcm": "", "chat": True, "row": 0})
        changed = not d.get("row")
        if fcm is not None and fcm != d["fcm"]:
            d["fcm"], changed = fcm[:400], True
        if chat is not None and chat != d["chat"]:
            d["chat"], changed = bool(chat), True
        if changed:
            _push_save(code)
        return {"ok": True, "chat": d["chat"], "firebase": bool(d["fcm"]), "server_push": fcm_enabled()}


def push_inbox(code: str, after: int) -> dict:
    with _push_lock:
        evs = [e for e in _push["events"] if code in e["codes"] and e["id"] > after]
        last = _push["seq"]
    if after <= 0:  # первый запрос после установки: старое не показываем, только запоминаем, с какого места ждать
        evs = []
    return {"last": last, "events": [{k: e[k] for k in ("id", "kind", "title", "body", "tab")} for e in evs[-20:]]}


def _codes(roles=None, skip=None) -> set:
    """Коды телефонов по ролям (с учётом администраторов). roles=None — все."""
    out = set()
    for c, d in devices().items():
        if c == skip:
            continue
        if roles is None or role_of(c, d) in roles:
            out.add(c)
    return out


def push_event(kind: str, title: str, body: str, codes: set, tab: str = "") -> None:
    if not codes:
        return
    with _push_lock:
        _push["seq"] = max(int(time.time() * 1000), _push["seq"] + 1)
        ev = {"id": _push["seq"], "kind": kind, "title": title[:120], "body": body[:400], "tab": tab,
              "codes": set(codes), "t": time.time()}
        _push["events"].append(ev)
        cut = time.time() - PUSH_KEEP_SEC
        _push["events"] = [e for e in _push["events"] if e["t"] > cut][-500:]
    if fcm_enabled():
        threading.Thread(target=_fcm_send_all, args=(ev,), daemon=True).start()


def push_safe(*a, **k) -> None:
    """То же, что push_event, но никогда не роняет основное действие (отправку сообщения, запись заказа)."""
    try:
        push_event(*a, **k)
    except Exception:
        log.exception("Уведомления: не смог поставить событие")


def push_order_text(text: str) -> None:
    """Вызывается из рассылки бота: уведомления о новых заказах получают все роли."""
    t = (text or "").strip()
    if not t.startswith(("🛒", "🆕")):
        return
    head, _, rest = t.partition("\n")
    push_safe("orders", head, rest.strip() or head, _codes(), "")


# ---------- Firebase Cloud Messaging (HTTP v1)
def fcm_enabled() -> bool:
    if _push["fcm"]["off"]:
        return False
    if _push["fcm"]["creds"] is None:
        import os
        raw = os.getenv("FIREBASE_CREDENTIALS_JSON", "").strip()
        if not raw:
            _push["fcm"]["off"] = True
            return False
        try:
            if not raw.startswith("{"):
                raw = base64.b64decode(raw).decode("utf-8")
            info = json.loads(raw)
            from google.oauth2 import service_account
            _push["fcm"]["creds"] = service_account.Credentials.from_service_account_info(
                info, scopes=["https://www.googleapis.com/auth/firebase.messaging"])
            _push["fcm"]["project"] = info["project_id"]
            log.info("Уведомления: Firebase подключён (проект %s)", info["project_id"])
        except Exception:
            log.exception("Уведомления: не смог прочитать FIREBASE_CREDENTIALS_JSON — push выключен, работает запасная проверка")
            _push["fcm"]["off"] = True
            return False
    return True


def _fcm_token() -> str:
    creds = _push["fcm"]["creds"]
    if not creds.valid:
        from google.auth.transport.requests import Request
        creds.refresh(Request())
    return creds.token


def _fcm_send_all(ev: dict) -> None:
    import requests
    with _push_lock:
        targets = [(c, _push["devs"][c]["fcm"]) for c in ev["codes"]
                   if c in _push["devs"] and _push["devs"][c]["fcm"]]
    if not targets:
        return
    try:
        auth = {"Authorization": "Bearer " + _fcm_token()}
    except Exception:
        log.exception("Уведомления: Firebase не выдал ключ доступа")
        return
    url = f"https://fcm.googleapis.com/v1/projects/{_push['fcm']['project']}/messages:send"
    data = {"id": str(ev["id"]), "kind": ev["kind"], "title": ev["title"], "body": ev["body"], "tab": ev["tab"]}
    dead = []
    for code, tok in targets:
        # Только данные, без блока notification: показывает уведомление само приложение. Так оно помнит,
        # что уже показало, и запасная проверка раз в 15 минут не покажет то же самое второй раз.
        msg = {"message": {"token": tok, "data": data, "android": {"priority": "HIGH", "ttl": "86400s"}}}
        try:
            r = requests.post(url, json=msg, headers=auth, timeout=15)
            if r.status_code in (400, 404) and ("UNREGISTERED" in r.text or "INVALID_ARGUMENT" in r.text and "token" in r.text):
                dead.append(code)
            elif r.status_code >= 300:
                log.warning("Уведомления: Firebase ответил %s: %s", r.status_code, r.text[:200])
        except Exception as e:
            log.warning("Уведомления: Firebase недоступен: %s", e)
    for code in dead:  # приложение удалили или переустановили — старый адрес больше не нужен
        try:
            push_register(code, fcm="")
        except Exception:
            pass


# ------------------------------------------------------------ обновления APK
# Новая версия приложения — это новый выпуск (Release) на GitHub, его собирает сборщик из ветки android.
# Сервер узнаёт о нём сам: приложение спрашивает /app/version и предлагает «Обновить», а раз в 30 минут
# сервер проверяет GitHub и, если вышла новая версия, присылает всем уведомление.
APP_REPO = "s1ove1788-lab/aRKENSTONE-BOT"
_app_ver = {"t": 0.0, "data": None}


def app_version(force: bool = False):
    """{"code": 6, "version": "1.6", "url": "...apk", "notes": "..."} или None, если GitHub недоступен."""
    import os
    if not force and _app_ver["data"] and time.time() - _app_ver["t"] < 600:
        return _app_ver["data"]
    import requests
    repo = os.getenv("APP_REPO", APP_REPO)
    try:
        r = requests.get(f"https://api.github.com/repos/{repo}/releases/latest", timeout=15,
                         headers={"Accept": "application/vnd.github+json", "User-Agent": "arkenstone-bot"})
        r.raise_for_status()
        j = r.json()
        tag = j.get("tag_name") or ""
        body = (j.get("body") or "").strip()
        apk = next((a for a in j.get("assets") or [] if a.get("name", "").endswith(".apk")), None)
        # Новый формат: тег app-v1.0.5, внутренний номер сборки — строкой «Сборка: 13» в описании.
        # Старый формат: тег app-v1.8, номер сборки — последнее число в теге.
        new = re.fullmatch(r"app-v(\d+\.\d+\.\d+)", tag)
        old = re.fullmatch(r"app-v1\.(\d+)", tag)
        code_hit = re.search(r"Сборка:\s*(\d+)", body)
        if apk and (new and code_hit or old):
            notes = body.split("Что нового:", 1)[1] if "Что нового:" in body else ""  # пишет сборщик из описания изменения
            _app_ver["data"] = {"code": int(code_hit.group(1)) if new else int(old.group(1)),
                                "version": new.group(1) if new else f"1.{old.group(1)}",
                                "url": apk["browser_download_url"], "notes": notes.strip()[:600]}
            _app_ver["t"] = time.time()
    except Exception as e:
        log.warning("Обновления: не смог узнать версию на GitHub: %s", e)
        _app_ver["t"] = time.time() - 480  # повторим через пару минут
    return _app_ver["data"]


def _update_watch() -> None:
    """Раз в 30 минут: вышла новая версия — уведомление всем. Какую уже объявляли, помним в «Настройках»."""
    time.sleep(90)  # дать боту спокойно запуститься
    while True:
        try:
            v = app_version(force=True)
            if v:
                last = _int(sheets.get_setting("app_version_announced", max_age=0), 0)
                if v["code"] > last:
                    if last:  # при самом первом запуске только запоминаем, без уведомления
                        push_safe("update", f"⬆️ Новая версия Arkenstone {v['version']}",
                                  (v["notes"] or "Откройте приложение и нажмите «Обновить»")[:300], _codes(), "")
                        log.info("Обновления: разослал уведомление о версии %s", v["version"])
                    sheets.set_setting("app_version_announced", str(v["code"]))
        except Exception:
            log.exception("Обновления: проверка версии")
        time.sleep(1800)


# ------------------------------------------------------------ табель цеха (тех. директор)
# «Работники цеха» — люди, которым ведут учёт часов. Им не нужно приложение: тех. директор заводит их вручную.
# «Табель» — по строке на человека и день: сколько часов отработал.
# «Отчёты табеля» — итоги за период, которые тех. директор отправил ген. директору (снимок на момент отправки).
WORKERS_TAB = "Работники цеха"
WORKERS_HEADERS = ["ID", "Имя", "Участок", "Добавлен", "Кто добавил", "Статус", "Ставка ₽/ч", "За кровать ₽"]
TABEL_TAB = "Табель"
TABEL_HEADERS = ["Дата", "ID", "Имя", "Часы", "Кто внёс", "Изменено", "Кровати"]
BONUS_TAB = "Премии"
BONUS_HEADERS = ["Номер", "Предложено", "Кто предложил", "ID сотрудника", "Сотрудник", "Сумма ₽", "За что",
                 "Статус", "Решение", "Кто решил", "Комментарий", "Код телефона"]
BONUS_WAIT, BONUS_OK, BONUS_NO = "На согласовании", "Согласовано", "Отклонено"
PAINT_WORDS = ("покрас", "маляр", "окрас", "краск")
REPORTS_TAB = "Отчёты табеля"
REPORTS_HEADERS = ["Отправлено", "Кто отправил", "С", "По", "Всего часов", "Данные", "Ставка ₽/ч", "К выплате ₽"]
DEFAULT_RATE = 625      # ₽ за час, пока ген. директор не поставил другую ставку в приложении
DEFAULT_BED_RATE = 300  # ₽ маляру за покрашенную кровать


# ------------------------------------------------------------ отдельная таблица табеля
# Табель, работники, премии, отчёты с суммами и ставки — в своей Google Таблице, которую видят только
# администратор и тех. директор. Пока её не подключили — листы живут в общей таблице, как раньше.
# Код таблицы: переменная TABEL_SHEET_ID на Bothost или «tabel_sheet_id» на листе «Настройки» (ставит перенос).
RATES_TAB = "Ставки"
RATES_HEADERS = ["Ключ", "Значение"]
TABEL_BOOK_TABS = [(WORKERS_TAB, WORKERS_HEADERS), (TABEL_TAB, TABEL_HEADERS), (BONUS_TAB, BONUS_HEADERS),
                   (REPORTS_TAB, REPORTS_HEADERS)]
_tb = {"id": "", "book": None, "tabs": {}}
_tb_lock = threading.Lock()


def tabel_book_id() -> str:
    import os
    return os.getenv("TABEL_SHEET_ID", "").strip() or sheets.get_setting("tabel_sheet_id")


def tabel_book_url() -> str:
    sid = tabel_book_id()
    return f"https://docs.google.com/spreadsheets/d/{sid}/edit" if sid else ""


def _tt(title: str, headers: list):
    """Лист табеля: из отдельной таблицы, если она подключена, иначе из общей."""
    sid = tabel_book_id()
    if not sid:
        return sheets._tab(title, headers)
    with _tb_lock:
        if _tb["id"] != sid:
            _tb.update(id=sid, book=None, tabs={})
        ws = _tb["tabs"].get(title)
        if ws is None:
            import gspread
            if _tb["book"] is None:
                _tb["book"] = sheets.get_client().open_by_key(sid)
            try:
                raw = _tb["book"].worksheet(title)
            except gspread.WorksheetNotFound:
                raw = _tb["book"].add_worksheet(title=title, rows=200, cols=max(len(headers), 4))
            sheets._ensure_header(raw, headers)
            # свой кэш, не общий: фоновое пакетное чтение общей таблицы ищет листы по названию в ней
            ws = _tb["tabs"][title] = sheets._CachedSheet(raw)
    return ws


def _get_rate_raw(key: str) -> str:
    if not tabel_book_id():
        return sheets.get_setting(key)
    for v in _tt(RATES_TAB, RATES_HEADERS).get_all_values()[1:]:
        if v and v[0].strip() == key:
            return v[1].strip() if len(v) > 1 else ""
    return ""


def _set_rate_raw(key: str, value: str) -> None:
    if not tabel_book_id():
        return sheets.set_setting(key, value)
    ws = _tt(RATES_TAB, RATES_HEADERS)
    rows = ws.get_all_values(fresh=True)
    for i, v in enumerate(rows[1:], start=2):
        if v and v[0].strip() == key:
            ws.update(values=[[value]], range_name=f"B{i}", value_input_option="RAW")
            return
    ws.append_row([key, value], value_input_option="RAW")


def bot_email() -> str:
    """Почта служебного аккаунта бота: её добавляют редактором в таблицу. Берём прямо из ключа Google."""
    try:
        raw = config.CREDS_JSON
        if raw:
            if not raw.startswith("{"):
                raw = base64.b64decode(raw).decode("utf-8")
            return json.loads(raw).get("client_email", "")
        return json.loads(Path(config.CREDS_FILE).read_text(encoding="utf-8")).get("client_email", "")
    except Exception:
        try:
            import costs
            return costs.bot_email()
        except Exception:
            return ""


def tabel_move(sid: str, who: str) -> dict:
    """Переносит листы табеля и ставки из общей таблицы в отдельную (sid).
    Порядок: копируем → перечитываем и сверяем → включаем новую таблицу → только потом убираем листы из общей.
    Если что-то пошло не так до включения, общая таблица остаётся нетронутой."""
    import gspread
    if tabel_book_id():
        raise ValueError("Табель уже в отдельной таблице")
    no_access = (f"У бота нет доступа к этой таблице. Откройте её, нажмите «Настройки доступа» и добавьте "
                 f"{bot_email() or 'почту бота'} с ролью «Редактор», затем нажмите «Подключить» ещё раз")
    try:
        new = sheets.get_client().open_by_key(sid)
    except (PermissionError, gspread.SpreadsheetNotFound):  # gspread так сообщает «нет доступа» / «не найдена»
        raise ValueError(no_access)
    except gspread.exceptions.APIError as e:
        if "403" in str(e) or "404" in str(e) or "PERMISSION" in str(e).upper():
            raise ValueError(no_access)
        raise
    have = {w.title: w for w in new.worksheets()}
    for title, _ in TABEL_BOOK_TABS + [(RATES_TAB, RATES_HEADERS)]:
        if title in have and len([r for r in have[title].get_all_values() if any(c.strip() for c in r)]) > 1:
            raise ValueError(f"В новой таблице уже есть лист «{title}» с данными. Нужна пустая таблица")
    book = sheets.get_book()
    moved, old_sheets = {}, []
    for title, headers in TABEL_BOOK_TABS:
        try:
            old = sheets._find_ws(book, title)
            vals = old.get_all_values()
            old_sheets.append(old)
        except gspread.WorksheetNotFound:
            vals = []
        vals = vals if vals and any(c.strip() for c in vals[0]) else [headers]
        width = max(len(r) for r in vals)
        vals = [list(r) + [""] * (width - len(r)) for r in vals]
        ws = have.get(title) or new.add_worksheet(title=title, rows=max(200, len(vals) + 50), cols=max(width, 4))
        if ws.row_count < len(vals):
            ws.add_rows(len(vals) - ws.row_count + 50)
        if ws.col_count < width:
            ws.add_cols(width - ws.col_count)
        ws.update(values=vals, range_name="A1", value_input_option="RAW")
        back = ws.get_all_values()
        strip = lambda rows: [[c for c in r] for r in rows if any(c.strip() for c in r)]
        if [r[:width] + [""] * (width - len(r[:width])) for r in strip(back)] != strip(vals):
            raise ValueError(f"Лист «{title}» скопировался не полностью, перенос остановлен. Общая таблица не тронута")
        moved[title] = len(vals) - 1
    rates = [[k, sheets.get_setting(k, max_age=0)] for k in ("hour_rate", "bed_rate")]
    rws = have.get(RATES_TAB) or new.add_worksheet(title=RATES_TAB, rows=20, cols=2)
    rws.update(values=[RATES_HEADERS] + [r for r in rates if r[1]], range_name="A1", value_input_option="RAW")
    # пустой «Лист1», который Google создаёт сам, убираем, если он пустой
    for w in new.worksheets():
        if w.title not in moved and w.title != RATES_TAB and not any(any(c.strip() for c in r) for r in w.get_all_values()):
            try:
                new.del_worksheet(w)
            except Exception:
                pass
    # включаем новую таблицу, затем убираем листы и ставки из общей
    sheets.set_setting("tabel_sheet_id", sid)
    with _tb_lock:
        _tb.update(id="", book=None, tabs={})
    for old in old_sheets:
        try:
            book.del_worksheet(old)
        except Exception:
            log.exception("Табель: не смог убрать лист «%s» из общей таблицы", old.title)
        sheets._tabs.pop(old.title, None)
        sheets._ws_map["t"] = 0.0
    for k, v in rates:
        if v:
            sheets.set_setting(k, "")
    log.info("Табель перенесён в отдельную таблицу (%s): %s", who, moved)
    return {"ok": True, "url": tabel_book_url(), "moved": moved}


def tabel_create(emails: list, who: str) -> dict:
    """Бот сам создаёт таблицу, открывает её указанным почтам (редакторы) и переносит туда табель."""
    emails = [e.strip() for e in emails if re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", e.strip())]
    if not emails:
        raise ValueError("Впишите почту Google, которой открыть таблицу")
    try:
        book = sheets.get_client().create("Табель цеха — Arkenstone")
    except Exception as e:
        log.warning("Табель: бот не смог создать таблицу: %s", e)
        raise ValueError("Google не дал боту создать таблицу. Создайте пустую таблицу сами и подключите её "
                         "вторым способом")
    for e in emails:
        book.share(e, perm_type="user", role="writer", notify=True)
    return tabel_move(book.id, who)


def _rate(key: str, default: float) -> float:
    try:
        r = float(str(_get_rate_raw(key) or "").replace(",", ".").strip() or default)
        return r if 0 <= r < 100000 else default
    except (TypeError, ValueError):
        return default


def hour_rate() -> float:
    """Ставка за час: из «Настроек» (hour_rate), иначе 625 ₽."""
    return _rate("hour_rate", DEFAULT_RATE)


def bed_rate() -> float:
    """Маляру за покрашенную кровать: из «Настроек» (bed_rate), иначе 300 ₽."""
    return _rate("bed_rate", DEFAULT_BED_RATE)


def is_painter(area: str) -> bool:
    a = (area or "").lower()
    return any(w in a for w in PAINT_WORDS)


def _money(hours, rate) -> int:
    """Рубли с округлением как в бухгалтерии: 14 062,5 → 14 063 (round в Python округлил бы к чётному)."""
    from decimal import ROUND_HALF_UP, Decimal
    return int((Decimal(str(hours or 0)) * Decimal(str(rate))).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def with_pay(summ: dict, rate: float, brate: float = None, personal: dict = None) -> dict:
    """Деньги за работу: часы × ставка + кровати × ставка за кровать (у маляров) + доплата/вычет из правки отчёта.
    Ставки — личные у сотрудника (personal: {ID: (за час, за кровать)}), иначе общие. Премии добавляются отдельно.
    Ставки человека запоминаются в отчёте (p["rate"]): потом их поменяют — старый отчёт не пересчитается."""
    brate = DEFAULT_BED_RATE if brate is None else brate
    for p in summ["people"]:
        if personal is not None:  # отправка отчёта: личная ставка сотрудника, а если её нет — общая
            own = personal.get(p.get("id")) or (None, None)
            p["rate"] = own[0] if own[0] is not None else rate
            p["bed_rate"] = own[1] if own[1] is not None else brate
        else:  # пересчёт сохранённого отчёта: ставка человека из отчёта, в старых отчётах её нет — ставка отчёта
            p.setdefault("rate", rate)
            p.setdefault("bed_rate", brate)
        p["pay_hours"] = _money(p["hours"], p["rate"])
        p["pay_beds"] = _money(p.get("beds", 0), p["bed_rate"])
        p["pay"] = p["pay_hours"] + p["pay_beds"] + int(p.get("adj") or 0)
    summ["rate"], summ["bed_rate"] = rate, brate
    summ["pay"] = sum(p["pay"] for p in summ["people"])
    return summ


def personal_rates() -> dict:
    return {w["id"]: (w["rate"], w["bed_rate"]) for w in workers(include_fired=True)}


def with_bonuses(summ: dict, bonuses: list) -> dict:
    """Согласованные премии за период отчёта — сверху к сумме (считаются при каждом просмотре:
    премию могут согласовать уже после того, как отчёт отправлен)."""
    d1, d2 = _d(summ["from"]), _d(summ["to"])
    by_id = {}
    for b in bonuses:
        if b["status"] == BONUS_OK and d1 <= b["day"] <= d2:
            by_id.setdefault(b["worker"], []).append(b)
    names = {p.get("id"): p for p in summ["people"]}
    for wid, bl in by_id.items():
        p = names.get(wid)
        if not p:  # премия человеку, у которого нет часов в этом периоде
            p = {"id": wid, "name": bl[0]["name"], "area": "", "hours": 0, "beds": 0, "days": 0, "by_day": {},
                 "pay_hours": 0, "pay_beds": 0, "pay": 0}
            summ["people"].append(p)
            names[wid] = p
        p["bonus"] = sum(b["amount"] for b in bl)
        p["bonus_list"] = [{"amount": b["amount"], "reason": b["reason"]} for b in bl]
    for p in summ["people"]:
        p.setdefault("bonus", 0)
        p["total_pay"] = p.get("pay", 0) + p["bonus"]
    summ["bonus"] = sum(p["bonus"] for p in summ["people"])
    summ["total_pay"] = summ.get("pay", 0) + summ["bonus"]
    return summ
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
    for i, v in enumerate(_tt(WORKERS_TAB, WORKERS_HEADERS).get_all_values()[1:], start=2):
        v = list(v) + [""] * 8
        if not v[0].strip() or not v[1].strip():
            continue
        fired = v[5].strip().lower() == FIRED.lower()
        if fired and not include_fired:
            continue
        out.append({"id": v[0].strip(), "name": v[1].strip(), "area": v[2].strip(), "row": i, "fired": fired,
                    "rate": _rate_cell(v[6]), "bed_rate": _rate_cell(v[7])})
    return sorted(out, key=lambda w: (w["area"].lower(), w["name"].lower()))


def _rate_cell(x):
    """Личная ставка из ячейки или поля: «450», «450,5» -> 450 / 450.5; пусто -> None (берётся общая)."""
    t = str(x if x is not None else "").replace(" ", "").replace("₽", "").replace(",", ".").strip()
    if not t:
        return None
    try:
        r = round(float(t), 2)
    except ValueError:
        return None
    return (int(r) if r == int(r) else r) if 0 < r < 100000 else None


def _rate_in(x, what: str):
    """Ставка, которую ввели в приложении: пусто — общая, иначе число больше нуля."""
    if x is None or not str(x).strip():
        return None
    r = _rate_cell(x)
    if r is None:
        raise ValueError(f"{what} — число больше нуля, например 450")
    return r


def worker_add(name: str, area: str, who: str, rate=None, brate=None) -> None:
    name, area = name.strip()[:40], area.strip()[:30]
    if not name:
        raise ValueError("Впишите имя")
    if any(w["name"].lower() == name.lower() for w in workers()):
        raise ValueError("Такой сотрудник уже есть")
    r, b = _rate_in(rate, "Ставка за час"), _rate_in(brate, "Ставка за кровать")
    _tt(WORKERS_TAB, WORKERS_HEADERS).append_row(
        [secrets.token_hex(3), name, area, _stamp(), who, "", "" if r is None else r, "" if b is None else b],
        value_input_option="RAW")


def worker_edit(wid: str, area, rate, brate) -> None:
    """Тех. директор меняет участок и личные ставки сотрудника (пусто — общая ставка)."""
    w = next((x for x in workers() if x["id"] == wid), None)
    if not w:
        raise ValueError("Сотрудник не найден, обновите список")
    r, b = _rate_in(rate, "Ставка за час"), _rate_in(brate, "Ставка за кровать")
    a = w["area"] if area is None else str(area).strip()[:30]
    ws = _tt(WORKERS_TAB, WORKERS_HEADERS)
    ws.update(values=[[a]], range_name=f"C{w['row']}", value_input_option="RAW")
    ws.update(values=[["" if r is None else r, "" if b is None else b]], range_name=f"G{w['row']}:H{w['row']}",
              value_input_option="RAW")


def worker_fire(wid: str) -> None:
    """Не удаляем строку: часы за прошлые дни должны остаться в отчётах. Просто прячем из списка."""
    w = next((x for x in workers() if x["id"] == wid), None)
    if not w:
        raise ValueError("Сотрудник не найден")
    _tt(WORKERS_TAB, WORKERS_HEADERS).update(values=[[FIRED]], range_name=f"F{w['row']}",
                                                     value_input_option="RAW")


def _beds(x):
    """Покрашенные кровати: целое 0…200; пусто или 0 — None."""
    t = str(x if x is not None else "").strip().replace(",", ".")
    if not t:
        return None
    try:
        n = float(t)
    except ValueError:
        raise ValueError("Кровати — целое число")
    if n != int(n) or n < 0 or n > 200:
        raise ValueError("Кровати — целое число от 0 до 200")
    return int(n) or None


def _tabel() -> list:
    out = []
    for i, v in enumerate(_tt(TABEL_TAB, TABEL_HEADERS).get_all_values()[1:], start=2):
        v = list(v) + [""] * 7
        try:
            day = _d(v[0])
        except ValueError:
            continue
        try:
            h = _hours(v[3])
        except ValueError:
            h = None
        try:
            b = _beds(v[6])
        except ValueError:
            b = None
        out.append({"row": i, "day": day, "id": v[1].strip(), "name": v[2].strip(), "hours": h, "beds": b})
    return out


def tech_data(day: date) -> dict:
    ws_list = workers()
    rows = _tabel()
    got = {r["id"]: r for r in rows if r["day"] == day}
    month = [r for r in rows if r["day"].year == day.year and r["day"].month == day.month]
    return {"day": day.isoformat(), "today": now_date().isoformat(),
            "workers": [{"id": w["id"], "name": w["name"], "area": w["area"], "painter": is_painter(w["area"]),
                         "hours": (got.get(w["id"]) or {}).get("hours"), "beds": (got.get(w["id"]) or {}).get("beds"),
                         "rate": w["rate"], "bed_rate": w["bed_rate"]}
                        for w in ws_list],
            "rate_common": hour_rate(), "bed_rate_common": bed_rate(),
            "filled": sum(1 for w in ws_list if (got.get(w["id"]) or {}).get("hours")),
            "month_hours": round(sum(r["hours"] or 0 for r in month), 2),
            "month_beds": sum(r["beds"] or 0 for r in month)}


def tech_day_save(day: date, hours: dict, who: str, beds: dict = None) -> int:
    """Записывает за день часы {ID: часы} и покрашенные кровати маляров {ID: штук}.
    Пусто или 0 — «не работал» / «не красил»."""
    if day > now_date():
        raise ValueError("Нельзя заполнить табель на будущее")
    if day < now_date() - timedelta(days=400):
        raise ValueError("Слишком старая дата")
    known = {w["id"]: w for w in workers(include_fired=True)}
    beds = beds or {}
    clean = {}
    for wid in set(hours or {}) | set(beds):
        if wid in known:
            clean[wid] = (_hours((hours or {}).get(wid)), _beds(beds.get(wid)) if is_painter(known[wid]["area"]) else None)
    ws = _tt(TABEL_TAB, TABEL_HEADERS)
    if ws.col_count < len(TABEL_HEADERS):  # старый лист без колонки «Кровати»
        ws.add_cols(len(TABEL_HEADERS) - ws.col_count)
    have = {r["id"]: r for r in _tabel() if r["day"] == day}
    now, ds, ups, new = _stamp(), day.strftime("%d.%m.%Y"), [], []
    for wid, (h, b) in clean.items():
        r = have.get(wid)
        if r:
            if r["hours"] != h or r["beds"] != b:
                ups.append({"range": f"C{r['row']}:G{r['row']}",
                            "values": [[known[wid]["name"], "" if h is None else h, who, now, "" if b is None else b]]})
        elif h is not None or b is not None:
            new.append([ds, wid, known[wid]["name"], "" if h is None else h, who, now, "" if b is None else b])
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
        if (r["hours"] or r["beds"]) and d1 <= r["day"] <= d2:
            w = known.get(r["id"]) or {"name": r["name"] or "?", "area": ""}
            p = per.setdefault(r["id"], {"id": r["id"], "name": w["name"], "area": w["area"], "hours": 0, "beds": 0,
                                         "days": 0, "by_day": {}, "beds_by_day": {}})
            k = r["day"].strftime("%d.%m")
            if r["hours"]:
                p["hours"] = round(p["hours"] + r["hours"], 2)
                p["days"] += 1
                p["by_day"][k] = r["hours"]
            if r["beds"]:
                p["beds"] += r["beds"]
                p["beds_by_day"][k] = r["beds"]
    people = sorted(per.values(), key=lambda p: (p["area"].lower(), p["name"].lower()))
    order = lambda kv: kv[0][3:] + kv[0][:2]
    for p in people:
        p["by_day"] = dict(sorted(p["by_day"].items(), key=order))
        p["beds_by_day"] = dict(sorted(p["beds_by_day"].items(), key=order))
    return {"from": d1.isoformat(), "to": d2.isoformat(), "total": round(sum(p["hours"] for p in people), 2),
            "beds": sum(p["beds"] for p in people), "people": people}


def report_send(d1: date, d2: date, who: str) -> dict:
    summ = period_summary(d1, d2)
    if not summ["people"]:
        raise ValueError("За этот период в табеле нет часов")
    with_pay(summ, hour_rate(), bed_rate(), personal_rates())  # ставки запоминаются в отчёте: поменяют — старые не пересчитаются
    _tt(REPORTS_TAB, REPORTS_HEADERS).append_row(
        [_stamp(), who, _d(summ["from"]).strftime("%d.%m.%Y"), _d(summ["to"]).strftime("%d.%m.%Y"), summ["total"],
         json.dumps(summ, ensure_ascii=False), summ["rate"], summ["pay"]], value_input_option="RAW")
    a, b = _d(summ["from"]), _d(summ["to"])
    period = a.strftime("%d.%m") + ("" if a == b else "–" + b.strftime("%d.%m.%Y"))
    full = with_bonuses(json.loads(json.dumps(summ, default=str)), bonuses())
    money = f"{full['total_pay']:,}".replace(",", " ")
    parts = [f"{summ['total']:g} ч × {summ['rate']:g} ₽"]
    if summ.get("beds"):
        parts.append(f"{summ['beds']} кров. × {summ['bed_rate']:g} ₽")
    if full["bonus"]:
        parts.append(f"премии {full['bonus']:,} ₽".replace(",", " "))
    push_safe("reports", f"📑 Табель за {period}: {money} ₽",
              " + ".join(parts) + f" · {len(summ['people'])} чел. Отправил(а) {who}",
              _codes({"gendir", "admin"}), "reports")
    return summ


def reports() -> list:
    out = []
    for i, v in enumerate(_tt(REPORTS_TAB, REPORTS_HEADERS).get_all_values()[1:], start=2):
        v = list(v) + [""] * 6
        try:
            data = json.loads(v[5])
        except ValueError:
            continue
        data.update(id=i, sent=v[0].strip(), who=v[1].strip())
        if "rate" not in data:  # отчёты, отправленные до появления ставки, — по текущей ставке
            with_pay(data, hour_rate(), bed_rate())
            data["rate_now"] = True
        elif "bed_rate" not in data:
            with_pay(data, data["rate"], bed_rate())
        else:
            with_pay(data, data["rate"], data["bed_rate"])  # у людей ставки уже в отчёте; без них — ставка отчёта
        data["row_sent"] = v[0].strip()
        out.append(data)
    out = out[::-1][:60]
    bl = bonuses()
    for r in out:
        with_bonuses(r, bl)
    return out


def _report_row(rid: int, sent: str):
    ws = _tt(REPORTS_TAB, REPORTS_HEADERS)
    rows = ws.get_all_values(fresh=True)
    if not 2 <= rid <= len(rows) or rows[rid - 1][0].strip() != (sent or "").strip():
        raise ValueError("Отчёты в таблице поменялись, обновите экран и повторите")
    return ws, rows[rid - 1]


def report_edit(rid: int, sent: str, body: dict, who: str) -> dict:
    """Ген. директор правит отчёт: период, люди (часы, дни, кровати, ставки, доплата/вычет). Суммы считает сервер."""
    ws, row = _report_row(rid, sent)
    try:
        old = json.loads((list(row) + [""] * 6)[5])
    except ValueError:
        old = {}
    d1, d2 = _d(body.get("from")), _d(body.get("to"))
    if d2 < d1:
        d1, d2 = d2, d1
    prev = {p.get("id"): p for p in old.get("people", []) if p.get("id")}
    people = []
    for x in body.get("people") or []:
        name = str(x.get("name") or "").strip()[:40]
        if not name:
            raise ValueError("У сотрудника пустое имя")
        hours = _hours(x.get("hours")) or 0
        beds = _beds(x.get("beds")) or 0
        wid = str(x.get("id") or "").strip()[:12] or ("r" + secrets.token_hex(3))
        try:
            adj = int(round(float(str(x.get("adj") or 0).replace(" ", "").replace(",", "."))))
        except ValueError:
            raise ValueError(f"{name}: доплата или вычет — число, например 2000 или -500")
        o = prev.get(wid, {})
        p = {"id": wid, "name": name, "area": str(x.get("area") or o.get("area") or "").strip()[:30],
             "hours": hours, "beds": beds, "days": max(0, min(400, _int(x.get("days"), o.get("days", 0)))),
             "rate": _rate_in(x.get("rate"), f"{name}: ставка за час") or old.get("rate") or hour_rate(),
             "bed_rate": _rate_in(x.get("bed_rate"), f"{name}: ставка за кровать") or old.get("bed_rate") or bed_rate(),
             "adj": adj, "adj_note": str(x.get("adj_note") or "").strip()[:120],
             "by_day": o.get("by_day", {}), "beds_by_day": o.get("beds_by_day", {})}
        if o and (o.get("hours") != hours or o.get("beds", 0) != beds):
            p["edited"] = True  # по дням было другое — показываем, что итог поправлен руками
        people.append(p)
    if not people:
        raise ValueError("В отчёте должен остаться хотя бы один человек. Чтобы убрать отчёт, нажмите «Удалить отчёт»")
    summ = {"from": d1.isoformat(), "to": d2.isoformat(), "people": people,
            "total": round(sum(p["hours"] for p in people), 2), "beds": sum(p["beds"] for p in people),
            "edited": {"by": who, "at": _stamp()}}
    with_pay(summ, old.get("rate") or hour_rate(), old.get("bed_rate") or bed_rate())
    ws.update(values=[[d1.strftime("%d.%m.%Y"), d2.strftime("%d.%m.%Y"), summ["total"],
                       json.dumps(summ, ensure_ascii=False), summ["rate"], summ["pay"]]],
              range_name=f"C{rid}:H{rid}", value_input_option="RAW")
    log.info("Отчёт табеля %s изменён (%s): %s ч, %s ₽", rid, who, summ["total"], summ["pay"])
    return {"ok": True, "pay": summ["pay"]}


def report_delete(rid: int, sent: str, who: str) -> dict:
    ws, row = _report_row(rid, sent)
    ws.delete_rows(rid)
    log.info("Отчёт табеля удалён (%s): %s–%s, отправлен %s", who, row[2] if len(row) > 2 else "", row[3] if len(row) > 3 else "", sent)
    return {"ok": True}


# ------------------------------------------------------------ премии (тех. директор → ген. директор)
def _bonus_ws():
    return _tt(BONUS_TAB, BONUS_HEADERS)


def bonuses() -> list:
    out = []
    for i, v in enumerate(_bonus_ws().get_all_values()[1:], start=2):
        v = list(v) + [""] * 12
        try:
            amount = int(float(str(v[5]).replace(" ", "").replace(",", ".")))
            day = _d(v[1].strip()[:10])
        except ValueError:
            continue
        out.append({"row": i, "no": v[0].strip(), "created": v[1].strip(), "day": day, "by": v[2].strip(),
                    "worker": v[3].strip(), "name": v[4].strip(), "amount": amount, "reason": v[6].strip(),
                    "status": v[7].strip() or BONUS_WAIT, "decided": v[8].strip(), "decided_by": v[9].strip(),
                    "comment": v[10].strip(), "code": norm_code(v[11])})
    return out


def _bonus_public(b: dict) -> dict:
    return {k: (b[k].isoformat() if k == "day" else b[k]) for k in
            ("row", "no", "created", "day", "by", "name", "amount", "reason", "status", "decided", "decided_by", "comment")}


def bonus_propose(worker: str, amount, reason: str, who: str, code: str) -> dict:
    w = next((x for x in workers() if x["id"] == worker), None)
    if not w:
        raise ValueError("Выберите сотрудника цеха")
    try:
        amount = int(round(float(str(amount).replace(" ", "").replace(",", "."))))
    except (TypeError, ValueError):
        raise ValueError("Сумма — число, например 3000")
    if not 0 < amount <= 1_000_000:
        raise ValueError("Сумма должна быть больше нуля")
    reason = (reason or "").strip()[:300]
    if not reason:
        raise ValueError("Напишите, за что премия")
    no = secrets.token_hex(3)
    _bonus_ws().append_row([no, _stamp(), who, w["id"], w["name"], amount, reason, BONUS_WAIT, "", "", "", code],
                           value_input_option="RAW")
    push_safe("reports", f"🎁 Премия на согласование: {amount:,} ₽".replace(",", " "),
              f"{w['name']} — {reason}. Предложил(а) {who}", _codes({"gendir", "admin"}, skip=code), "reports")
    return {"ok": True}


def bonus_decide(row: int, ok: bool, comment: str, who: str) -> dict:
    b = next((x for x in bonuses() if x["row"] == row), None)
    if not b:
        raise ValueError("Премия не найдена, обновите экран")
    if b["status"] != BONUS_WAIT:
        raise ValueError(f"Уже решено: {b['status'].lower()}")
    status = BONUS_OK if ok else BONUS_NO
    comment = (comment or "").strip()[:200]
    ws = _bonus_ws()
    cur = ws.row_values(row) + [""]
    if cur[0].strip() != b["no"]:  # строки в таблице сдвинули вручную
        raise ValueError("В таблице премий поменялись строки, обновите экран")
    ws.update(values=[[status, _stamp(), who, comment]], range_name=f"H{row}:K{row}", value_input_option="RAW")
    proposer = b["code"]
    if proposer:
        money = f"{b['amount']:,}".replace(",", " ")
        push_safe("reports", f"{'✅ Премия согласована' if ok else '❌ Премия отклонена'}: {money} ₽",
                  f"{b['name']} — {b['reason']}" + (f". {comment}" if comment else ""), {proposer}, "tech")
    return {"ok": True, "status": status}


# --------------------------------------------------------------- запросы
ROLE_TABS = {"manager": ["courier", "prod", "keeper", "manager", "chat"], "courier": ["courier", "chat"],
             "prod": ["prod", "chat"], "keeper": ["keeper", "chat"],
             "techdir": ["tech", "prod", "keeper", "chat"],
             "gendir": ["courier", "prod", "keeper", "manager", "reports", "chat"],
             "admin": ["courier", "prod", "keeper", "manager", "tech", "reports", "chat"]}


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
                     "label": ROLE_LABELS[role_of(code, me)], "tabs": ROLE_TABS[role_of(code, me)],
                     "admin": role_of(code, me) == "admin"}
    if not me:
        return 403, {"error": "Телефон не добавлен", "code": code}
    me = {**me, "role": role_of(code, me)}
    tabs = ROLE_TABS[me["role"]]
    is_admin = me["role"] == "admin"
    pay = code in pay_codes()
    who = me["name"] or "сотрудник"
    today = now_date()

    if path == "/api/push/inbox":
        return 200, push_inbox(code, _int(body.get("after"), 0))
    if method == "POST" and path == "/api/push/register":
        fcm = body.get("fcm")
        return 200, push_register(code, fcm=str(fcm).strip() if isinstance(fcm, str) else None,
                                  chat=body["chat"] if isinstance(body.get("chat"), bool) else None)
    if path == "/api/push/settings":
        return 200, push_register(code)
    if path == "/api/chat":
        return 200, chat_list(_int(body.get("since"), 0), code)
    if method == "POST" and path == "/api/chat/edit":
        try:
            return 200, chat_edit(_int(body.get("id"), 0), str(body.get("text") or ""), code)
        except PermissionError as e:
            return 400, {"error": str(e)}
        except ValueError as e:
            return 400, {"error": str(e)}
    if method == "POST" and path == "/api/chat/delete":
        try:
            return 200, chat_delete(_int(body.get("id"), 0), code, is_admin, who)
        except (PermissionError, ValueError) as e:
            return 400, {"error": str(e)}
    if method == "POST" and path == "/api/chat/send":
        try:
            return 200, chat_send(str(body.get("text") or ""), me, code, _int(body.get("reply"), 0))
        except ValueError as e:
            return 400, {"error": str(e)}

    if "tech" in tabs and path.startswith("/api/tech"):
        try:
            if path == "/api/tech":
                data = tech_data(_d(body.get("day")) if body.get("day") else today)
                data["book"] = {"separate": bool(tabel_book_id()), "url": tabel_book_url(), "admin": is_admin,
                                "bot": bot_email() if is_admin and not tabel_book_id() else ""}
                return 200, data
            if method == "POST" and path in ("/api/tech/book/create", "/api/tech/book/connect"):
                if not is_admin:
                    return 403, {"error": "Перенести табель может только администратор"}
                with _lock:
                    if path.endswith("create"):
                        return 200, tabel_create(str(body.get("emails") or "").replace(";", ",").split(","), who)
                    m = re.search(r"/d/([A-Za-z0-9_-]{20,})", str(body.get("link") or "")) \
                        or re.fullmatch(r"\s*([A-Za-z0-9_-]{20,})\s*", str(body.get("link") or ""))
                    if not m:
                        return 400, {"error": "Не нашёл в ссылке код таблицы. Вставьте ссылку целиком"}
                    return 200, tabel_move(m.group(1), who)
            if path == "/api/tech/workers":
                return 200, {"workers": workers()}
            if path == "/api/tech/summary":
                return 200, period_summary(_d(body.get("from")), _d(body.get("to")))
            if path == "/api/tech/sent":  # тех. директору — только часы, без денег
                return 200, {"reports": [{k: r[k] for k in ("from", "to", "total", "sent", "who")}
                                         for r in reports() if r.get("who") == who][:20]}
            if path == "/api/tech/bonuses":  # свои предложенные премии и их статус
                return 200, {"bonuses": [_bonus_public(b) for b in bonuses()[::-1] if b["by"] == who][:40]}
            if method == "POST":
                with _lock:
                    if path == "/api/tech/day":
                        n = tech_day_save(_d(body.get("day")), body.get("hours") or {}, who, body.get("beds") or {})
                        return 200, {"ok": True, "changed": n}
                    if path == "/api/tech/bonus":
                        return 200, bonus_propose(str(body.get("worker") or ""), body.get("amount"),
                                                  str(body.get("reason") or ""), who, code)
                    if path == "/api/tech/worker":
                        worker_add(str(body.get("name") or ""), str(body.get("area") or ""), who,
                                   body.get("rate"), body.get("bed_rate"))
                        return 200, {"ok": True}
                    if path == "/api/tech/worker/edit":
                        worker_edit(str(body.get("id") or ""), body.get("area"), body.get("rate"), body.get("bed_rate"))
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
        bl = bonuses()
        return 200, {"reports": reports(), "rate": hour_rate(), "bed_rate": bed_rate(),
                     "can_rate": me["role"] in ("gendir", "admin"),
                     "pending": [_bonus_public(b) for b in bl if b["status"] == BONUS_WAIT],
                     "decided": [_bonus_public(b) for b in bl[::-1] if b["status"] != BONUS_WAIT][:20]}
    if method == "POST" and path == "/api/reports/rate" and me["role"] in ("gendir", "admin"):
        key = "bed_rate" if body.get("kind") == "bed" else "hour_rate"
        try:
            r = round(float(str(body.get("rate")).replace(",", ".")), 2)
        except (TypeError, ValueError):
            return 400, {"error": "Ставка — число, например 625"}
        if not 0 < r < 100000:
            return 400, {"error": "Ставка должна быть больше нуля"}
        with _lock:
            _set_rate_raw(key, f"{r:g}")
        return 200, {"ok": True, "rate": r}
    if method == "POST" and path in ("/api/reports/edit", "/api/reports/delete") and me["role"] in ("gendir", "admin"):
        try:
            with _lock:
                if path.endswith("delete"):
                    return 200, report_delete(_int(body.get("id"), 0), str(body.get("sent") or ""), who)
                return 200, report_edit(_int(body.get("id"), 0), str(body.get("sent") or ""), body, who)
        except ValueError as e:
            return 400, {"error": str(e)}
    if method == "POST" and path == "/api/reports/bonus" and me["role"] in ("gendir", "admin"):
        try:
            with _lock:
                return 200, bonus_decide(_int(body.get("row"), 0), bool(body.get("ok")), str(body.get("comment") or ""), who)
        except ValueError as e:
            return 400, {"error": str(e)}

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
            orders = _find(today, kind, key, early=path.endswith("done"))
            if not orders:
                return 409, {"error": "Уже обработано" if kind == "city" or path.endswith("move")
                             else "Заказы по России отмечаются только в день отгрузки"}
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
        cur = devices(force=True).get(new_code)
        if not is_admin and (role == "admin" or (cur and role_of(new_code, cur) == "admin")):
            return 400, {"error": "Администратора назначает только администратор"}
        if cur and not is_admin:
            return 400, {"error": "Этот телефон уже добавлен. Сменить роль может только администратор"}
        with _lock:
            add_device(new_code, role, str(body.get("name") or "").strip()[:40], who)
        return 200, {"ok": True}
    if method == "POST" and path == "/api/admin/device" and is_admin:
        ed, role = norm_code(body.get("code")), body.get("role")
        cur = devices(force=True).get(ed)
        if not cur:
            return 400, {"error": "Сотрудник не найден, обновите список"}
        if role not in ROLE_LABELS:
            return 400, {"error": "Выберите роль"}
        if ed in admin_codes() and role != "admin":
            return 400, {"error": "Этот администратор задан на Bothost (ADMIN_CODES), его роль меняется только там"}
        if ed == code and role != "admin":
            return 400, {"error": "Свою роль администратора снять нельзя"}
        with _lock:
            add_device(ed, role, str(body.get("name") if body.get("name") is not None else cur["name"]).strip()[:40], who)
        return 200, {"ok": True}
    if method == "POST" and path == "/api/manager/device/remove" and "manager" in tabs:
        rm = norm_code(body.get("code"))
        if rm == code:
            return 400, {"error": "Себя удалить нельзя"}
        cur = devices(force=True).get(rm)
        if cur and role_of(rm, cur) == "admin" and (not is_admin or rm in admin_codes()):
            return 400, {"error": "Администратора убрать нельзя" if not is_admin else "Этот администратор задан на Bothost"}
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
        if path == "/app/version":
            v = app_version()
            return self._json(200, v or {"code": 0})
        if path.startswith("/chat-photo/"):
            name = path.rsplit("/", 1)[-1]
            if not re.fullmatch(r"[A-Za-z0-9_-]{20,40}\.(jpg|png|webp)", name):
                return self._send(404, b"", "text/plain")
            try:
                data = (photo_dir() / name).read_bytes()
            except OSError:
                return self._send(404, b"", "text/plain")
            ctype = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp"}[name.rsplit(".", 1)[1]]
            return self._send(200, data, ctype, "private, max-age=31536000, immutable")
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
        if path == "/api/chat/photo":
            size = int(self.headers.get("Content-Length") or 0)
            if size > MAX_PHOTO:
                return self._json(400, {"error": "Фото больше 8 МБ"})
            data = self.rfile.read(size)
            from urllib.parse import unquote
            try:
                code, res = chat_photo(self.headers.get("X-Token") or "", data,
                                       unquote(self.headers.get("X-Caption") or ""), _int(self.headers.get("X-Reply"), 0))
            except Exception:
                log.exception("Приложение: фото в чат")
                code, res = 500, {"error": "Не получилось сохранить фото"}
            return self._json(code, res)
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


def _prewarm() -> None:
    """Сразу после запуска читаем листы всех экранов, чтобы и самое первое открытие было быстрым.
    Упёрлись в минутный лимит Google (429) — ждём минуту и дочитываем то, что не успели."""
    time.sleep(5)
    t0, today = time.time(), now_date()
    todo = [("курьер", lambda: courier_data(today)), ("производство", lambda: prod_data(today)),
            ("склад", keeper_data), ("менеджер", lambda: manager_data(today)),
            ("табель", lambda: tech_data(today)), ("отчёты", reports), ("чат", lambda: chat_list(0, ""))]
    for attempt in (1, 2):
        left = []
        for name, f in todo:
            try:
                f()
            except Exception as e:
                if "429" in str(e) and attempt == 1:
                    left.append((name, f))
                else:
                    log.warning("Прогрев: экран «%s» не прочитался: %s", name, e)
        if not left:
            break
        log.info("Прогрев: лимит Google на минуту, дочитаю через минуту: %s", ", ".join(n for n, _ in left))
        time.sleep(65)
        todo = left
    log.info("Прогрев таблицы: готово за %.1f сек", time.time() - t0)


def start(**hooks) -> None:
    """Запускает веб-сервер в отдельном потоке: приложение на /, Алиса на /alice/<секрет>.
    hooks — функции бота, которые делают то же, что кнопки в MAX."""
    _deps.update(hooks)
    srv = ThreadingHTTPServer(("0.0.0.0", config.ALICE_PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log.info("Приложение для сотрудников: слушаю порт %d", config.ALICE_PORT)
    threading.Thread(target=_update_watch, daemon=True).start()
    try:
        sheets.start_warmer()  # таблица в памяти сервера: кнопки не ждут Google
    except Exception:
        log.exception("Таблица: не смог запустить фоновое обновление")
    threading.Thread(target=_prewarm, daemon=True).start()
    try:
        d = photo_dir()
        if str(d).startswith("/app/shared"):
            log.info("Фото чата: %s (постоянное хранилище)", d)
        else:
            log.warning("Фото чата: %s — постоянное хранилище Bothost не подключено, фото могут пропасть при обновлении", d)
    except Exception:
        log.exception("Фото чата: не смог создать папку")
