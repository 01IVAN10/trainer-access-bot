"""
Команди власниці каналу (крок 7). Працюють ЛИШЕ в адмін-групі (ADMIN_CHAT_ID).

Хто адмін? Не список ID у .env, а «хто є в адмін-групі». Власниця сама керує групою:
додала помічницю — та одразу може /grant; прибрала — і доступу немає. Без правки .env
і перезапуску. Ціна такого рішення (чесно сказати клієнтці): кожен учасник групи може
видавати й забирати доступ. Тому в групі — лише ті, кому довіряють гроші.

Чому перевірка chat.id надійна: ID чату Telegram підставляє сам, підробити його
повідомленням неможливо. /grant в особистих або в чужій групі, куди хтось додав бота, —
не пройде (тест «стати зловмисником» у tests/test_admin.py).

Кожна ручна дія — у журнал admin_actions (міграція №4): хто, кому, коли, що.
"""
import logging
from datetime import timedelta

from aiogram import Bot, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject, Filter
from aiogram.types import Message

from bot import admin_texts, db, stats, texts
from bot.access import ChannelAccess, Granted, Kick, grant_after_payment, kick
from bot.config import Config
from bot.keyboards import channel_keyboard
from bot.plans import Plan
from bot.subscription import has_access, utcnow

log = logging.getLogger(__name__)

MAX_GRANT_DAYS = 365  # «/grant @olena 300» замість 30 — помилка на клавіатурі; більше року — точно вона


class AdminChat(Filter):
    """Пропускає лише повідомлення з адмін-групи. config aiogram передасть сам (як у хендлери)."""

    async def __call__(self, message: Message, config: Config) -> bool:
        return message.chat.id == config.admin_chat_id


router = Router(name="admin")
router.message.filter(AdminChat())

GRANT_USAGE = "Як: /grant <code>@username 30</code> або /grant <code>123456789 30</code> (днів: 1–365)"
REVOKE_USAGE = "Як: /revoke <code>@username</code> або /revoke <code>123456789</code>"
USER_USAGE = "Як: /user <code>@username</code> або /user <code>123456789</code>"


def grant_plan_id(current_plan_id: str | None, plans: dict[str, Plan]) -> str:
    """З якого тарифу брати нагадування і пільговий період для ручного доступу.

    Поточний тариф — якщо він є і НЕ демо. Інакше — перший платний: 30 днів «подарунку»
    з демо-налаштуваннями (нагадування за 2 хв, grace 1 хв) — це видалення через хвилину
    після кінця без попередження. Та сама пастка, що з правилом демо-тарифу (крок 2).
    """
    current = plans.get(current_plan_id) if current_plan_id else None
    if current is not None and not current.demo:
        return current.id
    paid = [p for p in plans.values() if not p.demo]
    return (paid or list(plans.values()))[0].id


@router.message(Command("admin", "help"))
async def admin_help(message: Message) -> None:
    await message.answer(admin_texts.ADMIN_HELP)


@router.message(Command("stats"))
async def cmd_stats(message: Message, config: Config, plans: dict[str, Plan]) -> None:
    now = utcnow()
    result = stats.compute(
        await db.live_subscriptions(), await db.paid_since(now - timedelta(days=30)), plans, now,
        config.timezone,
        users=await db.count("SELECT COUNT(*) FROM users"),
        paying_users=await db.count(
            "SELECT COUNT(DISTINCT user_id) FROM payments WHERE status = 'success' AND applied_at IS NOT NULL"),
        removed=await db.count("SELECT COUNT(*) FROM subscriptions WHERE removed = 1"),
    )
    await message.answer(admin_texts.stats_text(result, now, config.timezone, config.demo_mode))


