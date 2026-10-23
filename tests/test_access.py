"""
Крок 5: доступ до каналу. Заявка на вступ → бот вирішує. Оплата → бот впускає.

Головне правило, яке тут перевіряємо: посилання можна переслати кому завгодно,
але в канал потрапить лише той, у кого САМ (його user_id) є підписка.
"""
import asyncio
from datetime import timedelta

from aiogram.methods import ApproveChatJoinRequest, CreateChatInviteLink, DeclineChatJoinRequest
from aiogram.types import Chat, User

from bot import db
from bot.access import Granted, ensure_invite_link, grant_after_payment
from bot.keyboards import PayCb, PlanCb
from bot.plans import load_plans
from bot.subscription import utcnow
from tests.fake_payments import FakeProvider
from tests.fake_telegram import (
    ACCESS, INVITE_LINK, USER, buttons, callback_update, get_dispatcher, join_request_update, make_bot, make_config,
)

PLANS = load_plans()
DP = get_dispatcher()
FRIEND = User(id=222, is_bot=False, first_name="Друг")


def world(script, subscription_days: float | None = None, removed: bool = False):
    """Свіжа база; за потреби — підписка Олени (+N днів від зараз, мінус — уже минула)."""
    bank = FakeProvider()

    async def go():
        await db.init(":memory:")
        bot, session = make_bot()
        try:
            if subscription_days is not None:
                await db.upsert_user(USER.id, USER.username, USER.first_name)
                await db._db.execute(
                    "INSERT INTO subscriptions (user_id, plan_id, expires_at, removed, updated_at) VALUES (?,?,?,?,?)",
                    (USER.id, "month", (utcnow() + timedelta(days=subscription_days)).isoformat(),
                     int(removed), utcnow().isoformat()))
                await db._db.commit()

            async def feed(update):
                await DP.feed_update(bot, update, config=make_config(), plans=PLANS, provider=bank)
            return session, await script(feed, bot, session, bank)
        finally:
            await db.close()
    return asyncio.run(go())


# ---------- посилання ----------

def test_invite_link_created_once_and_reused():
    """Перезапуск бота не має плодити нові посилання в каналі."""
    async def script(feed, bot, session, bank):
        first = await ensure_invite_link(bot, -1001)
        second = await ensure_invite_link(bot, -1001)
        return first, second

    session, (first, second) = world(script)
    created = session.of(CreateChatInviteLink)
    assert len(created) == 1 and created[0].creates_join_request is True   # саме з ЗАЯВКАМИ
    assert first.invite_link == second.invite_link == INVITE_LINK


# ---------- заявки на вступ ----------

def test_join_with_active_subscription_is_approved():
    async def script(feed, bot, session, bank):
        await feed(join_request_update())

    session, _ = world(script, subscription_days=10)
    assert len(session.of(ApproveChatJoinRequest)) == 1
    assert "Заявку схвалено" in session.sent()[0].text


def test_join_during_grace_is_approved():
    async def script(feed, bot, session, bank):
        await feed(join_request_update())

    session, _ = world(script, subscription_days=-0.5)   # минула 12 год тому, grace 1 день
    assert len(session.of(ApproveChatJoinRequest)) == 1


def test_forwarded_link_does_not_let_friend_in():
    """Олена заплатила й переслала посилання другу. Друг подає заявку — бот НЕ схвалює."""
    async def script(feed, bot, session, bank):
        await feed(join_request_update(user=FRIEND))
        async with db._db.execute("SELECT user_id FROM join_requests") as cur:
            return [r[0] for r in await cur.fetchall()]

    session, pending = world(script, subscription_days=10)   # підписка є в ОЛЕНИ, а не в друга
    assert session.of(ApproveChatJoinRequest) == [] and session.of(DeclineChatJoinRequest) == []
    assert pending == [FRIEND.id]                             # заявка друга чекає ЙОГО оплати
    assert session.sent()[0].chat_id == FRIEND.id and "потрібна активна підписка" in session.sent()[0].text
    assert buttons(session.sent()[0]) == ["💳 Обрати тариф"]
    # ПОСИЛАННЯ в бот (людина натисне «Start»), а не callback — див. наступні тести.
    button = session.sent()[0].reply_markup.inline_keyboard[0][0]
    assert button.url == "https://t.me/test_bot?start=plans" and button.callback_data is None


def test_join_with_expired_or_removed_subscription_waits():
    for days, removed in ((-5, False), (10, True)):
        async def script(feed, bot, session, bank):
            await feed(join_request_update())

        session, _ = world(script, subscription_days=days, removed=removed)
        assert session.of(ApproveChatJoinRequest) == []


def test_join_request_to_other_chat_is_ignored():
    """Бот може бути адміном і деінде — чужі заявки не чіпаємо і нікому не пишемо."""
    async def script(feed, bot, session, bank):
        await feed(join_request_update(chat=Chat(id=-100999, type="channel", title="Інший")))

    session, _ = world(script, subscription_days=10)
    assert session.of(ApproveChatJoinRequest) == [] and session.sent() == []


def test_join_request_saves_unknown_user():
    """Людина ніколи не писала боту, а одразу постукала в канал — має з'явитися в users
    (інакше платіж для неї не створиться: payments → REFERENCES users)."""
    async def script(feed, bot, session, bank):
        await feed(join_request_update(user=FRIEND))
        async with db._db.execute("SELECT full_name FROM users WHERE user_id = ?", (FRIEND.id,)) as cur:
            return (await cur.fetchone())[0]

    _, name = world(script)
    assert name == "Друг"


# ---------- після оплати ----------

def _buy_and_pay(feed, bank):
    async def go():
        await feed(callback_update(PlanCb(action="buy", plan_id="month").pack()))
        bank.pay("inv1")
        await feed(callback_update(PayCb(action="check", payment_id=1).pack()))
    return go()


def test_pending_request_approved_automatically_after_payment():
    """Головний сценарій кроку: постукала → «оплатіть» → оплатила → бот САМ впустив."""
    async def script(feed, bot, session, bank):
        await feed(join_request_update())          # заявка без підписки — чекає
        assert session.of(ApproveChatJoinRequest) == []
        await _buy_and_pay(feed, bank)
        async with db._db.execute("SELECT COUNT(*) FROM join_requests") as cur:
            return (await cur.fetchone())[0]

    session, pending_left = world(script)
    assert len(session.of(ApproveChatJoinRequest)) == 1 and pending_left == 0
    msg = session.edited()[-1]
    assert "схвалено" in msg.text and buttons(msg) == ["📢 Відкрити канал"]


def test_paid_without_request_gets_join_button():
    async def script(feed, bot, session, bank):
        await _buy_and_pay(feed, bank)

    session, _ = world(script)
    msg = session.edited()[-1]
    assert "Вступити в канал" in msg.text and buttons(msg) == ["🔑 Вступити в канал"]
    assert msg.reply_markup.inline_keyboard[0][0].url == INVITE_LINK
    assert session.of(ApproveChatJoinRequest) == []


def test_renewal_when_already_member():
    async def script(feed, bot, session, bank):
        session.in_channel.add(USER.id)
        await _buy_and_pay(feed, bank)

    session, _ = world(script, subscription_days=2)
    assert "Ви вже в каналі" in session.edited()[-1].text


def test_pending_request_cancelled_by_user_falls_back_to_link():
    async def script(feed, bot, session, bank):
        await feed(join_request_update())
        session.approve_fails = True               # людина встигла скасувати заявку
        await _buy_and_pay(feed, bank)

    session, _ = world(script)
    assert buttons(session.edited()[-1]) == ["🔑 Вступити в канал"]


def test_two_paths_at_once_approve_only_once():
    """Webhook і звірка зарахували одночасно — схвалювати заявку піде лише один."""
    async def script(feed, bot, session, bank):
        await db.upsert_user(USER.id, USER.username, USER.first_name)
        await db.save_join_request(USER.id, utcnow())
        return await asyncio.gather(*(grant_after_payment(bot, ACCESS, USER.id) for _ in range(3)))

    session, results = world(script)
    assert len(session.of(ApproveChatJoinRequest)) == 1
    assert results.count(Granted.APPROVED) == 1


def test_first_payment_already_approved_by_other_path_says_truth():
    """Живий тест 23.10: звірка встигла першою — зарахувала й схвалила заявку. Потім людина
    тисне «Перевірити». Це ПЕРША підписка — текст не має казати «продовжено»."""
    async def script(feed, bot, session, bank):
        await feed(join_request_update())
        await feed(callback_update(PlanCb(action="buy", plan_id="month").pack()))
        bank.pay("inv1")
        from bot.reconcile import reconcile_once
        await reconcile_once(bot, bank, PLANS, make_config(), ACCESS)      # звірка — першою
        await feed(callback_update(PayCb(action="check", payment_id=1).pack()))

    session, _ = world(script)
    assert len(session.of(ApproveChatJoinRequest)) == 1                   # схвалено рівно раз
    assert "схвалено" in session.sent()[-1].text                          # повідомлення від звірки
    final = session.edited()[-1].text
    assert "Ви вже в каналі" in final and "продовжено" not in final


# ---------- живий тест 23.10: людина не натискала /start ----------

def test_start_plans_deep_link_shows_plans_right_away():
    """Людина прийшла за посиланням із повідомлення про заявку → «Start» → одразу тарифи."""
    from tests.fake_telegram import text_update

    async def script(feed, bot, session, bank):
        await feed(text_update("/start plans"))

    session, _ = world(script)
    assert len(session.sent()) == 2
    assert "Сила вдома" in session.sent()[0].text                       # привітання + меню
    assert len(buttons(session.sent()[1])) == 3                          # і одразу тарифи


def test_user_who_never_started_bot_still_gets_access():
    """Як з Мамою 23.10: людина тиснула лише кнопки під повідомленням про заявку і ніколи
    не натискала «Start». Звірка зарахувала оплату й схвалила заявку, а написати Telegram
    не дозволив. Головне — доступ є і бот не впав; відсутність повідомлення — лише в лозі."""
    from bot.reconcile import reconcile_once

    async def script(feed, bot, session, bank):
        await feed(join_request_update())
        await feed(callback_update(PlanCb(action="buy", plan_id="demo").pack()))
        bank.pay("inv1")
        session.cannot_message.add(USER.id)                                # заявку схвалять — «вікно» закриється
        applied = await reconcile_once(bot, bank, PLANS, make_config(), ACCESS)
        return applied, await db.get_subscription(USER.id)

    session, (applied, sub) = world(script)
    assert applied == 1 and sub is not None
    assert len(session.of(ApproveChatJoinRequest)) == 1 and USER.id in session.in_channel
