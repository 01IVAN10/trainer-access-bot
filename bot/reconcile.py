"""
Фонова звірка: раз на хвилину бот САМ питає mono про незавершені платежі.

Навіщо, якщо є webhook:
- mono робить лише до 3 спроб доставити webhook — бот лежав хвилину під час деплою, і
  повідомлення «оплачено» загубилося НАЗАВЖДИ. Людина заплатила, а доступу немає;
- про expired (рахунок згорів) mono webhook не надсилає взагалі;
- локально на Mac без тунелю webhook не дійде — звірка робить оплату автоматичною і тут.
Webhook — швидкий шлях (секунди), звірка — надійний (до хвилини). Разом — і швидко, і надійно.
"""
import asyncio
import logging

from aiogram import Bot

from bot import billing, db
from bot.config import Config
from bot.payments import PaymentError, PaymentProvider
from bot.plans import Plan
from bot.subscription import utcnow

log = logging.getLogger(__name__)

INTERVAL_SECONDS = 60


async def reconcile_once(bot: Bot, provider: PaymentProvider, plans: dict[str, Plan], config: Config) -> int:
    """Один прохід. Повертає, скільки платежів саме зараз зараховано."""
    applied = 0
    for payment in await db.pending_payments(utcnow()):
        try:
            status = await provider.get_status(payment["invoice_id"])
        except PaymentError as e:
            # Банк лежить — нічого страшного, спробуємо наступного разу. Решту не мучимо.
            log.warning("Звірка: mono недоступний (%s) — пропускаю до наступного проходу", e)
            break
        sub = await billing.record_status(payment["id"], status, plans, utcnow())
        if sub is not None:
            applied += 1
            log.info("Звірка: платіж #%s зараховано (webhook не дійшов або вимкнений)", payment["id"])
            await billing.notify_paid(bot, sub, plans, config.timezone)
    return applied


async def reconcile_forever(bot: Bot, provider: PaymentProvider, plans: dict[str, Plan], config: Config) -> None:
    while True:
        try:
            await reconcile_once(bot, provider, plans, config)
        except Exception:
            # Будь-яка несподіванка не має вбити цикл назавжди — інакше звірка тихо зупиниться.
            log.exception("Звірка: несподівана помилка")
        await asyncio.sleep(INTERVAL_SECONDS)
