"""Навык Алисы (Яндекс Диалоги): голосовые вопросы к таблицам бота. Только чтение — ничего не меняет.
Запускается внутри бота маленьким веб-сервером (alice.start) или как функция Яндекс Облака (alice.handler)."""
import json
import logging
import re
import threading
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import config
import logic
import sheets

log = logging.getLogger("orders-bot")

WORD_WIDTHS = {"сто двадцать": 120, "сто сорок": 140, "сто шестьдесят": 160, "сто восемьдесят": 180, "двести": 200}
HELP = ("Я могу сказать: сколько отгрузок на сегодня, сколько сваренных каркасов, остатки на складе, "
        "например «остаток сто шестьдесят чёрных», план на завтра, или сколько осталось сырья, например краски.")


def _width(cmd: str):
    for words, w in WORD_WIDTHS.items():
        if words in cmd:
            return w
    m = re.search(r"\b(120|140|160|180|200)\b", cmd)
    return int(m.group(1)) if m else None


def _color(cmd: str):
    if re.search(r"черн|чёрн", cmd):
        return "04"
    if "бел" in cmd:
        return "06"
    return None


def _day(cmd: str, today):
    if "послезавтра" in cmd:
        return today + timedelta(days=2)
    if "завтра" in cmd:
        return today + timedelta(days=1)
    return today


def _shipped_done(today) -> int:
    try:
        if sheets.get_setting("shipped_date") == str(today):
            return int(float(sheets.get_setting("shipped_n") or 0))
    except Exception:
        log.exception("Алиса: не смог прочитать отметку об отгрузке")
    return 0


def _plural(n: int, one: str, few: str, many: str) -> str:
    n10, n100 = n % 10, n % 100
    if n10 == 1 and n100 != 11:
        return one
    if 2 <= n10 <= 4 and not 12 <= n100 <= 14:
        return few
    return many


def _beds(n: int) -> str:
    return f"{n} {_plural(n, 'кровать', 'кровати', 'кроватей')}"


def answer(cmd: str, today) -> str:
    cmd = (cmd or "").lower().replace("ё", "е")

    if "отгруз" in cmd or "отправ" in cmd:
        orders = sheets.read_orders()
        total = logic.today_total(orders, today)
        left = max(0, total - _shipped_done(today))
        return "Отгрузок на сегодня нет." if not left else f"На сегодня отгрузить {_beds(left)}."

    if "каркас" in cmd:
        fr = sheets.frames_get()
        w = _width(cmd)
        if w:
            n = fr.get(w, 0)
            return f"Сваренных каркасов {w}: {n}."
        parts = [f"{k}: {v}" for k, v in sorted(fr.items())]
        return "Сваренные каркасы. " + ", ".join(parts) + f". Всего {sum(fr.values())}."

    if "план" in cmd or "производств" in cmd or "что делать" in cmd:
        day = _day(cmd, today)
        label = {0: "на сегодня", 1: "на завтра", 2: "на послезавтра"}[(day - today).days]
        counts: dict = {}
        for o in sheets.read_orders():
            if o["status"]:
                continue
            r = logic.key_range(o, today)
            if r and r[0] <= day <= r[1]:
                counts[logic.item_line(o)] = counts.get(logic.item_line(o), 0) + logic.qty(o)
        if not counts:
            return f"{label.capitalize()} доставок нет."
        total = sum(counts.values())
        return f"{label.capitalize()} {_beds(total)}: " + ", ".join(f"{name} — {n}" for name, n in sorted(counts.items())) + "."

    for kind, stems in (("raw", ("сырь",)), ("tmc", ("тмц", "тэ эм цэ"))):
        if any(s in cmd for s in stems):
            items = sheets.inv_get(kind)
            if not items:
                return "В этом списке пока ничего нет."
            low = [i for i in items if i["min"] and i["qty"] < i["min"]]
            if low:
                return "Заканчивается: " + ", ".join(f"{i['name']} {logic.fmt_qty(i['qty'])} {i['unit']}" for i in low) + "."
            return "Всё в порядке, ничего не заканчивается."

    # конкретная позиция сырья или ТМЦ: «сколько краски»
    words = [w for w in re.findall(r"[а-я0-9×x]+", cmd) if len(w) > 3]
    for kind in ("raw", "tmc"):
        for it in sheets.inv_get(kind):
            name = it["name"].lower().replace("ё", "е")
            if any(w[:5] in name for w in words if w not in ("сколько", "осталось", "склад", "складе", "остаток")):
                return f"{it['name']}: {logic.fmt_qty(it['qty'])} {it['unit']}".strip() + "."

    if "остат" in cmd or "склад" in cmd or "сколько" in cmd:
        stock = sheets.latest_stock()
        w, c = _width(cmd), _color(cmd)
        names = {"04": "чёрных", "06": "белых"}
        if w:
            codes = [f"{w}.{c}"] if c else [f"{w}.04", f"{w}.06"]
            parts = [f"{names[k.split('.')[1]]} {stock.get(k, (0, ''))[0]}" for k in codes]
            return f"Кроватей {w}: " + ", ".join(parts) + "."
        by_w: dict = {}
        for code, (q, _) in stock.items():
            by_w[int(code.split(".")[0])] = by_w.get(int(code.split(".")[0]), 0) + q
        if by_w:
            return "На складе. " + ", ".join(f"{w}: {n}" for w, n in sorted(by_w.items())) + "."
        return "Данных об остатках пока нет."

    return "Не поняла. " + HELP


