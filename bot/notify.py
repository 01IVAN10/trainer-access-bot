"""
Сповіщення в адмін-групу (крок 7). Одна функція — щоб усі місця, які пишуть власниці,
однаково поводились при збої.

Головне правило: збій сповіщення НІКОЛИ не ламає основну дію. Гроші зараховано, людину
видалено — це вже сталося. Якщо адмін-група недоступна (бота прибрали з групи, Telegram
тимчасово лежить), пишемо в лог і йдемо далі.
"""
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from bot.config import Config

log = logging.getLogger(__name__)


async def tell_admins(bot: Bot, config: Config, text: str) -> None:
    try:
        await bot.send_message(config.admin_chat_id, text)
    except TelegramAPIError as e:
        log.warning("Не вдалося написати в адмін-групу %s: %s", config.admin_chat_id, e)
