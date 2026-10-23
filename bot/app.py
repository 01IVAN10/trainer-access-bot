"""
Складання диспетчера: роутери, фільтри, middleware.

Окремою функцією, а не всередині main(), щоб ТЕСТИ збирали бота РІВНО ТАК САМО,
як продакшн. Якби тести мали свою копію складання, вони могли б пройти, а справжній
бот — ні (наприклад, забули підключити роутер у main.py).
"""
from aiogram import Dispatcher, F

from bot.config import Config
from bot.handlers import admin, join, my, pay, plans as plans_handlers, start
from bot.middlewares import AntiFloodMiddleware, UserTrackingMiddleware
from bot.payments import PaymentProvider
from bot.plans import Plan


def build_dispatcher(config: Config, plans: dict[str, Plan], provider: PaymentProvider,
                     flood_limit: int = 5) -> Dispatcher:
    """Викликати ОДИН раз на процес: роутери — одиночки, другий диспетчер їх не отримає."""
    # Спільні дані для всіх хендлерів: aiogram передасть їх у функцію за назвою параметра
    # (config: Config, plans: dict, provider: PaymentProvider). Замість глобальних змінних.
    # provider — mono в main.py, фальшивий банк у тестах.
    dp = Dispatcher(config=config, plans=plans, provider=provider)

    # Клієнтські роутери — ЛИШЕ для особистих чатів (як у барбершопі): інакше бот
    # відповідав би меню на кожне повідомлення в адмін-групі.
    client_routers = (start.router, plans_handlers.router, pay.router, my.router, start.fallback_router)
    for r in client_routers:
        r.message.filter(F.chat.type == "private")

    # Порядок middleware: антифлуд першим (флуд не доходить навіть до бази), потім облік.
    dp.message.outer_middleware(AntiFloodMiddleware(limit=flood_limit, window=10))
    dp.message.outer_middleware(UserTrackingMiddleware())
    dp.callback_query.outer_middleware(UserTrackingMiddleware())

    # fallback — ОСТАННІЙ: він ловить усе, що не впізнали інші.
    # join — заявки на вступ у канал (не повідомлення, тому фільтр «особисті чати» йому не потрібен).
    # admin — команди власниці; свій фільтр «лише адмін-група» (bot/handlers/admin.py).
    # dp["access"] (посилання на канал) main.py додасть після старту — бот створює посилання сам.
    dp.include_routers(join.router, admin.router, *client_routers)
    return dp
