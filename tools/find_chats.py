"""
Знайти ID каналу і адмін-групи.

Запуск:  python -m tools.find_chats
Потрібен лише BOT_TOKEN у .env (CHANNEL_ID і ADMIN_CHAT_ID ще можуть бути порожні).
Для PROD-бота:  ENV_FILE=.env.prod python -m tools.find_chats

Як це працює: коли бота додають у канал чи групу, Telegram створює подію
my_chat_member («статус бота в чаті змінився»). Telegram тримає непрочитані
події до 24 годин. Скрипт читає їх і показує, в яких чатах бот є.

Скрипт НЕ «з'їдає» події (не підтверджує їх), тому його можна запускати скільки
завгодно разів.

Важливо: поки сам бот запущений (python -m bot.main), цей скрипт не спрацює —
Telegram віддає оновлення лише одному одержувачу. Спочатку зупини бота.
"""
import asyncio
import os
import sys

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramConflictError, TelegramMigrateToChat
from dotenv import load_dotenv

load_dotenv(os.getenv("ENV_FILE", ".env"))  # ENV_FILE=.env.prod — для PROD-бота (див. bot/config.py)

KIND = {"channel": "📢 канал", "supergroup": "👥 супергрупа", "group": "👥 група (звич.)",
        "private": "👤 особистий"}


async def main() -> None:
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        sys.exit("❌ Не задано BOT_TOKEN у .env")

    bot = Bot(token)
    try:
        me = await bot.get_me()
        print(f"🤖 Бот: @{me.username}\n")
        try:
            updates = await bot.get_updates(
                timeout=0,
                allowed_updates=["my_chat_member", "message", "channel_post"],
            )
        except TelegramConflictError:
            sys.exit("❌ Бот уже запущений деінде (polling). Зупини python -m bot.main і спробуй ще раз.")

        chats: dict[int, tuple[str, str]] = {}  # id → (тип, назва)
        for u in updates:
            event = u.my_chat_member or u.message or u.channel_post
            if event and event.chat.type != "private":
                chats[event.chat.id] = (event.chat.type, event.chat.title or "")

        if not chats:
            print("Подій не знайдено. Що зробити:\n"
                  "  • прибери бота з каналу/групи і додай знову (адміном), АБО\n"
                  "  • напиши будь-яке повідомлення в адмін-групі,\n"
                  "і запусти скрипт ще раз.")
            return

        for chat_id, (kind, title) in chats.items():
            # Поточний статус (подія могла бути старою: бота додали, а потім прибрали)
            try:
                status = (await bot.get_chat_member(chat_id, me.id)).status
            except TelegramMigrateToChat as e:
                status = f"група стала супергрупою → новий ID {e.migrate_to_chat_id}"
            except TelegramAPIError:
                status = "немає доступу"
            print(f"{KIND.get(kind, kind):18} {chat_id:<16} «{title}»  — бот: {status}")

        print("\nКуди що вставити в .env:\n"
              "  📢 канал → CHANNEL_ID\n"
              "  👥 група → ADMIN_CHAT_ID\n"
              "Далі: python -m tools.check_setup")
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