def handle(body: dict) -> dict:
    """Тело запроса Алисы -> тело ответа."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    req = body.get("request") or {}
    session = body.get("session") or {}
    cmd = req.get("command") or ""
    ver = body.get("version", "1.0")
    if req.get("original_utterance") == "ping":
        return {"response": {"text": "pong", "end_session": True}, "version": ver}
    if session.get("new") and not cmd:
        text = "Слушаю. " + HELP
    else:
        try:
            text = answer(cmd, datetime.now(ZoneInfo(config.TIMEZONE)).date())
        except Exception:
            log.exception("Алиса: ошибка при ответе")
            text = "Не получилось получить данные из таблицы. Попробуйте ещё раз."
    return {"response": {"text": text, "tts": text, "end_session": False}, "version": ver}


# --------------------------------------------------------- запуск
def handler(event, context=None):
    """Точка входа для функции Яндекс Облака."""
    body = event
    if isinstance(event, dict) and "body" in event:
        body = json.loads(event["body"]) if isinstance(event["body"], str) else event["body"]
    return handle(body)


class _H(BaseHTTPRequestHandler):
    def _body(self) -> bytes:
        """Тело запроса: и с Content-Length, и «кусочками» (chunked), как умеют разные прокси."""
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            out = b""
            while True:
                size = int((self.rfile.readline() or b"0").split(b";")[0].strip() or b"0", 16)
                if size == 0:
                    self.rfile.readline()
                    return out
                out += self.rfile.read(size)
                self.rfile.readline()
        return self.rfile.read(int(self.headers.get("Content-Length") or 0))

    def _send(self, code: int, payload: dict) -> None:
        out = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def do_POST(self):
        if self.path.split("?")[0].rstrip("/") != f"/alice/{config.ALICE_SECRET}":
            log.warning("Алиса: запрос на неверный адрес (длина пути %d) — проверьте секрет в адресе навыка", len(self.path))
            return self._send(404, {"error": "not found"})
        try:
            raw = self._body()
            body = json.loads(raw or b"{}")
            res = handle(body)
            log.info("Алиса: запрос «%s» -> ответ отправлен", ((body.get("request") or {}).get("command") or "")[:60])
            self._send(200, res)
        except Exception:
            log.exception("Алиса: не смог обработать запрос")
            self._send(200, {"response": {"text": "Что-то пошло не так, попробуйте ещё раз.", "end_session": False},
                             "version": "1.0"})

    def do_GET(self):  # проверка «жив ли адрес» из браузера
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *a):  # не засоряем логи
        pass


def start() -> None:
    """Запускает веб-сервер для Алисы в отдельном потоке (если задан ALICE_SECRET)."""
    if not config.ALICE_SECRET:
        return
    srv = ThreadingHTTPServer(("0.0.0.0", config.ALICE_PORT), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log.info("Навык Алисы: слушаю порт %d, адрес /alice/<секрет>", config.ALICE_PORT)