@router.message(Command("user"))
async def cmd_user(message: Message, command: CommandObject, config: Config, plans: dict[str, Plan]) -> None:
    if not command.args:
        return await message.answer(USER_USAGE)
    user = await db.find_user(command.args)
    if user is None:
        return await message.answer(admin_texts.NOT_FOUND)
    sub = await db.get_subscription(user["user_id"])
    await message.answer(admin_texts.user_card(
        user, sub, plans.get(sub.plan_id) if sub else None,
        await db.user_payments(user["user_id"]), await db.user_admin_actions(user["user_id"]), plans,
        await db.has_join_request(user["user_id"]), utcnow(), config.timezone))


@router.message(Command("grant"))
async def cmd_grant(message: Message, command: CommandObject, bot: Bot, config: Config,
                    plans: dict[str, Plan], access: ChannelAccess) -> None:
    args = (command.args or "").split()
    if len(args) != 2 or not args[1].isdigit() or not 1 <= int(args[1]) <= MAX_GRANT_DAYS:
        return await message.answer(GRANT_USAGE)
    user = await db.find_user(args[0])
    if user is None:
        return await message.answer(admin_texts.NOT_FOUND)
    user_id, days = user["user_id"], int(args[1])

    current = await db.get_subscription(user_id)
    plan_id = grant_plan_id(current.plan_id if current else None, plans)
    sub = await db.grant(user_id, plan_id, timedelta(days=days), message.from_user.id, utcnow())
    log.info("ADMIN | %s видав user=%s +%s дн. → до %s", message.from_user.id, user_id, days, sub.expires_at)

    # Далі — як після оплати: висяча заявка → схвалити; інакше — посилання. Збій Telegram тут
    # НЕ скасовує доступ: він уже в базі, заявку бот схвалить, коли людина її подасть.
    try:
        granted = await grant_after_payment(bot, access, user_id)
    except TelegramAPIError as e:
        log.warning("ADMIN | grant user=%s: не вдалося схвалити заявку: %s", user_id, e)
        granted = Granted.LINK
    try:
        await bot.send_message(user_id, texts.granted_by_admin_text(sub, config.timezone, granted.value),
                               reply_markup=channel_keyboard(access.invite_link, granted.value))
        delivered = True
    except TelegramAPIError as e:
        log.warning("ADMIN | grant user=%s: не вдалося написати: %s", user_id, e)
        delivered = False
    await message.answer(admin_texts.granted_text(user, sub, plans.get(sub.plan_id), days, delivered,
                                                  config.timezone, access.invite_link))


@router.message(Command("revoke"))
async def cmd_revoke(message: Message, command: CommandObject, bot: Bot, config: Config,
                     plans: dict[str, Plan], access: ChannelAccess) -> None:
    args = (command.args or "").split()
    if len(args) != 1:
        return await message.answer(REVOKE_USAGE)
    user = await db.find_user(args[0])
    if user is None:
        return await message.answer(admin_texts.NOT_FOUND)
    user_id, now = user["user_id"], utcnow()

    # Спершу база (доступ закрито В ТУ Ж МИТЬ — заявку вже не схвалимо), потім канал.
    before = await db.revoke(user_id, message.from_user.id, now)
    had_access = has_access(before, plans.get(before.plan_id) if before else None, now)
    log.info("ADMIN | %s закрив доступ user=%s (доступ був: %s)", message.from_user.id, user_id, had_access)

    # На відміну від планувальника, тут НЕ відкочуємо позначку при збої: поруч жива людина,
    # яка бачить результат. Чесно кажемо «у базі закрито, з каналу не вийшло — повторіть».
    # Планувальник же працює сам, без свідків, — тому там «відкотити і тихо спробувати ще».
    error, result = None, None
    try:
        result = await kick(bot, access, user_id)
    except TelegramAPIError as e:
        error = e.message
        log.warning("ADMIN | revoke user=%s: прибрати з каналу не вдалося: %s", user_id, e)

    if had_access or result == Kick.KICKED:
        try:
            await bot.send_message(user_id, texts.revoked_by_admin_text(config.support_contact))
        except TelegramAPIError as e:
            log.info("ADMIN | revoke user=%s: не вдалося написати: %s", user_id, e)
    await message.answer(admin_texts.revoked_text(user, had_access, result.value if result else "", error))
