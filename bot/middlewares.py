"""
Middleware з етапу 1 (барбершоп) — майже без змін.

Middleware — код, що виконується ДО хендлера для кожного повідомлення:
записати клієнта в базу, відсікти флуд. Без них це довелося б дописувати
в кожен хендлер (і колись забути в одному).
"""
import logging
import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message

from bot import db

log = logging.getLogger(__name__)


class UserTrackingMiddleware(BaseMiddleware):
    """Записує користувача в базу при будь-якій дії: повідомлення або натискання кнопки.

    НОВЕ порівняно з етапом 1: тепер це ОБОВ'ЯЗКОВО, а не лише для статистики.
    Таблиця payments посилається на users (REFERENCES), тож створити платіж для
    людини, якої немає в users, база не дасть.
    """

    async def __call__(self, handler: Callable[[Any, dict[str, Any]], Awaitable[Any]],
                       event: Message | CallbackQuery, data: dict[str, Any]) -> Any:
        user = event.from_user
        chat = event.chat if isinstance(event, Message) else (event.message.chat if event.message else None)
        if user is not None and chat is not None and chat.type == "private":
            await db.upsert_user(user.id, user.username, user.full_name)
        return await handler(event, data)


class AntiFloodMiddleware(BaseMiddleware):
    """Не більше `limit` повідомлень за `window` секунд від одного користувача (лише особисті чати).

    Тримаємо в пам'яті: вікно — секунди, після перезапуску скидати не шкода.
    """

    def __init__(self, limit: int = 5, window: float = 10.0) -> None:
        self.limit = limit
        self.window = window
        self._times: dict[int, deque] = defaultdict(deque)
        self._warned_until: dict[int, float] = {}

    async def __call__(self, handler: Callable[[Message, dict[str, Any]], Awaitable[Any]],
                       event: Message, data: dict[str, Any]) -> Any:
        user = event.from_user
        if user is None or event.chat.type != "private":
            return await handler(event, data)

        now = time.monotonic()
        times = self._times[user.id]
        while times and now - times[0] > self.window:
            times.popleft()
        times.append(now)
        if len(times) <= self.limit:
            return await handler(event, data)

        if self._warned_until.get(user.id, 0) < now:
            self._warned_until[user.id] = now + self.window
            log.info("Флуд від %s: %s повідомлень за %s с", user.id, len(times), self.window)
            await event.answer("Ви пишете дуже швидко 🙂 Зачекайте кілька секунд.")
        return None
