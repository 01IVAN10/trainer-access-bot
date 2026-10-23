"""
Оплата: «Оплатити» → рахунок у mono → «Я оплатив — перевірити» → підписка.

Порядок у «Оплатити» важливий:
  1) СПЕРШУ запис у нашій базі (create_payment) — отримуємо НАШ номер платежу;
  2) ПОТІМ рахунок у mono з цим номером (reference);
  3) прив'язуємо invoiceId до запису.
Якщо робити навпаки і бот впаде між 2 і 3 — у mono буде рахунок, про який ми не знаємо.
А так у нас завжди є слід: «платіж #17 створено, рахунку немає — mono не відповів».

«Перевірити» нічого не вирішує зі слів людини: бот САМ питає mono про статус
і лише після success продовжує підписку (apply_payment — рівно один раз, крок 1).
"""
import logging
from datetime import datetime, timedelta

from aiogram import F, Router
from aiogram.types import CallbackQuery

from bot import billing, db, texts
from bot.access import ChannelAccess, grant_after_payment
from bot.config import Config
from bot.handlers.common import edit
from bot.handlers.plans import find_plan, unavailable
from bot.keyboards import PayCb, PlanCb, channel_keyboard, invoice_keyboard, retry_keyboard
from bot.payments import PaymentError, PaymentProvider
from bot.plans import Plan
from bot.subscription import purchase_blocker, utcnow

log = logging.getLogger(__name__)
router = Router(name="pay")

# Скільки живе рахунок. Досить, щоб спокійно ввести картку; не настільки довго,
# щоб людина повернулась через добу до старої ціни.
INVOICE_TTL = timedelta(minutes=30)


async def _show_invoice(callback: CallbackQuery, plan: Plan, payment: dict, config: Config) -> None:
    valid_until = datetime.fromisoformat(payment["valid_until"])
    await edit(callback,
               texts.invoice_text(plan, payment["amount_kop"], valid_until, config.timezone, config.demo_mode),
               invoice_keyboard(payment["page_url"], payment["amount_kop"] // 100, payment["id"]))
    # Крок 8: де людина бачить рахунок — щоб прибрати кнопки, коли оплату підтвердить webhook/звірка.
    await db.set_invoice_message(payment["id"], callback.message.message_id)


@router.callback_query(PlanCb.filter(F.action == "buy"))
async def cb_buy(callback: CallbackQuery, callback_data: PlanCb, config: Config,
                 plans: dict[str, Plan], provider: PaymentProvider) -> None:
    plan = find_plan(plans, callback_data.plan_id, config)
    if plan is None:
        return await unavailable(callback, config, plans)

    user_id = callback.from_user.id
    now = utcnow()
    sub = await db.get_subscription(user_id)
    blocker = purchase_blocker(plan, sub, plans.get(sub.plan_id) if sub else None, now)
    if blocker:
        await callback.answer(blocker, show_alert=True)
        return

    # Уже є неоплачений рахунок на цей тариф — показуємо його, а не створюємо новий.
    payment = await db.find_open_payment(user_id, plan.id, now)
    if payment is None:
        payment_id = await db.create_payment(user_id, plan, now)
        me = await callback.bot.me()
        try:
            invoice = await provider.create_invoice(
                amount_kop=plan.amount_kop,
                reference=f"payment-{payment_id}",
                description=f"{texts.BUSINESS_NAME} — {plan.title}",
                redirect_url=f"https://t.me/{me.username}",  # після оплати — назад у бот
                webhook_url=config.webhook_url,               # None → лише кнопка і фонова звірка
                validity_seconds=int(INVOICE_TTL.total_seconds()),
            )
        except PaymentError as e:
            log.error("Рахунок для платежу #%s не створено: %s", payment_id, e)
            await db.set_payment_status(payment_id, "failure")  # слід у базі: спроба була
            await callback.answer(texts.PAYMENT_PROVIDER_DOWN, show_alert=True)
            return
        await db.set_invoice(payment_id, invoice.invoice_id, invoice.page_url, now + INVOICE_TTL)
        payment = await db.get_payment(payment_id)
        log.info("INVOICE | user=%s | plan=%s | payment=#%s", user_id, plan.id, payment_id)

    await _show_invoice(callback, plan, payment, config)
    await callback.answer()


@router.callback_query(PayCb.filter(F.action == "check"))
async def cb_check(callback: CallbackQuery, callback_data: PayCb, config: Config,
                   plans: dict[str, Plan], provider: PaymentProvider, access: ChannelAccess) -> None:
    payment = await db.get_payment(callback_data.payment_id)
    # Чужий або вигаданий номер платежу — відповідаємо однаково, нічого не розкриваючи.
    if payment is None or payment["user_id"] != callback.from_user.id:
        await callback.answer("Цей рахунок не знайдено.", show_alert=True)
        return

    plan = plans.get(payment["plan_id"])
    status = payment["status"]

    # Питаємо банк, лише поки статус не остаточний. Якщо в базі вже success, але
    # підписку не продовжено (бот упав між цими кроками) — нижче просто доведемо до кінця.
    if status in ("created", "processing") and payment["invoice_id"]:
        try:
            status = await provider.get_status(payment["invoice_id"])
        except PaymentError as e:
            log.warning("Статус платежу #%s не отримано: %s", payment["id"], e)
            await callback.answer(texts.PAYMENT_PROVIDER_DOWN, show_alert=True)
            return
    # Та сама функція, що для webhook і звірки (bot/billing.py). Ідемпотентно: якщо вже
    # зараховано іншим шляхом — поверне None, тоді просто показуємо поточну підписку.
    newly = await billing.record_status(payment["id"], status, plans, utcnow(), callback.bot, config)

    if status == "success":
        sub = newly or await db.get_subscription(payment["user_id"])
        granted = await grant_after_payment(callback.bot, access, payment["user_id"])
        await edit(callback, texts.payment_success_text(plan, sub, config.timezone, granted.value),
                   channel_keyboard(access.invite_link, granted.value))
        await callback.answer("Оплату отримано ✅")
    elif status in ("created", "processing"):
        await callback.answer(texts.PAYMENT_PENDING, show_alert=True)
    elif status == "failure":
        await edit(callback, texts.PAYMENT_FAILED, retry_keyboard(payment["plan_id"]))
        await callback.answer()
    else:  # expired
        await edit(callback, texts.PAYMENT_EXPIRED, retry_keyboard(payment["plan_id"]))
        await callback.answer()
