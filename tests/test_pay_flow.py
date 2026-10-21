"""
Оплата очима людини: «Оплатити» → рахунок → «Перевірити» → підписка.
Банк фальшивий (tests/fake_payments.py) — тест вирішує, що він «відповість».
"""
import asyncio
from datetime import timedelta

from bot import db
from bot.keyboards import PayCb, PlanCb
from bot.plans import load_plans
from bot.subscription import utcnow
from tests.fake_payments import FakeProvider
from tests.fake_telegram import get_dispatcher, USER, buttons, callback_update, make_bot, make_config

PLANS = load_plans()
DP = get_dispatcher()  # спільний для всіх тестів (див. tests/fake_telegram.py)

BUY_MONTH = PlanCb(action="buy", plan_id="month").pack()
BUY_DEMO = PlanCb(action="buy", plan_id="demo").pack()


def check(payment_id: int = 1) -> str:
    return PayCb(action="check", payment_id=payment_id).pack()


class Run:
    """Одна «людина» і один банк. Кроки виконуються по черзі; між ними тест може
    «оплатити» рахунок у банку (bank.pay()) — як людина на сторінці mono."""

    def __init__(self) -> None:
        self.bank = FakeProvider()

    def __call__(self, script):
        async def go():
            await db.init(":memory:")
            bot, session = make_bot()
            try:
                result = await script(lambda data, user=USER: DP.feed_update(
                    bot, callback_update(data, user=user), config=make_config(), plans=PLANS,
                    provider=self.bank))
                return session, result
            finally:
                await db.close()
        return asyncio.run(go())


def test_buy_creates_invoice_in_kopecks_and_shows_pay_button():
    run = Run()

    async def script(press):
        await press(BUY_MONTH)
        return await db.get_payment(1)

    session, payment = run(script)
    assert run.bank.created[0]["amount_kop"] == 49000          # гривні → копійки
    assert run.bank.created[0]["reference"] == "payment-1"     # наш номер у банку
    assert run.bank.created[0]["redirect_url"] == "https://t.me/test_bot"
    assert payment["invoice_id"] == "inv1" and payment["status"] == "created"
    msg = session.edited()[-1]
    assert "490 грн" in msg.text and "4242" in msg.text        # демо-підказка з тестовою карткою
    assert msg.reply_markup.inline_keyboard[0][0].url == "https://pay.test/inv1"
    assert buttons(msg) == ["💳 Оплатити 490 грн", "✅ Я оплатив — перевірити", "⬅️ Усі тарифи"]


def test_second_buy_reuses_open_invoice():
    """Натиснув «Оплатити», повернувся до тарифів, натиснув знову — той самий рахунок."""
    run = Run()

    async def script(press):
        await press(BUY_MONTH)
        await press(BUY_MONTH)
        async with db._db.execute("SELECT COUNT(*) FROM payments") as cur:
            return (await cur.fetchone())[0]

    _, count = run(script)
    assert count == 1 and len(run.bank.created) == 1


def test_different_plan_gets_new_invoice():
    run = Run()

    async def script(press):
        await press(BUY_MONTH)
        await press(PlanCb(action="buy", plan_id="quarter").pack())

    run(script)
    assert [c["amount_kop"] for c in run.bank.created] == [49000, 129000]


def test_check_before_paying_says_pending():
    run = Run()

    async def script(press):
        await press(BUY_MONTH)
        await press(check())
        return await db.get_subscription(USER.id)

    session, sub = run(script)
    assert sub is None
    assert session.answers()[-1].show_alert and "ще не отримано" in session.answers()[-1].text


def test_paid_then_check_gives_subscription():
    run = Run()

    async def script(press):
        await press(BUY_MONTH)
        run.bank.pay("inv1")                     # людина оплатила на сторінці mono
        await press(check())
        return await db.get_subscription(USER.id), await db.get_payment(1)

    session, (sub, payment) = run(script)
    assert payment["status"] == "success" and payment["applied_at"] is not None
    assert timedelta(days=29, hours=23) < sub.expires_at - utcnow() <= timedelta(days=30)
    assert "Оплату отримано" in session.edited()[-1].text


