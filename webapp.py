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
ROLE_WORDS = {"менеджер": "manager", "курьер": "courier", "производство": "prod", "цех": "prod",
              "кладовщик": "keeper", "склад": "keeper"}
HERE = Path(__file__).resolve().parent
STATIC = {  # что отдаём браузеру как есть
    "/": ("app.html", "text/html; charset=utf-8"),
    "/app": ("app.html", "text/html; charset=utf-8"),
    "/manifest.webmanifest": ("app/manifest.webmanifest", "application/manifest+json"),
    "/sw.js": ("app/sw.js", "text/javascript; charset=utf-8"),
    "/icon-192.png": ("app/icon-192.png", "image/png"),
    "/icon-512.png": ("app/icon-512.png", "image/png"),
}

_deps: dict = {"deliver": None, "move": None, "notify": None}
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
        labels = {v.lower(): k for k, v in config.ROLE_LABELS.items()}
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
    row = [code, name, config.ROLE_LABELS[role], datetime.now(ZoneInfo(config.TIMEZONE)).strftime("%d.%m.%Y %H:%M"), who]
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


def _parse_role(text: str):
    t = (text or "").strip().lower()
    for word, key in ROLE_WORDS.items():
        if t.startswith(word[:4]):
            return key
    return t if t in config.ROLE_LABELS else None


def app_command(raw: str, who: str) -> str:
    """Команда менеджера в MAX: /app КОД роль Имя · /app — список · /app удалить КОД."""
    parts = raw.split()[1:]
    usage = ("📱 Приложение для сотрудников\n\nДобавить телефон:\n/app КОД роль Имя\n"
             "например: /app K7QM-2XPA курьер Иван\n\nРоли: менеджер, курьер, производство, кладовщик\n"
             "Убрать: /app удалить КОД")
    try:
        if not parts:
            have = devices(force=True)
            if not have:
                return usage + "\n\nПока никого не добавлено."
            lines = [f"• {c} — {d['name'] or 'без имени'}, {config.ROLE_LABELS[d['role']]}" for c, d in have.items()]
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
        return f"✅ {code} добавлен: {name or 'без имени'}, {config.ROLE_LABELS[role]}.\nПусть нажмёт «Проверить» в приложении."
    except Exception:
        log.exception("Приложение: команда /app")
        return "⚠️ Не получилось записать в таблицу, попробуйте ещё раз."


# ------------------------------------------------------------ данные экранов
def _qty_text(o: dict) -> str:
    return f" ×{logic.qty(o)}" if logic.qty(o) > 1 else ""


def courier_data(today: date) -> dict:
    orders = sheets.read_orders()
    items = logic.courier_items(orders, today)
    ru = [{
        "key": g["key"], "late": g["late"],
        "title": g["channel"] + (f" №{g['shipment']}" if g["shipment"] else ""),
        "count": sum(logic.qty(o) for o in g["orders"]),
        "lines": [f"{name} — {n} шт." for name, n in sorted(logic.count_items(g["orders"]).items())],
    } for g in logic.russia_groups(items)]
    city = [{
        "key": o["order_no"], "late": late, "address": o["address"] or "адрес не указан",
        "item": logic.item_line(o) + _qty_text(o), "phone": logic.fmt_phone(o["phone"]),
        "tel": re.sub(r"[^\d+]", "", (o["phone"] or "").split("доб")[0]),
        "title": f"{o['channel']} · № {o['order_no']}",
    } for o, late in logic.city_items(items)]
    ahead = []
    for n, label in ((1, "Завтра"), (2, "Послезавтра")):
        day = today + timedelta(days=n)
        ahead.append({"label": label, "count": len(logic.day_items(orders, day, today)),
                      "text": logic.day_preview(logic.day_items(orders, day, today), day, label)})
    return {"date": today.strftime("%d.%m"), "ru": ru, "city": city, "ahead": ahead}


