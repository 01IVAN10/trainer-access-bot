"""
Сценарії «як у людини»: справжні хендлери, роутери, фільтри, middleware і база,
але Telegram — фальшивий (tests/fake_telegram.py). Перевіряємо, ЩО бот відповів.
"""
import asyncio
from datetime import timedelta

from bot import db
from bot.keyboards import BTN_HELP, BTN_MY, BTN_PLANS, PlanCb
from bot.plans import load_plans
from bot.subscription import Subscription, utcnow
from tests.fake_payments import FakeProvider
from tests.fake_telegram import (
    INVITE_LINK,    get_dispatcher,
    ADMIN_GROUP, USER, buttons, callback_update, make_bot, make_config, text_update,
)

PLANS = load_plans()
DP = get_dispatcher()  # спільний для всіх тестів (див. tests/fake_telegram.py)


def scenario(steps, demo_mode=True, subscription: Subscription | None = None,
             provider: FakeProvider | None = None):
    """Свіжа БД → (за потреби) готова підписка → події по черзі → що надіслав бот."""
    async def run():
        await db.init(":memory:")
        bot, session = make_bot()
        try:
            if subscription:
                await db.upsert_user(USER.id, USER.username, USER.first_name)
                await db._db.execute(
                    "INSERT INTO subscriptions (user_id, plan_id, expires_at, removed, updated_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (USER.id, subscription.plan_id, subscription.expires_at.isoformat(),
                     int(subscription.removed), utcnow().isoformat()))
                await db._db.commit()
            for update in steps:
                await DP.feed_update(bot, update, config=make_config(demo_mode), plans=PLANS,
                                     provider=provider or FakeProvider())
            return session
        finally:
            await db.close()
    return asyncio.run(run())


def test_start_shows_menu_and_demo_note():
    s = scenario([text_update("/start")])
    msg = s.sent()[0]
    assert "Сила вдома" in msg.text and "Олена" in msg.text and "ТЕСТОВА" in msg.text
    assert [b.text for row in msg.reply_markup.keyboard for b in row] == [BTN_PLANS, BTN_MY, BTN_HELP]


def test_start_without_demo_mode_has_no_demo_note():
    s = scenario([text_update("/start")], demo_mode=False)
    assert "ТЕСТОВА" not in s.sent()[0].text


def test_user_is_saved_to_db():
    """Без цього на кроці 3 не вийде створити платіж (payments → REFERENCES users)."""
    async def run():
        await db.init(":memory:")
        bot, _ = make_bot()
        try:
            await DP.feed_update(bot, text_update("/start"), config=make_config(), plans=PLANS,
                                 provider=FakeProvider())
            async with db._db.execute("SELECT full_name FROM users WHERE user_id = ?", (USER.id,)) as cur:
                return (await cur.fetchone())["full_name"]
        finally:
            await db.close()
    assert asyncio.run(run()) == "Олена"


def test_plans_list_demo_mode():
    s = scenario([text_update(BTN_PLANS)])
    assert buttons(s.sent()[0]) == ["Демо — 5 хвилин — 10 грн", "1 місяць — 490 грн", "3 місяці — 1290 грн"]


def test_plans_list_without_demo():
    s = scenario([text_update(BTN_PLANS)], demo_mode=False)
    assert buttons(s.sent()[0]) == ["1 місяць — 490 грн", "3 місяці — 1290 грн"]


def test_view_plan_edits_message_into_card():
    s = scenario([callback_update(PlanCb(action="view", plan_id="month").pack())])
    card = s.edited()[0]
    assert "1 місяць" in card.text and "490 грн" in card.text
    assert buttons(card) == ["💳 Оплатити 490 грн", "⬅️ Усі тарифи"]
    assert len(s.answers()) == 1  # рівно одна відповідь на натискання


def test_back_to_list():
    s = scenario([callback_update(PlanCb(action="list").pack())])
    assert len(buttons(s.edited()[0])) == 3


def test_old_demo_button_after_demo_mode_disabled():
    """Кнопка з учорашнього повідомлення: демо вже вимкнули — бот пояснює і показує актуальне."""
    s = scenario([callback_update(PlanCb(action="view", plan_id="demo").pack())], demo_mode=False)
    assert s.answers()[0].show_alert and "недоступний" in s.answers()[0].text
    assert buttons(s.edited()[0]) == ["1 місяць — 490 грн", "3 місяці — 1290 грн"]
    assert len(s.answers()) == 1


def test_forged_button_with_unknown_plan():
    """Хтось підробив callback_data — бот не падає, а показує список."""
    s = scenario([callback_update(PlanCb(action="buy", plan_id="free_forever").pack())])
    assert "недоступний" in s.answers()[0].text


