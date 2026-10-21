"""
Точка входу. Запуск:  python -m bot.main

Порядок: логи → конфіг → тарифи → бот → перевірки (fail fast) → БД → polling.
Крок 3: оплата через mono (рахунок + «Перевірити»). Webhook і доступ — кроки 4–5.
"""
import asyncio
import logging
import sys

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand

from bot import db
from bot.app import build_dispatcher
from bot.checks import check_admin_chat, check_channel, check_mono
from bot.config import load_config
from bot.payments.mono import MonoProvider
from bot.plans import load_plans

# Меню команд (кнопка «Меню» біля поля вводу). /paysupport — обов'язкова для бота з оплатами.
COMMANDS = [
    BotCommand(command="start", description="Почати спочатку"),
    BotCommand(command="my", description="Моя підписка"),
    BotCommand(command="help", description="Що вміє бот"),
    BotCommand(command="paysupport", description="Підтримка з оплати"),
]


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
    # Тарифи — до всього іншого: помилка в YAML має зупинити запуск одразу (fail fast).
    try:
        plans = load_plans()
    except ValueError as e:
        sys.exit(f"❌ {e}")
    logging.info("Тарифи: %s", ", ".join(f"{p.id} ({p.price_uah} грн)" for p in plans.values()))

    bot = Bot(config.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    provider = MonoProvider(config.mono_token)
    dp = build_dispatcher(config, plans, provider)  # роутери, фільтри, middleware — див. bot/app.py

    try:
        await run_checks(bot, config)
        await db.init(config.db_path)
        await bot.set_my_commands(COMMANDS)
        logging.info("Демо-режим: %s", "так" if config.demo_mode else "ні")
        # ВІДМІННІСТЬ від етапу 1: там ми викидали події, що прийшли, поки бот був вимкнений
        # (щоб AI не відповідав на старе пачкою). Тут так не можна: серед них можуть бути
        # заявки на вступ від людей, які ВЖЕ заплатили. Тому drop_pending_updates=False.
        await bot.delete_webhook(drop_pending_updates=False)
        logging.info("Бот запущено")
        await dp.start_polling(bot)
    finally:
        await provider.close()
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Бот зупинено")
