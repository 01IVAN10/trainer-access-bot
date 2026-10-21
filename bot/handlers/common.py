"""Дрібниці, потрібні кільком хендлерам."""
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery


async def edit(callback: CallbackQuery, text: str, markup=None) -> None:
    """Змінити повідомлення з кнопками. Якщо текст той самий (двічі натиснули) —
    Telegram відповідає помилкою «message is not modified»; це не проблема, ігноруємо."""
    try:
        await callback.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise
