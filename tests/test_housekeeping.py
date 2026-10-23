"""
Крок 8: «прибирання» — дрібниці, які людина помічає, а розробник ні.

1) Старий рахунок. Оплату підтвердив webhook або звірка (людина не тиснула «Перевірити») —
   під рахунком досі «💳 Оплатити». Бот прибирає ці кнопки і пише «оплачено» / «згорів».
2) Висячі заявки. Постукала в канал, не заплатила й забула — через 48 год бот відхиляє заявку,
   щоб у каналі не збиралися сотні «мертвих» заявок.
"""
import asyncio
from datetime import timedelta

from aiogram.methods import ApproveChatJoinRequest, DeclineChatJoinRequest, EditMessageText

from bot import db, texts
from bot.keyboards import PayCb, PlanCb
from bot.plans import load_plans
from bot.reconcile import reconcile_once
from bot.scheduler import JOIN_REQUEST_TTL, run_once
from bot.subscription import utcnow
from tests.fake_payments import FakeProvider
from tests.fake_telegram import ACCESS, USER, buttons, callback_update, get_dispatcher, make_bot, make_config

PLANS = load_plans()
DP = get_dispatcher()
H = timedelta(hours=1)


class World:
    def __init__(self):
        self.bank = FakeProvider()

    def run(self, script):
        async def go():
            await db.init(":memory:")
            self.bot, self.tg = make_bot()
            try:
                await db.upsert_user(USER.id, USER.username, USER.first_name)
                return await script(self)
            finally:
                await db.close()
        return asyncio.run(go())

    async def press(self, data):
        await DP.feed_update(self.bot, callback_update(data), config=make_config(), plans=PLANS, provider=self.bank)

    async def buy(self):
        await self.press(PlanCb(action="buy", plan_id="month").pack())
        return (await db.get_payment(1))["message_id"]

    async def reconcile(self):
        return await reconcile_once(self.bot, self.bank, PLANS, make_config(), ACCESS)

    async def tick(self, at=None):
        return await run_once(self.bot, PLANS, make_config(), ACCESS, at or utcnow())

    def edits_of(self, message_id):
        return [e for e in self.tg.edited() if e.message_id == message_id]


# ---------- 1. старий рахунок ----------

def test_invoice_buttons_removed_after_reconcile_success():
    w = World()

    async def script(w):
        msg_id = await w.buy()
        w.bank.pay("inv1")
        await w.reconcile()
        return msg_id

    msg_id = w.run(script)
    assert msg_id is not None
    closing = w.edits_of(msg_id)[-1]
    assert closing.text == texts.INVOICE_PAID and closing.reply_markup is None   # кнопок більше немає
    assert "Оплату отримано" in w.tg.sent()[-1].text                             # подробиці — новим повідомленням


def test_expired_invoice_offers_retry():
    """Про expired mono не шле webhook — звірка дізнається і ЗМІНЮЄ рахунок на «згорів, спробуйте ще»."""
    w = World()

    async def script(w):
        msg_id = await w.buy()
        w.bank.pay("inv1", "expired")
        await w.reconcile()
        return msg_id

    closing = w.edits_of(w.run(script))[-1]
    assert "Час на оплату" in closing.text and buttons(closing) == ["🔄 Спробувати ще раз", "⬅️ Усі тарифи"]


def test_button_path_is_not_edited_twice():
    """Людина сама натиснула «Перевірити» (рахунок уже змінено) — звірка після цього нічого не чіпає."""
    w = World()

    async def script(w):
        msg_id = await w.buy()
        w.bank.pay("inv1")
        await w.press(PayCb(action="check", payment_id=1).pack())
        before = len(w.tg.edited())
        await w.reconcile()
        return msg_id, before

    msg_id, before = w.run(script)
    assert len(w.tg.edited()) == before                                  # звірка нічого не редагувала
    assert not any(e.text == texts.INVOICE_PAID for e in w.tg.edited())  # і «див. нижче» без «нижче» не з'явилось


def test_deleted_invoice_message_does_not_break_payment():
    """Людина видалила повідомлення з рахунком — редагувати нічого, але доступ має бути."""
    w = World()

    async def script(w):
        await w.buy()
        w.tg.edit_fails = True
        w.bank.pay("inv1")
        await w.reconcile()
        return await db.get_subscription(USER.id)

    assert w.run(script) is not None and "Оплату отримано" in w.tg.sent()[-1].text


