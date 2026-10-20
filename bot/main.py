"""
Точка входу. Запуск:  python -m bot.main

Порядок, як в етапі 1: логи → конфіг → бот → перевірки (fail fast) → polling.
Зараз (крок 0) бот лише запускається, перевіряє налаштування і відповідає на /start.
Тарифи, оплата і доступ з'являться на кроках 1–6.
"""
import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.types import Message

from bot.checks import check_admin_chat, check_channel, check_mono
from bot.config import load_config


async def run_checks(bot: Bot, config) -> None:
    """Якщо хоч одна перевірка не пройшла — не запускаємось (fail fast)."""
    problems = (await check_channel(bot, config.channel_id)
                + await check_admin_chat(bot, config.admin_chat_id))
    mono_problems, merchant = await check_mono(config.mono_token)
    problems += mono_problems
    # Попередження (⚠️) не зупиняють бота, справжні помилки — зупиняють.
    errors = [p for p in problems if not p.startswith("⚠️")]
    for p in problems:
        (logging.error if p in errors else logging.warning)(p)
    if errors:
        sys.exit("❌ Налаштування неповні — див. помилки вище (детальніше: python -m tools.check_setup)")
    logging.info("Перевірки пройдено. mono: %s", merchant)


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        stream=sys.stdout,  # stdout: Railway фарбує stderr у червоне
    )
    config = load_config()
    bot = Bot(config.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()

    # Тимчасовий /start — лише щоб побачити, що бот живий. Справжній — на кроці 2.
    @dp.message(CommandStart(), F.chat.type == "private")
    async def start(message: Message) -> None:
        await message.answer("💪 «Сила вдома» — бот у розробці. Скоро тут будуть тарифи.")

    try:
        await run_checks(bot, config)
        # ВІДМІННІСТЬ від етапу 1: там ми викидали події, що прийшли, поки бот був вимкнений
        # (щоб AI не відповідав на старе пачкою). Тут так не можна: серед них можуть бути
        # заявки на вступ від людей, які ВЖЕ заплатили. Тому drop_pending_updates=False.
        await bot.delete_webhook(drop_pending_updates=False)
        logging.info("Бот запущено")
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Бот зупинено")