def _find(today: date, kind: str, key: str) -> list:
    items = logic.courier_items(sheets.read_orders(force=True), today)
    if kind == "ru":
        g = next((g for g in logic.russia_groups(items) if g["key"] == key), None)
        return g["orders"] if g else []
    return [o for o, _ in items if o["order_no"] == key and logic.is_city(o)][:1]


def prod_data(today: date) -> dict:
    orders = sheets.read_orders()
    stock = sheets.latest_stock()
    need = logic.need_counts(orders)
    rows = []
    for code in config.STOCK_CODES:
        have = stock[code][0] if code in stock else None
        rows.append({"name": logic.code_name(code), "have": have, "need": need.get(code, 0)})
    try:
        frames = [{"width": w, "n": n} for w, n in sorted(sheets.frames_get().items())]
    except Exception:
        log.exception("Приложение: каркасы")
        frames = []
    return {"plan": logic.production_plan(orders, today), "stock": rows, "frames": frames,
            "today": logic.today_total(orders, today)}


def keeper_data() -> dict:
    res = {}
    for kind in ("raw", "tmc"):
        try:
            res[kind] = [{"name": i["name"], "qty": logic.fmt_qty(i["qty"]), "unit": i["unit"],
                          "low": bool(i["min"] and i["qty"] < i["min"])} for i in sheets.inv_get(kind)]
        except Exception:
            log.exception("Приложение: склад %s", kind)
            res[kind] = []
    return res


def manager_data(today: date) -> dict:
    orders = sheets.read_orders()
    open_ = [o for o in orders if not o["status"]]
    return {
        "today": logic.today_total(orders, today),
        "open": sum(logic.qty(o) for o in open_),
        "city": sum(logic.qty(o) for o in open_ if logic.is_city(o)),
        "devices": [{"code": c, "name": d["name"], "role": d["role"], "label": config.ROLE_LABELS[d["role"]]}
                    for c, d in devices(force=True).items()],
    }


# --------------------------------------------------------------- запросы
ROLE_TABS = {"manager": ["courier", "prod", "keeper", "manager"], "courier": ["courier"],
             "prod": ["prod"], "keeper": ["keeper"]}


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
                     "label": config.ROLE_LABELS[me["role"]], "tabs": ROLE_TABS[me["role"]]}
    if not me:
        return 403, {"error": "Телефон не добавлен", "code": code}
    tabs = ROLE_TABS[me["role"]]
    who = me["name"] or "сотрудник"
    today = now_date()

    if path == "/api/courier" and "courier" in tabs:
        return 200, courier_data(today)
    if path == "/api/prod" and "prod" in tabs:
        return 200, prod_data(today)
    if path == "/api/keeper" and "keeper" in tabs:
        return 200, keeper_data()
    if path == "/api/manager" and "manager" in tabs:
        return 200, manager_data(today)

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

    if method == "POST" and path == "/api/manager/device" and "manager" in tabs:
        new_code, role = norm_code(body.get("code")), body.get("role")
        if not new_code:
            return 400, {"error": "Код должен быть из 8 знаков, как на экране телефона: K7QM-2XPA"}
        if role not in config.ROLE_LABELS:
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
        except Exception:
            log.exception("Приложение: ошибка в %s", path)
            code, data = 500, {"error": "Не получилось открыть таблицу, попробуйте ещё раз"}
        self._json(code, data)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path.startswith("/api/"):
            return self._api("GET", {})
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
        if path.startswith("/api/"):
            try:
                body = json.loads(self._body() or b"{}")
            except ValueError:
                body = {}
            return self._api("POST", body if isinstance(body, dict) else {})
        self._json(404, {"error": "not found"})

    def log_message(self, *a):
        pass


def start(deliver, move, notify) -> None:
    """Запускает веб-сервер в отдельном потоке: приложение на /, Алиса на /alice/<секрет>."""
    _deps.update(deliver=deliver, move=move, notify=notify)
    srv = ThreadingHTTPServer(("0.0.0.0", config.ALICE_PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log.info("Приложение для сотрудников: слушаю порт %d", config.ALICE_PORT)