def test_check_pressed_many_times_extends_once():
    run = Run()

    async def script(press):
        await press(BUY_MONTH)
        run.bank.pay("inv1")
        await asyncio.gather(*(press(check()) for _ in range(3)))  # тричі поспіль, нетерпляче
        await press(check())
        return await db.get_subscription(USER.id)

    _, sub = run(script)
    assert sub.expires_at - utcnow() <= timedelta(days=30)        # 30 днів, а не 120


def test_after_success_bank_is_not_asked_again():
    """Статус success — остаточний. Повторне «Перевірити» не смикає банк."""
    run = Run()

    async def script(press):
        await press(BUY_MONTH)
        run.bank.pay("inv1")
        await press(check())
        calls = run.bank.status_calls
        await press(check())
        return calls

    _, calls_after_first = run(script)
    assert run.bank.status_calls == calls_after_first


def test_card_declined():
    run = Run()

    async def script(press):
        await press(BUY_MONTH)
        run.bank.pay("inv1", "failure")
        await press(check())
        return await db.get_subscription(USER.id)

    session, sub = run(script)
    assert sub is None
    assert "не пройшла" in session.edited()[-1].text
    assert buttons(session.edited()[-1])[0] == "🔄 Спробувати ще раз"


def test_invoice_expired():
    run = Run()

    async def script(press):
        await press(BUY_MONTH)
        run.bank.pay("inv1", "expired")
        await press(check())
        await press(BUY_MONTH)                  # «Спробувати ще раз» → НОВИЙ рахунок
        return await db.get_payment(1), await db.get_payment(2)

    session, (old, new) = run(script)
    assert old["status"] == "expired" and new["invoice_id"] == "inv2"
    assert "минув" in session.edited()[-2].text


def test_bank_down_on_create():
    run = Run()
    run.bank.down = True

    async def script(press):
        await press(BUY_MONTH)
        return await db.get_payment(1)

    session, payment = run(script)
    assert payment["status"] == "failure" and payment["invoice_id"] is None   # слід у базі є
    assert "банком" in session.answers()[-1].text


def test_bank_down_on_check_then_recovers():
    run = Run()

    async def script(press):
        await press(BUY_MONTH)
        run.bank.pay("inv1")
        run.bank.down = True
        await press(check())                    # банк лежить → «спробуйте пізніше»
        no_sub = await db.get_subscription(USER.id)
        run.bank.down = False
        await press(check())                    # банк піднявся → оплата зараховується
        return no_sub, await db.get_subscription(USER.id)

    session, (before, after) = run(script)
    assert before is None and after is not None
    assert "банком" in session.answers()[1].text   # [0] — відповідь на «Оплатити»


def test_someone_elses_payment_id():
    """Підробили кнопку: вписали номер ЧУЖОГО платежу. Нічого не розкриваємо і не зараховуємо."""
    from aiogram.types import User
    stranger = User(id=777, is_bot=False, first_name="Чужий")
    run = Run()

    async def script(press):
        await press(BUY_MONTH)                  # платіж #1 — Олени
        run.bank.pay("inv1")
        await press(check(1), user=stranger)    # чужий тисне «перевірити» платіж #1
        return await db.get_subscription(stranger.id), await db.get_payment(1)

    session, (stranger_sub, payment) = run(script)
    assert stranger_sub is None and payment["applied_at"] is None
    assert "не знайдено" in session.answers()[-1].text


def test_demo_full_purchase():
    run = Run()

    async def script(press):
        await press(BUY_DEMO)
        run.bank.pay("inv1")
        await press(check())
        return await db.get_subscription(USER.id)

    _, sub = run(script)
    assert run.bank.created[0]["amount_kop"] == 1000
    assert sub.plan_id == "demo" and sub.expires_at - utcnow() <= timedelta(minutes=5)
