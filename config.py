"""Настройки бота. Берутся из переменных окружения (на хостинге) или из файла .env."""
import os
import sys

from dotenv import load_dotenv

load_dotenv()


def _parse_ids(raw: str) -> set:
    ids = set()
    for part in raw.replace(" ", "").replace(";", ",").split(","):
        if part.lstrip("-").isdigit():
            ids.add(int(part))
        elif part:
            print(f"ВНИМАНИЕ: в MANAGER_IDS должно быть число, а не «{part}» — пропускаю", flush=True)
    return ids


TOKEN = (os.getenv("MAX_BOT_TOKEN") or os.getenv("BOT_TOKEN") or "").strip()
API = os.getenv("MAX_API_BASE", "https://platform-api.max.ru").rstrip("/")
MANAGER_IDS = _parse_ids(os.getenv("MANAGER_IDS", ""))

CREDS_FILE = os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")
# Либо содержимое credentials.json целиком (для хостингов), либо то же в base64
CREDS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON", "").strip()

YM_TOKEN = os.getenv("YANDEX_MARKET_TOKEN", "").strip()  # Яндекс Маркет, токен только на чтение

SHEET_ID = os.getenv("SHEET_ID", "").strip().strip("\"'")
if "/d/" in SHEET_ID:  # вставили ссылку целиком — достаём из неё код
    SHEET_ID = SHEET_ID.split("/d/")[1].split("/")[0]
SHEET_TAB = os.getenv("SHEET_TAB", "").strip()  # лист с заказами; пусто = первый лист

TIMEZONE = os.getenv("TIMEZONE", "Europe/Moscow")
STATE_FILE = os.getenv("STATE_FILE", "state.json")

# Названия служебных листов в Google Таблице (создаются автоматически)
USERS_TAB = "Пользователи"
GROUPS_TAB = "Подписчики"
STOCK_TAB = "Остатки"
SHIP_TAB = "Отгрузки"

# «Чистый чат»: бот правит одно сообщение-меню и пытается удалять сообщения пользователей.
# Отключить: переменная окружения CLEAN_CHAT=0
CLEAN_CHAT = os.getenv("CLEAN_CHAT", "1").strip().lower() not in ("0", "false", "no", "нет")

# Роли: ключ -> как называется в таблице и на кнопках
ROLE_LABELS = {"manager": "Менеджер", "courier": "Курьер", "prod": "Производство"}

# Кому приходят уведомления о новых заказах (уберите "courier", если курьерам не нужно)
NOTIFY_ROLES = {"manager", "courier", "prod"}

# Размеры, по которым производство вносит остатки: <ширина>.<цвет> (04 чёрный, 06 белый)
STOCK_CODES = [f"{w}.{c}" for w in (120, 140, 160, 180, 200) for c in ("04", "06")]


def check() -> None:
    if not TOKEN or not SHEET_ID:
        wanted = ["MAX_BOT_TOKEN", "BOT_TOKEN", "SHEET_ID", "GOOGLE_CREDENTIALS_JSON", "MANAGER_IDS"]
        seen = ", ".join(f"{k}={'есть' if os.getenv(k) else 'ПУСТО'}" for k in wanted)
        extra = [k for k in os.environ if "SHEET" in k.upper() and k != "SHEET_ID"]
        hint = f" Похожие по названию: {extra}." if extra else ""
        sys.exit(
            "Не хватает настроек (переменные окружения в панели хостинга или файл .env). "
            f"Бот видит: {seen}.{hint} Проверьте написание ключей: латиница, заглавные, без пробелов."
        )
