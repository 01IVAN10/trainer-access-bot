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

from bot import admin_texts, db, texts
from bot.access import ChannelAccess, grant_after_payment
from bot.config import Config
from bot.keyboards import channel_keyboard, retry_keyboard
from bot.notify import tell_admins
from bot.plans import Plan
from bot.subscription import Subscription

log = logging.getLogger(__name__)


async def record_status(payment_id: int, status: str, plans: dict[str, Plan], now: datetime,
                        bot: Bot, config: Config) -> Subscription | None:
    """Записує статус і, якщо це success, зараховує платіж у підписку.

    Повертає підписку, ЯКЩО саме цей виклик її продовжив (тоді треба повідомити людину).
    None — нічого нового: ще не оплачено, або вже зараховано іншим шляхом.

    Крок 7: сповіщення власниці — ТУТ, а не в кожному з трьох шляхів. Що однакове для всіх
    шляхів, живе всередині спільної функції (не забудемо в одному з них); що різне — зовні
    (кнопка редагує повідомлення, webhook і звірка надсилають нове — notify_paid).
    І рівно один раз: сповіщаємо лише той виклик, чий apply_payment реально зарахував.
    """
    await db.set_payment_status(payment_id, status)  # остаточний статус не перезапишеться
    if status != "success":
        return None
    payment = await db.get_payment(payment_id)
    prev = await db.get_subscription(payment["user_id"])  # ДО зарахування: нова / продовження / повернення
    sub = await db.apply_payment(payment_id, plans, now)  # рівно один раз
    if sub is not None:
        log.info("PAID | payment=#%s | user=%s | до %s", payment_id, sub.user_id, sub.expires_at)
        try:  # гроші вже зараховано — що б не сталося зі сповіщенням, людина має отримати доступ
            kind = admin_texts.payment_kind(prev, plans.get(prev.plan_id) if prev else None, now)
            await tell_admins(bot, config, admin_texts.paid_text(
                await db.get_user(sub.user_id), sub, plans.get(sub.plan_id), payment, kind, config.timezone))
        except Exception:
            log.exception("Не вдалося сповістити адмінів про платіж #%s", payment_id)
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


OPEN = ("created", "processing")


async def close_invoice(bot: Bot, payment: dict, status: str) -> None:
    """Крок 8: оплату підтвердили webhook або звірка — прибрати кнопки зі старого рахунку.

    Без цього під уже оплаченим рахунком лишаються «💳 Оплатити» і «Перевірити» (inline-кнопки
    живуть вічно). Людина натисне «Оплатити» ще раз — і побачить сторінку mono «рахунок оплачено»
    або згорілий рахунок. Плутанина на рівному місці.

    payment — запис ДО оновлення статусу: якщо він уже був остаточним, рахунок закрив хтось інший.
    Кнопка «Перевірити» сюди не ходить — вона й так редагує саме це повідомлення.
    """
    message_id = payment.get("message_id")
    if message_id is None or payment["status"] not in OPEN or status in OPEN:
        return
    if status == "success":
        text, markup = texts.INVOICE_PAID, None
    else:  # failure / expired — одразу кнопка «спробувати ще раз»
        text = texts.PAYMENT_FAILED if status == "failure" else texts.PAYMENT_EXPIRED
        markup = retry_keyboard(payment["plan_id"])
    try:
        await bot.edit_message_text(text, chat_id=payment["user_id"], message_id=message_id, reply_markup=markup)
    except TelegramAPIError as e:
        # Повідомлення видалили, текст той самий («not modified») тощо — не страшно, це косметика.
        log.info("Рахунок #%s: не вдалося оновити повідомлення: %s", payment["id"], e)
