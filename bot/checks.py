"""
Перевірки налаштувань при старті. Розвиток check_admin_chat() з етапу 1.

Ідея та сама: якщо щось налаштовано не так, бот має ГУЧНО впасти при запуску
з поясненням, що виправити, а не тихо зламатися на першій оплаті клієнта.

Тут ми не віримо інтерфейсу Telegram «на око», а питаємо в API:
- чи бот справді адмін каналу і чи має ПОТРІБНІ права;
- чи бот є в адмін-групі;
- чи токен mono живий.

Кожна функція повертає список проблем (порожній = все добре).
Використовують і main.py (fail fast), і tools/check_setup.py (звіт людині).
"""
import logging
import ssl

import aiohttp
import certifi
from aiogram import Bot
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.exceptions import TelegramAPIError, TelegramMigrateToChat

log = logging.getLogger(__name__)

MONO_API = "https://api.monobank.ua"

# Набір довірених сертифікатів з пакета certifi, а не системний.
# Чому: Python з python.org на macOS НЕ бачить сертифікати системи → будь-який HTTPS-запит
# падає з CertificateError. aiogram робить так само всередині — тому Telegram працював,
# а наш окремий запит до mono — ні. На Railway (Linux) теж працює однаково.
SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())


def _rights_link(bot_username: str) -> str:
    """Посилання, яке відкриває вибір каналу і ОДРАЗУ пропонує боту потрібні права.

    Корисно, коли в налаштуваннях каналу немає галочки з потрібною назвою:
    Telegram сам покаже, які права бот просить.
    """
    return f"https://t.me/{bot_username}?startchannel&admin=invite_users+restrict_members"


async def check_channel(bot: Bot, channel_id: int) -> list[str]:
    problems: list[str] = []
    me = await bot.get_me()
    try:
        chat = await bot.get_chat(channel_id)
        member = await bot.get_chat_member(channel_id, me.id)
    except TelegramAPIError as e:
        return [f"Канал {channel_id}: бот не бачить його ({e.message}). "
                "Бота додано в канал? ID правильний? → python -m tools.find_chats"]

    if chat.type != ChatType.CHANNEL:
        problems.append(f"CHANNEL_ID={channel_id} — це «{chat.title}» типу {chat.type}, а не канал.")

    # Публічний канал (є @адреса) — увійти може будь-хто без оплати.
    if chat.username:
        problems.append(f"Канал «{chat.title}» ПУБЛІЧНИЙ (@{chat.username}): будь-хто зайде "
                        "без оплати. Зроби його приватним.")

    if member.status != ChatMemberStatus.ADMINISTRATOR:
        problems.append(f"Бот не адміністратор каналу «{chat.title}» (статус: {member.status}). "
                        f"Додай його адміном: {_rights_link(me.username)}")
        return problems

    # Права, без яких бот не виконає свою роботу:
    if not member.can_invite_users:
        problems.append("Немає права «Додавати підписників» (can_invite_users): "
                        "бот не створить посилання і не схвалить заявку.")
    if not member.can_restrict_members:
        problems.append("Немає права блокувати/видаляти учасників (can_restrict_members): "
                        "бот не зможе видалити тих, хто не продовжив. "
                        f"Спробуй перепризначити бота через: {_rights_link(me.username)}")
    # Зайве право — не помилка, але небезпечно: злитий токен = захоплений канал.
    if member.can_promote_members:
        problems.append("⚠️ У бота є право «Додавати адміністраторів». Йому воно не потрібне — "
                        "забери (принцип найменших прав).")
    return problems


async def check_admin_chat(bot: Bot, admin_chat_id: int) -> list[str]:
    me = await bot.get_me()
    try:
        chat = await bot.get_chat(admin_chat_id)
        member = await bot.get_chat_member(admin_chat_id, me.id)
    except TelegramMigrateToChat as e:
        # Звичайна група стала супергрупою → старий ID більше не працює.
        return [f"Група {admin_chat_id} перетворилася на супергрупу. Новий ID: "
                f"{e.migrate_to_chat_id} — заміни ADMIN_CHAT_ID у .env."]
    except TelegramAPIError as e:
        return [f"Адмін-група {admin_chat_id}: бот не бачить її ({e.message}). "
                "Бота додано в групу? → python -m tools.find_chats"]

    problems: list[str] = []
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        problems.append(f"ADMIN_CHAT_ID={admin_chat_id} — це «{chat.title}» типу {chat.type}, а не група.")
    if chat.type == ChatType.GROUP:
        # Не помилка, але міна сповільненої дії: при зміні деяких налаштувань Telegram
        # сам перетворює групу на супергрупу, ID змінюється — і бот «тихо» перестає писати.
        problems.append("⚠️ Це звичайна група, не супергрупа. Перетвори її зараз (Налаштування групи → "
                        "«Історія чату для нових учасників» → Видима), отримай новий ID -100… "
                        "через python -m tools.find_chats — інакше ID може змінитися пізніше сам.")
    if member.status != ChatMemberStatus.ADMINISTRATOR:
        # Не адмін → Telegram не надсилає боту звичайні повідомлення з групи (лише команди).
        problems.append(f"Бот не адміністратор групи «{chat.title}» (статус: {member.status}).")
    return problems


async def check_mono(token: str) -> tuple[list[str], str | None]:
    """Перевіряє токен mono запитом «дані мерчанта». Повертає (проблеми, назва мерчанта)."""
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10), trust_env=True,
                                         connector=aiohttp.TCPConnector(ssl=SSL_CONTEXT)) as session:
            async with session.get(f"{MONO_API}/api/merchant/details",
                                   headers={"X-Token": token}) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return [], data.get("merchantName") or data.get("merchantId")
                if resp.status in (401, 403):
                    return ["MONO_TOKEN недійсний (mono відповів 403). Перевір, що скопійовано повністю."], None
                return [f"mono відповів {resp.status}: {(await resp.text())[:200]}"], None
    except (aiohttp.ClientError, TimeoutError) as e:
        # Мережа — не обов'язково наша помилка. Пишемо, але різниця важлива для діагностики.
        return [f"Не вдалося з'єднатися з api.monobank.ua ({type(e).__name__}). Перевір інтернет."], None
