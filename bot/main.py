"""
Точка входу. Запуск:  python -m bot.main

Порядок: логи → конфіг → тарифи → бот → перевірки (fail fast) → БД → polling.
Крок 4: в одному процесі працюють ТРИ речі:
  1) polling — бот отримує повідомлення з Telegram;
  2) HTTP-сервер — приймає webhook від mono (bot/webhook.py);
  3) фонова звірка — раз на хвилину питає mono про незавершені платежі (bot/reconcile.py).
Крок 5: доступ до каналу — посилання із заявками (bot/access.py, bot/handlers/join.py).
Крок 6: планувальник (bot/scheduler.py) — 4-та фонова річ: нагадування, «закінчилась», видалення.
Крок 7: адмінка — сповіщення в адмін-групу (bot/notify.py), команди (bot/handlers/admin.py).
"""
import asyncio
import logging
import sys

from aiogram import Bot
from aiohttp import web
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand, BotCommandScopeChat

from bot import db
from bot.access import ensure_invite_link
from bot.app import build_dispatcher
from bot.checks import check_admin_chat, check_channel, check_mono
from bot.config import load_config
from bot.payments.mono import MonoProvider
from bot.reconcile import reconcile_forever
from bot.scheduler import run_forever as scheduler_forever
from bot.webhook import WEBHOOK_PATH, build_web_app
from bot.plans import load_plans

# Меню команд (кнопка «Меню» біля поля вводу). /paysupport — обов'язкова для бота з оплатами.
COMMANDS = [
    BotCommand(command="start", description="Почати спочатку"),
    BotCommand(command="my", description="Моя підписка"),
    BotCommand(command="help", description="Що вміє бот"),
    BotCommand(command="paysupport", description="Підтримка з оплати"),
]
# Окреме меню для адмін-групи (крок 7): там кнопка «Меню» показує команди власниці, а не клієнтські.
ADMIN_COMMANDS = [
    BotCommand(command="stats", description="Підписники і виручка"),
    BotCommand(command="user", description="Усе про людину: /user @username"),
    BotCommand(command="grant", description="Видати доступ: /grant @username 30"),
    BotCommand(command="revoke", description="Закрити доступ: /revoke @username"),
    BotCommand(command="admin", description="Довідка по командах"),
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

    runner = None
    background: list[asyncio.Task] = []
    try:
        await run_checks(bot, config)
        await db.init(config.db_path)
        demo_cmd = [BotCommand(command="demo", description="🧪 Як протестувати демо")] if config.demo_mode else []
        await bot.set_my_commands(demo_cmd + COMMANDS)
        await bot.set_my_commands(ADMIN_COMMANDS, scope=BotCommandScopeChat(chat_id=config.admin_chat_id))
        # Посилання на канал із заявками — бот створює його сам (один раз, потім бере з бази).
        access = await ensure_invite_link(bot, config.channel_id)
        dp["access"] = access

        # HTTP-сервер для webhook. Слухаємо 0.0.0.0 (усі адреси), щоб достукались ззовні
        # (тунель на Mac, Railway). Запускаємо завжди: GET / — перевірка «бот живий».
        runner = web.AppRunner(build_web_app(bot, provider, plans, config, access), access_log=None)
        await runner.setup()
        await web.TCPSite(runner, host="0.0.0.0", port=config.port).start()
        logging.info("HTTP-сервер: порт %s; webhook mono: %s", config.port,
                     config.webhook_url or f"вимкнено (локально: {WEBHOOK_PATH}, задай WEBHOOK_BASE_URL)")
        background.append(asyncio.create_task(reconcile_forever(bot, provider, plans, config, access)))
        background.append(asyncio.create_task(scheduler_forever(bot, plans, config, access)))
        logging.info("Демо-режим: %s", "так" if config.demo_mode else "ні")
        # ВІДМІННІСТЬ від етапу 1: там ми викидали події, що прийшли, поки бот був вимкнений
        # (щоб AI не відповідав на старе пачкою). Тут так не можна: серед них можуть бути
        # заявки на вступ від людей, які ВЖЕ заплатили. Тому drop_pending_updates=False.
        # УВАГА, дві різні речі з однаковою назвою: це webhook ТЕЛЕГРАМУ (спосіб отримувати
        # повідомлення). Ми його вимикаємо, бо працюємо через polling. Webhook від MONO (оплати) —
        # окремий, на нашому HTTP-сервері вище, і цей рядок його не стосується.
        await bot.delete_webhook(drop_pending_updates=False)
        logging.info("Бот запущено")
        await dp.start_polling(bot)
    finally:
        for task in background:
            task.cancel()
        if runner is not None:
            await runner.cleanup()
        await provider.close()
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Бот зупинено")
