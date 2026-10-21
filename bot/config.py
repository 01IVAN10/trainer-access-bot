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
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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
    support_contact: str   # куди писати з питань оплати (/paysupport) — обов'язково для бота з оплатами
    demo_mode: bool        # демо для портфоліо: показувати демо-тариф і позначки «тестова оплата»
    timezone: str          # у якому часовому поясі показувати дати людям
    db_path: str
    # Публічна адреса бота (https://…) — куди mono надсилатиме webhook. Порожньо → без webhook:
    # оплату підтверджують кнопка «Перевірити» і фонова звірка (так зручно локально на Mac).
    webhook_base_url: str = ""
    port: int = 8080       # на якому порту слухає HTTP-сервер бота (Railway задає PORT сам)

    @property
    def webhook_url(self) -> str | None:
        return f"{self.webhook_base_url}/webhooks/mono" if self.webhook_base_url else None


def _require(name: str) -> str:
    """Повертає змінну або зупиняє програму зі зрозумілою помилкою (fail fast)."""
    value = os.getenv(name, "").strip()
    if not value:
        sys.exit(f"❌ Не задано {name}. Заповни його у файлі .env (див. .env.example)")
    return value


def _require_chat_id(name: str, channel: bool) -> int:
    """ID каналу чи групи: обов'язковий, ціле ВІД'ЄМНЕ число.

    Як Telegram нумерує чати:
      користувач        → додатне число         (123456789)
      звичайна група    → від'ємне              (-4512345678)
      супергрупа, канал → від'ємне з -100…      (-1001234567890)
    Канал ЗАВЖДИ має -100…, тому для CHANNEL_ID перевіряємо суворіше.
    Помилку краще побачити тут, при старті, а не на першій оплаті.
    """
    raw = _require(name)
    try:
        value = int(raw)
    except ValueError:
        sys.exit(f"❌ {name} має бути цілим числом, а зараз: {raw!r}")
    if value > 0:
        sys.exit(f"❌ {name}={raw}: додатне число — це ID користувача, а не чату. "
                 "Дізнатися правильний: python -m tools.find_chats")
    if channel and not raw.startswith("-100"):
        sys.exit(f"❌ {name}={raw}: ID каналу завжди починається з -100. "
                 "Дізнатися правильний: python -m tools.find_chats")
    return value


def _bool(name: str, default: bool = False) -> bool:
    """Так/ні з .env. Незрозуміле значення — помилка, а не тихе False:
    інакше DEMO_MODE=yes-please вимкнув би демо, і ніхто б не зрозумів чому."""
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    if raw in ("1", "true", "yes", "on", "так"):
        return True
    if raw in ("0", "false", "no", "off", "ні"):
        return False
    sys.exit(f"❌ {name}={raw!r}: очікую 1 або 0")


def _optional_port() -> int:
    raw = os.getenv("PORT", "").strip()
    if not raw:
        return 8080
    if not raw.isdigit() or not 0 < int(raw) < 65536:
        sys.exit(f"❌ PORT={raw!r}: очікую номер порту (наприклад, 8080)")
    return int(raw)


def load_config() -> Config:
    cfg = Config(
        bot_token=_require("BOT_TOKEN"),
        channel_id=_require_chat_id("CHANNEL_ID", channel=True),
        admin_chat_id=_require_chat_id("ADMIN_CHAT_ID", channel=False),
        mono_token=_require("MONO_TOKEN"),
        support_contact=_require("SUPPORT_CONTACT"),
        demo_mode=_bool("DEMO_MODE"),
        timezone=os.getenv("TIMEZONE", "Europe/Kyiv").strip(),
        db_path=os.getenv("DB_PATH", "storage/bot.db"),
        webhook_base_url=os.getenv("WEBHOOK_BASE_URL", "").strip().rstrip("/"),
        port=_optional_port(),
    )
    # mono відправляє webhook лише на https; http-адреса — майже напевно помилка копіювання.
    if cfg.webhook_base_url and not cfg.webhook_base_url.startswith("https://"):
        sys.exit(f"❌ WEBHOOK_BASE_URL має починатися з https://, а зараз: {cfg.webhook_base_url!r}")
    try:
        ZoneInfo(cfg.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        sys.exit(f"❌ TIMEZONE={cfg.timezone!r} — невідомий часовий пояс (приклад: Europe/Kyiv)")
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
    print(f"   SUPPORT:       {cfg.support_contact}")
    print(f"   DEMO_MODE:     {'так' if cfg.demo_mode else 'ні'}")
    print(f"   TIMEZONE:      {cfg.timezone}")
    print(f"   DB_PATH:       {cfg.db_path}")
    print(f"   WEBHOOK:       {cfg.webhook_url or 'вимкнено (кнопка + фонова звірка)'}")
    print(f"   PORT:          {cfg.port}")