def test_demo_blocked_for_paid_subscriber():
    month = Subscription(user_id=USER.id, plan_id="month", expires_at=utcnow() + timedelta(days=10))
    s = scenario([callback_update(PlanCb(action="buy", plan_id="demo").pack())], subscription=month)
    assert s.answers()[0].show_alert and "оплачена підписка" in s.answers()[0].text
    assert len(s.answers()[0].text) <= 200  # ліміт Telegram на текст спливаючого вікна


def test_my_subscription_none():
    s = scenario([text_update(BTN_MY)])
    assert "немає підписки" in s.sent()[0].text
    assert buttons(s.sent()[0]) == ["💳 Обрати тариф"]


def test_my_subscription_active_offers_renewal():
    month = Subscription(user_id=USER.id, plan_id="month", expires_at=utcnow() + timedelta(days=10))
    s = scenario([text_update(BTN_MY)], subscription=month)
    assert "активна" in s.sent()[0].text and "за київським часом" in s.sent()[0].text
    # Людина загубила повідомлення після оплати — посилання на канал завжди тут.
    assert buttons(s.sent()[0]) == ["🔑 Посилання на канал", "🔄 Продовжити"]
    assert s.sent()[0].reply_markup.inline_keyboard[0][0].url == INVITE_LINK


def test_my_subscription_expired_no_channel_link():
    """Підписка минула (і пільговий період теж) — посилання на канал НЕ даємо."""
    old = Subscription(user_id=USER.id, plan_id="month", expires_at=utcnow() - timedelta(days=5))
    s = scenario([text_update(BTN_MY)], subscription=old)
    assert buttons(s.sent()[0]) == ["💳 Обрати тариф"]


def test_help_and_paysupport_show_contact():
    s = scenario([text_update(BTN_HELP), text_update("/paysupport")])
    assert "@support" in s.sent()[0].text and "/paysupport" in s.sent()[0].text
    assert "@support" in s.sent()[1].text and "за який тариф" in s.sent()[1].text


def test_unknown_text_gets_hint():
    s = scenario([text_update("привіт, скільки коштує?")])
    assert "меню" in s.sent()[0].text


def test_bot_silent_in_admin_group():
    """Повідомлення в адмін-групі — не від клієнтів. Бот не має відповідати меню."""
    s = scenario([text_update("/start", chat=ADMIN_GROUP), text_update("привіт", chat=ADMIN_GROUP)])
    assert s.sent() == []


def test_button_press_alone_saves_user():
    """Людина натиснула кнопку, а /start у ЦІЙ базі не робила (база нова, бот перенесли).
    Користувач має з'явитися в users — інакше на кроці 3 платіж не створиться (REFERENCES users)."""
    async def run():
        await db.init(":memory:")
        bot, _ = make_bot()
        try:
            await DP.feed_update(bot, callback_update(PlanCb(action="view", plan_id="month").pack()),
                                 config=make_config(), plans=PLANS, provider=FakeProvider())
            async with db._db.execute("SELECT COUNT(*) FROM users WHERE user_id = ?", (USER.id,)) as cur:
                return (await cur.fetchone())[0]
        finally:
            await db.close()
    assert asyncio.run(run()) == 1


# ---------- /demo (крок 10) ----------

def _demo(**config_extra):
    async def run():
        await db.init(":memory:")
        bot, session = make_bot()
        try:
            await DP.feed_update(bot, text_update("/demo"), config=make_config(**config_extra), plans=PLANS,
                                 provider=FakeProvider())
            return session.sent()
        finally:
            await db.close()
    return asyncio.run(run())


def test_demo_explains_what_to_press_and_what_to_expect():
    msg = _demo()[0]
    assert "4242 4242 4242 4242" in msg.text and "Вступити в канал" in msg.text
    assert "6-й" in msg.text                                   # що бот прибере з каналу — і коли
    assert "🎥" not in msg.text and "свого каналу" not in msg.text  # без відео й контакту — без цих рядків


def test_demo_shows_video_and_contact_when_configured():
    msg = _demo(demo_video_url="https://youtu.be/x", developer_contact="@dev")[0]
    assert "https://youtu.be/x" in msg.text and "@dev" in msg.text


def test_demo_hidden_outside_demo_mode():
    """У боті реального клієнта /demo не існує — звичайна відповідь на незнайоме."""
    msg = _demo(demo_mode=False)[0]
    assert "4242" not in msg.text and "меню" in msg.text


def test_start_points_to_demo():
    s = scenario([text_update("/start")])
    assert "/demo" in s.sent()[0].text
