"""
Перевірка всіх налаштувань кроку 0 — одним запуском.

Запуск:  python -m tools.check_setup

Перевіряє: токен бота, канал (приватний? бот адмін? потрібні права?),
адмін-групу, токен mono. Ті самі перевірки бот робить при кожному старті
(bot/main.py) — тут вони просто з детальним звітом.
"""
import asyncio
import sys

from aiogram import Bot
from aiogram.exceptions import TelegramUnauthorizedError

from bot.checks import check_admin_chat, check_channel, check_mono
from bot.config import load_config


def _report(title: str, problems: list[str], ok_text: str = "") -> bool:
    if problems:
        print(f"❌ {title}")
        for p in problems:
            print(f"     • {p}")
        return False
    print(f"✅ {title}{(' — ' + ok_text) if ok_text else ''}")
    return True


async def main() -> None:
    cfg = load_config()
    bot = Bot(cfg.bot_token)
    all_ok = True
    try:
        try:
            me = await bot.get_me()
        except TelegramUnauthorizedError:
            sys.exit("❌ BOT_TOKEN недійсний. Перевір токен у BotFather.")
        print(f"✅ Бот: @{me.username}")

        all_ok &= _report("Канал", await check_channel(bot, cfg.channel_id))
        all_ok &= _report("Адмін-група", await check_admin_chat(bot, cfg.admin_chat_id))
        mono_problems, merchant = await check_mono(cfg.mono_token)
        all_ok &= _report("monobank", mono_problems, f"мерчант: {merchant}")
    finally:
        await bot.session.close()

    print("\n🎉 Крок 0 готовий." if all_ok else "\nВиправ пункти з ❌ і запусти ще раз.")
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    asyncio.run(main())
