"""
«Що робити, коли дізналися статус платежу» — ОДНЕ місце на весь бот.

Дізнатися статус можна трьома шляхами:
  1) людина натиснула «Я оплатив — перевірити»   (handlers/pay.py)
  2) mono надіслав webhook                       (bot/webhook.py)
  3) фонова звірка спитала mono сама             (bot/reconcile.py)
Усі три викликають record_status(). Якби в кожного була своя копія логіки,
рано чи пізно вони розійшлися б: наприклад, webhook забув би перевірку суми.

Шляхи можуть спрацювати ОДНОЧАСНО (людина тисне кнопку саме тоді, коли прийшов webhook).
Тому все тут ідемпотентне: статус — лише вперед, зарахування — рівно один раз (крок 1).
"""
import logging
from datetime import datetime

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError

from bot import db, texts
from bot.access import ChannelAccess, grant_after_payment
from bot.keyboards import channel_keyboard
from bot.plans import Plan
from bot.subscription import Subscription

log = logging.getLogger(__name__)


async def record_status(payment_id: int, status: str, plans: dict[str, Plan],
                        now: datetime) -> Subscription | None:
    """Записує статус і, якщо це success, зараховує платіж у підписку.

    Повертає підписку, ЯКЩО саме цей виклик її продовжив (тоді треба повідомити людину).
    None — нічого нового: ще не оплачено, або вже зараховано іншим шляхом.
    """
    await db.set_payment_status(payment_id, status)  # остаточний статус не перезапишеться
    if status != "success":
        return None
    sub = await db.apply_payment(payment_id, plans, now)  # рівно один раз
    if sub is not None:
        log.info("PAID | payment=#%s | user=%s | до %s", payment_id, sub.user_id, sub.expires_at)
    return sub


async def notify_paid(bot: Bot, sub: Subscription, plans: dict[str, Plan], tz: str,
                      access: ChannelAccess) -> None:
    """Впустити в канал (якщо людина вже подавала заявку) і повідомити про оплату.
    Для webhook і звірки — коли людина нічого не натискала.

    Помилка Telegram (людина заблокувала бота) НЕ має зламати обробку платежу:
    гроші зараховано, підписка є — просто не змогли сказати."""
    try:
        granted = await grant_after_payment(bot, access, sub.user_id)
        await bot.send_message(sub.user_id,
                               texts.payment_success_text(plans.get(sub.plan_id), sub, tz, granted.value),
                               reply_markup=channel_keyboard(access.invite_link, granted.value))
    except TelegramForbiddenError as e:
        # Людина ніколи не натискала /start (або заблокувала бота). Доступ уже дано — це головне.
        log.warning("Не вдалося повідомити user=%s про оплату (не запускав бота / заблокував): %s",
                    sub.user_id, e)
    except TelegramAPIError as e:
        log.warning("Не вдалося повідомити user=%s про оплату: %s", sub.user_id, e)
