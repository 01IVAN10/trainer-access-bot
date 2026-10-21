"""Фонова звірка: webhook загубився — бот сам дізнається про оплату і повідомить людину."""
import asyncio
from datetime import timedelta

from bot import db
from bot.plans import load_plans
from bot.reconcile import reconcile_once
from bot.subscription import utcnow
from tests.fake_payments import FakeProvider
from tests.fake_telegram import USER, make_bot, make_config

PLANS = load_plans()


def run(script, created_ago: timedelta = timedelta(minutes=5)):
    bank = FakeProvider()

    async def go():
        await db.init(":memory:")
        bot, session = make_bot()
        try:
            await db.upsert_user(USER.id, USER.username, USER.first_name)
            created = utcnow() - created_ago
            pid = await db.create_payment(USER.id, PLANS["month"], created)
            invoice = await bank.create_invoice(amount_kop=49000)
            await db.set_invoice(pid, invoice.invoice_id, invoice.page_url, created + timedelta(minutes=30))
            result = await script(lambda: reconcile_once(bot, bank, PLANS, make_config()), bank)
            return session, result
        finally:
            await db.close()
    return asyncio.run(go())


def test_lost_webhook_is_caught_and_user_notified():
    async def script(reconcile, bank):
        bank.pay("inv1")
        first = await reconcile()
        second = await reconcile()               # наступна хвилина — вже нічого нового
        return first, second, await db.get_subscription(USER.id)

    session, (first, second, sub) = run(script)
    assert (first, second) == (1, 0) and sub is not None
    assert len(session.sent()) == 1 and "Оплату отримано" in session.sent()[0].text


def test_not_paid_yet_nothing_happens():
    async def script(reconcile, bank):
        return await reconcile(), await db.get_payment(1)

    session, (applied, payment) = run(script)
    assert applied == 0 and payment["status"] == "created" and session.sent() == []


def test_expired_invoice_recorded():
    """Про expired mono webhook НЕ надсилає — лише звірка дізнається, що рахунок згорів."""
    async def script(reconcile, bank):
        bank.pay("inv1", "expired")
        await reconcile()
        return await db.get_payment(1)

    _, payment = run(script)
    assert payment["status"] == "expired"


def test_bank_down_does_not_crash():
    async def script(reconcile, bank):
        bank.pay("inv1")
        bank.down = True
        first = await reconcile()
        bank.down = False
        return first, await reconcile()

    _, (first, second) = run(script)
    assert (first, second) == (0, 1)


def test_old_abandoned_payments_are_left_alone():
    """Рахунок дводенної давнини, так і не оплачений, — банк про нього не смикаємо."""
    async def script(reconcile, bank):
        await reconcile()
        return bank.status_calls

    _, calls = run(script, created_ago=timedelta(days=2))
    assert calls == 0