def test_new_invoice_screen_remembers_latest_message():
    """Рахунок показали вдруге в ІНШОМУ повідомленні (кнопка під нагадуванням) — прибирати треба там."""
    w = World()

    async def script(w):
        first = await w.buy()
        second = await w.buy()          # новий callback = нове повідомлення, той самий рахунок
        return first, second

    first, second = w.run(script)
    assert first != second


# ---------- 2. висячі заявки ----------

async def _waiting_since(ago):
    await db.save_join_request(USER.id, utcnow() - ago)


def test_stale_request_declined_and_user_told():
    w = World()

    async def script(w):
        await _waiting_since(JOIN_REQUEST_TTL + H)
        done = await w.tick()
        return done, await db.has_join_request(USER.id)

    done, still_waiting = w.run(script)
    assert done["declined"] == 1 and not still_waiting
    assert len(w.tg.of(DeclineChatJoinRequest)) == 1
    msg = w.tg.sent()[-1]
    assert "скасовано" in msg.text and "2 дн." in msg.text and buttons(msg) == ["💳 Обрати тариф"]


def test_fresh_request_is_left_alone():
    w = World()

    async def script(w):
        await _waiting_since(JOIN_REQUEST_TTL - H)
        await w.tick()
        return await db.has_join_request(USER.id)

    assert w.run(script) and w.tg.of(DeclineChatJoinRequest) == [] and w.tg.sent() == []


def test_request_already_gone_is_cleaned_silently():
    """Заявку вже схвалили вручну — НЕ пишемо «вашу заявку скасовано» людині, яка в каналі."""
    w = World()

    async def script(w):
        await _waiting_since(JOIN_REQUEST_TTL + H)
        w.tg.decline_error = "missing"
        done = await w.tick()
        return done, await db.has_join_request(USER.id)

    done, still_waiting = w.run(script)
    assert done["declined"] == 0 and not still_waiting and w.tg.sent() == []


def test_telegram_down_keeps_request_for_next_pass():
    """Спершу Telegram, потім база: якби стерли першими — заявка висіла б у каналі, а бот про неї забув."""
    w = World()

    async def script(w):
        await _waiting_since(JOIN_REQUEST_TTL + H)
        w.tg.decline_error = "down"
        first = await w.tick()
        kept = await db.has_join_request(USER.id)
        w.tg.decline_error = None
        second = await w.tick()
        return first["declined"], kept, second["declined"]

    assert w.run(script) == (0, True, 1)


def test_pays_after_decline_gets_join_button():
    """Заявку відхилили, а через день людина таки оплатила — бот не пробує схвалити мертву заявку,
    а дає кнопку «Вступити в канал»: нова заявка — і її схвалять одразу."""
    w = World()

    async def script(w):
        await _waiting_since(JOIN_REQUEST_TTL + H)
        await w.tick()
        await w.buy()
        w.bank.pay("inv1")
        await w.press(PayCb(action="check", payment_id=1).pack())

    w.run(script)
    assert w.tg.of(ApproveChatJoinRequest) == []
    assert buttons(w.tg.edited()[-1]) == ["🔑 Вступити в канал"]


def test_race_reconcile_saw_old_status_does_not_overwrite(monkeypatch):
    """Гонка: звірка взяла список «ще не оплачених», а людина тієї ж секунди натиснула «Перевірити»
    і отримала «✅ Оплату отримано». Звірка НЕ має переписати це на «подробиці нижче» — нижче нічого
    не буде, бо зарахувала не вона."""
    w = World()

    async def script(w):
        await w.buy()
        snapshot = await db.pending_payments(utcnow())       # звірка «бачить» платіж ще неоплаченим
        w.bank.pay("inv1")
        await w.press(PayCb(action="check", payment_id=1).pack())   # а людина вже встигла
        async def stale(now):
            return snapshot
        monkeypatch.setattr(db, "pending_payments", stale)
        await w.reconcile()

    w.run(script)
    assert not any(e.text == texts.INVOICE_PAID for e in w.tg.edited())
