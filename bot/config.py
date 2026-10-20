"""
Налаштування бота. Той самий підхід, що в етапі 1 (барбершоп):
секрети — у .env (локально) або у Variables на Railway, а не в коді.

Новe тут — ДВА оточення з однаковим кодом:
  DEV  (твій Mac, файл .env)       → dev-бот, канал [DEV], адмін-група [DEV], storage/bot.db
  PROD (Railway, панель Variables)  → основний бот, основний канал і група, /data/bot.db
Код не знає, де він працює, — він просто читає змінні.

Перевірка:  python -m bot.config
"""
import os
import sys
from dataclasses import dataclass

from dotenv import load_dotenv

# Шукає .env у поточній папці. Якщо змінна вже задана в оточенні (як на сервері) —
# .env її НЕ перезапише.
load_dotenv()


@dataclass(frozen=True)  # frozen = налаштування не можна випадково змінити під час роботи
class Config:
    bot_token: str
    channel_id: int        # закритий канал, доступ до якого продаємо
    admin_chat_id: int     # група власника: сповіщення про оплати, /stats
    mono_token: str        # токен monobank acquiring (тестовий або бойовий)
    db_path: str


def _require(name: str) -> str:
    """Повертає змінну або зупиняє програму зі зрозумілою помилкою (fail fast)."""
    value = os.getenv(name, "").strip()
    if not value:
        sys.exit(f"❌ Не задано {name}. Заповни його у файлі .env (див. .env.example)")
    return value


def _require_chat_id(name: str) -> int:
    """ID групи чи каналу: обов'язковий, ціле число, починається з -100.

    -100… — так Telegram позначає супергрупи й канали. Якщо ID без -100,
    найімовірніше, скопійовано не те (наприклад, ID користувача).
    Помилку краще побачити тут, при старті, а не на першій оплаті.
    """
    raw = _require(name)
    try:
        value = int(raw)
    except ValueError:
        sys.exit(f"❌ {name} має бути цілим числом, а зараз: {raw!r}")
    if not raw.startswith("-100"):
        sys.exit(f"❌ {name}={raw}: ID каналу чи групи має починатися з -100. "
                 "Дізнатися правильний: python -m tools.find_chats")
    return value


def load_config() -> Config:
    cfg = Config(
        bot_token=_require("BOT_TOKEN"),
        channel_id=_require_chat_id("CHANNEL_ID"),
        admin_chat_id=_require_chat_id("ADMIN_CHAT_ID"),
        mono_token=_require("MONO_TOKEN"),
        db_path=os.getenv("DB_PATH", "storage/bot.db"),
    )
    # Типова помилка копіювання: один і той самий ID в обох полях.
    if cfg.channel_id == cfg.admin_chat_id:
        sys.exit("❌ CHANNEL_ID і ADMIN_CHAT_ID однакові. Канал (що продаємо) і "
                 "адмін-група (куди пишуть сповіщення) — це різні чати.")
    return cfg


if __name__ == "__main__":
    cfg = load_config()
    # Секрети ніколи не друкуємо повністю — лише перші символи для перевірки.
    print("✅ Конфіг завантажено")
    print(f"   BOT_TOKEN:     {cfg.bot_token[:6]}…")
    print(f"   CHANNEL_ID:    {cfg.channel_id}")
    print(f"   ADMIN_CHAT_ID: {cfg.admin_chat_id}")
    print(f"   MONO_TOKEN:    {cfg.mono_token[:4]}…")
    print(f"   DB_PATH:       {cfg.db_path}")
