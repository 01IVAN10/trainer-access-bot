"""/start, /help, /paysupport і все, що не впізнали інші хендлери.

config і plans хендлер отримує ПАРАМЕТРАМИ: main.py кладе їх у диспетчер
(dp["config"] = …), і aiogram сам передає їх у функцію за назвою параметра.
Відмінність від барбершопу, де модулі викликали load_config() при імпорті:
тут хендлер не залежить від .env — його можна викликати в тесті з будь-яким конфігом.
"""
from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import Message

from bot import texts
from bot.config import Config
from bot.keyboards import BTN_HELP, START_PLANS, main_menu, plans_keyboard
from bot.plans import Plan, available_plans

router = Router(name="start")


@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject, config: Config, plans: dict[str, Plan]) -> None:
    await message.answer(texts.start_text(message.from_user.first_name, config.demo_mode),
                         reply_markup=main_menu)
    # /start plans — людина прийшла з посилання в повідомленні про заявку в канал:
    # одразу показуємо тарифи, щоб не шукала їх у меню.
    if command.args == START_PLANS:
        visible = available_plans(plans, config.demo_mode)
        await message.answer(texts.plans_text(visible), reply_markup=plans_keyboard(visible))


@router.message(Command("demo"))
async def cmd_demo(message: Message, config: Config) -> None:
    """Інструкція для тих, хто тестує демо. Поза демо-режимом команди «немає» —
    повідомлення піде далі, у fallback (у боті реального клієнта /demo не потрібен)."""
    if not config.demo_mode:
        return await unknown(message)
    await message.answer(texts.demo_text(config.demo_video_url, config.developer_contact),
                         reply_markup=main_menu)


@router.message(Command("help"))
@router.message(F.text == BTN_HELP)
async def cmd_help(message: Message, config: Config) -> None:
    await message.answer(texts.help_text(config.support_contact, config.demo_mode),
                         reply_markup=main_menu)


@router.message(Command("paysupport"))
async def cmd_paysupport(message: Message, config: Config) -> None:
    """Підтримка з оплати. Telegram очікує цю команду від кожного бота, що приймає платежі."""
    await message.answer(texts.paysupport_text(config.support_contact, config.demo_mode),
                         reply_markup=main_menu)


# ОСТАННІЙ роутер у main.py: сюди потрапляє все, що не впізнали інші.
fallback_router = Router(name="fallback")


@fallback_router.message()
async def unknown(message: Message) -> None:
    # Без AI (на відміну від барбершопу): у цьому боті всі дії — кнопками.
    # Але мовчати не можна — людина не зрозуміє, чи бот живий.
    await message.answer("Я працюю через меню внизу 👇\nЯкщо є питання — /help", reply_markup=main_menu)
