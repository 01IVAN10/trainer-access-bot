"""
Крок 7: адмінка. Що бачить власниця і що вона може зробити.

Три групи перевірок:
  1) сповіщення — про КОЖНУ оплату рівно один раз, хоч би яким шляхом вона прийшла;
     збій адмін-групи не ламає оплату;
  2) безпека — команди працюють ЛИШЕ в адмін-групі («стати зловмисником»);
  3) команди — /stats, /user, /grant, /revoke роблять те, що обіцяють, і все записано в журнал.
"""
import asyncio
from datetime import datetime, timedelta, timezone

from aiogram.methods import ApproveChatJoinRequest, BanChatMember, UnbanChatMember
from aiogram.types import User

from bot import admin_texts, db, stats
from bot.handlers.admin import grant_plan_id
from bot.keyboards import PayCb, PlanCb
from bot.plans import load_plans
from bot.reconcile import reconcile_once
from bot.subscription import Subscription, has_access, utcnow
from tests.fake_payments import FakeProvider
from tests.fake_telegram import (
    ACCESS, ADMIN_CHAT, ADMIN_CHAT_ID, ADMIN_GROUP, ADMIN_USER, INVITE_LINK, PRIVATE, USER, buttons,
    callback_update, get_dispatcher, join_request_update, make_bot, make_config, text_update,
)

PLANS = load_plans()
DP = get_dispatcher()
D = timedelta(days=1)


class World:
    """Свіжа база + фальшивий Telegram + фальшивий банк. Олена вже запускала бота."""

    def __init__(self, sub_days: float | None = None, plan_id: str = "month", removed: bool = False):
        self.sub_days, self.plan_id, self.removed = sub_days, plan_id, removed
        self.bank = FakeProvider()

    def run(self, script):
        async def go():
            await db.init(":memory:")
            self.bot, self.tg = make_bot()
            try:
                await db.upsert_user(USER.id, USER.username, USER.first_name)
                if self.sub_days is not None:
                    await db._db.execute(
                        "INSERT INTO subscriptions (user_id, plan_id, expires_at, removed, updated_at) "
                        "VALUES (?,?,?,?,?)",
                        (USER.id, self.plan_id, db._iso(utcnow() + self.sub_days * D), int(self.removed),
                         db._iso(utcnow())))
                    await db._db.commit()
                return await script(self)
            finally:
                await db.close()
        return asyncio.run(go())

    async def feed(self, update):
        await DP.feed_update(self.bot, update, config=make_config(), plans=PLANS, provider=self.bank)

    async def admin(self, command: str):
        """Власниця пише команду в адмін-групі."""
        await self.feed(text_update(command, chat=ADMIN_CHAT, user=ADMIN_USER))

    async def buy_and_check(self, plan_id="month", user=USER, invoice="inv1", payment_id=1):
        await self.feed(callback_update(PlanCb(action="buy", plan_id=plan_id).pack(), user=user))
        self.bank.pay(invoice)
        await self.feed(callback_update(PayCb(action="check", payment_id=payment_id).pack(), user=user))

    def last_admin(self) -> str:
        return self.tg.admin()[-1].text


async def _actions():
    async with db._db.execute("SELECT admin_id, action, user_id, details FROM admin_actions ORDER BY id") as cur:
        return [dict(r) for r in await cur.fetchall()]


# ---------- 1. сповіщення про оплати ----------

def test_payment_notifies_admin_exactly_once():
    w = World()

    async def script(w):
        await w.buy_and_check()
        await w.feed(callback_update(PayCb(action="check", payment_id=1).pack()))  # натиснула ще раз
        await reconcile_once(w.bot, w.bank, PLANS, make_config(), ACCESS)          # і звірка пройшла

    w.run(script)
    notices = w.tg.admin()
    assert len(notices) == 1
    text = notices[0].text
    assert "💰" in text and "490 грн" in text and "Олена" in text and "@olena" in text
    assert "🆕 нова підписка" in text and "Платіж #1" in text and "1 місяць" in text
    assert f"tg://user?id={USER.id}" in text                       # клікабельне ім'я — написати людині


def test_reconcile_path_also_notifies_admin():
    """Оплата дійшла через фонову звірку (людина нічого не натискала) — власниця все одно знає."""
    w = World()

    async def script(w):
        await w.feed(callback_update(PlanCb(action="buy", plan_id="month").pack()))
        w.bank.pay("inv1")
        await reconcile_once(w.bot, w.bank, PLANS, make_config(), ACCESS)

    w.run(script)
    assert len(w.tg.admin()) == 1 and "💰" in w.last_admin()


def test_renewal_is_labeled():
    w = World(sub_days=10)

    async def script(w):
        await w.buy_and_check()

    w.run(script)
    assert "🔁 продовження" in w.last_admin()


def test_payment_kind_rules():
    now = utcnow()
    month = PLANS["month"]
    sub = lambda days, removed=False: Subscription(USER.id, "month", now + days * D, removed=removed)
    assert admin_texts.payment_kind(None, None, now) == "🆕 нова підписка"
    assert admin_texts.payment_kind(sub(5), month, now) == "🔁 продовження"
    assert admin_texts.payment_kind(sub(-0.5), month, now) == "🔁 продовження"          # пільговий період
    assert admin_texts.payment_kind(sub(-5), month, now) == "↩️ повернення після перерви"
    assert admin_texts.payment_kind(sub(5, removed=True), month, now) == "↩️ повернення після перерви"


def test_admin_chat_down_does_not_break_payment():
    """Бота прибрали з адмін-групи — оплата все одно зараховується, людина отримує доступ."""
    w = World()

    async def script(w):
        w.tg.cannot_message.add(ADMIN_CHAT_ID)
        await w.buy_and_check()
        return await db.get_subscription(USER.id)

    sub = w.run(script)
    assert sub is not None and "Оплату отримано" in w.tg.edited()[-1].text


def test_name_with_html_is_escaped():
    """Ім'я — це текст від людини. «<b>…</b>» в імені не має ламати повідомлення власниці."""
    w = World()
    hacker = User(id=333, is_bot=False, first_name="<b>Хакер</b>", username="h")

    async def script(w):
        await db.upsert_user(hacker.id, hacker.username, hacker.full_name)
        await w.buy_and_check(user=hacker)

    w.run(script)
    assert "&lt;b&gt;Хакер&lt;/b&gt;" in w.last_admin() and "<b>Хакер" not in w.last_admin()


# ---------- 2. безпека: команди лише в адмін-групі ----------

def test_commands_ignored_outside_admin_chat():
    """Стати зловмисником: Олена пише /grant собі в особисті; хтось додав бота у свою групу."""
    w = World()

    async def script(w):
        await w.feed(text_update("/grant @olena 365"))                                    # особисті
        await w.feed(text_update("/grant @olena 365", chat=ADMIN_GROUP, user=ADMIN_USER))  # чужа група
        await w.feed(text_update("/stats", chat=ADMIN_GROUP, user=ADMIN_USER))
        return await db.get_subscription(USER.id), await _actions()

    sub, actions = w.run(script)
    assert sub is None and actions == []
    assert w.tg.admin() == []
    replies = w.tg.sent()
    assert len(replies) == 1 and replies[0].chat_id == PRIVATE.id   # в особистих — звичайне «я працюю через меню»
    assert "меню" in replies[0].text


def test_admin_chat_small_talk_is_ignored():
    """В адмін-групі люди спілкуються — бот відповідає лише на свої команди."""
    w = World()

    async def script(w):
        await w.admin("привіт, як справи?")
        await w.admin("/start")

    w.run(script)
    assert w.tg.sent() == [] and w.tg.admin() == []


def test_admin_help():
    w = World()

    async def script(w):
        await w.admin("/admin")
        await w.admin("/help@test_bot")   # у групі Telegram часто додає @бот до команди

    w.run(script)
    assert len(w.tg.admin()) == 2 and all("/grant" in m.text for m in w.tg.admin())


# ---------- 3. /stats ----------

def test_stats_compute_today_is_kyiv_midnight():
    """24.10, 00:30 за Києвом (= 23.10, 21:30 UTC). «Сьогодні» почалося 30 хв тому, а не 21,5 год."""
    now = datetime(2026, 10, 23, 21, 30, tzinfo=timezone.utc)
    paid = [
        {"plan_id": "month", "amount_kop": 49000, "applied_at": "2026-10-23T21:10:00+00:00"},  # 00:10 Київ — сьогодні
        {"plan_id": "month", "amount_kop": 49000, "applied_at": "2026-10-23T20:50:00+00:00"},  # 23:50 Київ — вчора
        {"plan_id": "quarter", "amount_kop": 129000, "applied_at": "2026-10-10T10:00:00+00:00"},  # 13 днів тому
        {"plan_id": "demo", "amount_kop": 1000, "applied_at": "2026-10-23T21:20:00+00:00"},    # тест — не гроші
        {"plan_id": "month", "amount_kop": 49000, "applied_at": "2026-09-20T10:00:00+00:00"},  # >30 днів
    ]
    subs = [
        Subscription(1, "month", now + 2 * D),                   # активна, скоро закінчиться
        Subscription(2, "month", now + 20 * D),                  # активна
        Subscription(3, "month", now - timedelta(hours=5)),      # пільговий період (grace 1 день)
        Subscription(4, "month", now - 2 * D),                   # минула, ще не видалена — ніде не рахуємо
    ]
    s = stats.compute(subs, paid, PLANS, now, "Europe/Kyiv", users=10, paying_users=4, removed=2)
    assert (s.active, s.grace, s.ending_soon, s.removed) == (2, 1, 1, 2)
    assert s.today == stats.Money(1, 49000)
    assert s.week == stats.Money(2, 98000)
    assert s.month == stats.Money(3, 227000)
    assert s.demo_payments == 1


def test_stats_command():
    w = World()

    async def script(w):
        await w.buy_and_check()
        await w.admin("/stats")

    w.run(script)
    text = w.last_admin()
    assert "📊" in text and "Активних підписок: <b>1</b>" in text
    assert "Сьогодні: <b>490 грн</b> (1 оплата)" in text and "платили хоч раз: 1" in text


def test_money_and_plural_formatting():
    assert admin_texts.uah(147000) == "1 470 грн"
    assert [admin_texts.plural(n, "оплата", "оплати", "оплат") for n in (1, 2, 5, 11, 21, 22, 112)] == \
        ["оплата", "оплати", "оплат", "оплат", "оплата", "оплати", "оплат"]


# ---------- 4. /grant ----------

def test_grant_to_user_without_subscription():
    w = World()

    async def script(w):
        await w.admin("/grant @olena 30")
        return await db.get_subscription(USER.id), await _actions()

    sub, actions = w.run(script)
    assert sub.plan_id == "month" and abs(sub.expires_at - (utcnow() + 30 * D)) < timedelta(minutes=1)
    assert actions == [{"admin_id": ADMIN_USER.id, "action": "grant", "user_id": USER.id,
                        "details": actions[0]["details"]}] and "+30 дн." in actions[0]["details"]
    to_user = w.tg.sent()[-1]
    assert to_user.chat_id == USER.id and "🎁" in to_user.text and "Оплату" not in to_user.text
    assert buttons(to_user) == ["🔑 Вступити в канал"]
    assert "Видано +30 дн." in w.last_admin()


def test_grant_adds_to_active_subscription():
    """Як і з оплатою: діючі дні не згорають."""
    w = World(sub_days=10)

    async def script(w):
        await w.admin("/grant 111 30")      # за ID
        return await db.get_subscription(USER.id)

    sub = w.run(script)
    assert abs(sub.expires_at - (utcnow() + 40 * D)) < timedelta(minutes=1)


def test_grant_after_demo_switches_to_paid_plan():
    """30 днів із демо-налаштуваннями = видалення через хвилину після кінця без попередження."""
    w = World(sub_days=0.001, plan_id="demo")

    async def script(w):
        await w.admin("/grant @OLENA 30")   # регістр username не важливий
        return await db.get_subscription(USER.id)

    assert w.run(script).plan_id == "month"
    assert grant_plan_id("quarter", PLANS) == "quarter"
    assert grant_plan_id("demo", PLANS) == "month" and grant_plan_id(None, PLANS) == "month"
    assert grant_plan_id("deleted_plan", PLANS) == "month"


def test_grant_approves_pending_join_request():
    w = World()

    async def script(w):
        await w.feed(join_request_update())   # постукала без оплати — заявка чекає
        await w.admin("/grant @olena 30")

    w.run(script)
    assert len(w.tg.of(ApproveChatJoinRequest)) == 1
    assert "схвалено" in w.tg.sent()[-1].text


def test_grant_bad_input_changes_nothing():
    w = World()

    async def script(w):
        for cmd in ("/grant", "/grant @olena", "/grant @olena 0", "/grant @olena 1000",
                    "/grant @olena abc", "/grant @olena 30 extra", "/grant @nobody 30"):
            await w.admin(cmd)
        return await db.get_subscription(USER.id), await _actions()

    sub, actions = w.run(script)
    assert sub is None and actions == []
    answers = [m.text for m in w.tg.admin()]
    assert len(answers) == 7 and all("Як: /grant" in a for a in answers[:6])
    assert "Не знайшов" in answers[6]


def test_grant_when_user_blocked_bot():
    """Доступ видано, але написати не можна — власниця отримує посилання, щоб передати самій."""
    w = World()

    async def script(w):
        w.tg.cannot_message.add(USER.id)
        await w.admin("/grant @olena 7")
        return await db.get_subscription(USER.id)

    assert w.run(script) is not None
    assert "Написати людині не вдалося" in w.last_admin() and INVITE_LINK in w.last_admin()


# ---------- 5. /revoke ----------

def test_revoke_member_with_active_subscription():
    w = World(sub_days=20)

    async def script(w):
        w.tg.in_channel.add(USER.id)
        await w.admin("/revoke @olena")
        sub = await db.get_subscription(USER.id)
        await w.feed(join_request_update())     # спробувала зайти знову — не має пустити
        return sub, await _actions()

    sub, actions = w.run(script)
    assert sub.removed and not has_access(sub, PLANS["month"], utcnow())
    assert len(w.tg.of(BanChatMember)) == 1 and len(w.tg.of(UnbanChatMember)) == 1
    assert USER.id not in w.tg.in_channel and USER.id not in w.tg.banned   # зможе повернутись після оплати
    assert w.tg.of(ApproveChatJoinRequest) == []
    user_texts = [m.text for m in w.tg.sent()]
    assert any("закрито адміністратором" in t and "@support" in t for t in user_texts)
    assert "Прибрано з каналу" in w.tg.admin()[0].text
    assert [a["action"] for a in actions] == ["revoke"]


def test_revoke_when_telegram_fails_says_so_and_retry_works():
    """Тут НЕ відкочуємо позначку, як у планувальнику: людина бачить результат і повторить."""
    w = World(sub_days=20)

    async def script(w):
        w.tg.in_channel.add(USER.id)
        w.tg.ban_fails = True
        await w.admin("/revoke @olena")
        first = (await db.get_subscription(USER.id)).removed
        w.tg.ban_fails = False
        await w.admin("/revoke @olena")
        return first

    removed_after_failure = w.run(script)
    assert removed_after_failure                                       # у базі закрито одразу
    first, second = (m.text for m in w.tg.admin())
    assert "НЕ вдалося" in first and "Повторіть /revoke" in first
    assert "Прибрано з каналу" in second and USER.id not in w.tg.in_channel


def test_revoke_channel_admin_is_not_kicked():
    w = World(sub_days=20)

    async def script(w):
        w.tg.owners.add(USER.id)
        await w.admin("/revoke @olena")

    w.run(script)
    assert w.tg.of(BanChatMember) == [] and "адміністратор каналу" in w.last_admin()


def test_revoked_user_can_pay_and_come_back():
    w = World(sub_days=20)

    async def script(w):
        w.tg.in_channel.add(USER.id)
        await w.admin("/revoke @olena")
        await w.buy_and_check()
        return await db.get_subscription(USER.id)

    sub = w.run(script)
    assert has_access(sub, PLANS["month"], utcnow())
    assert "↩️ повернення" in w.last_admin()


# ---------- 6. /user ----------

def test_user_card_shows_everything():
    w = World()

    async def script(w):
        await w.feed(join_request_update())
        await w.feed(callback_update(PlanCb(action="buy", plan_id="month").pack()))   # рахунок, не оплачений
        await w.admin("/grant @olena 7")
        await w.feed(join_request_update())   # заявку схвалено одразу — висячої вже немає
        await w.admin("/user @olena")

    w.run(script)
    card = w.last_admin()
    assert "👤" in card and "@olena" in card and "✅ Активна" in card
    assert "#1" in card and "⏳ очікує оплати" in card
    assert "🎁 видано доступ" in card and str(ADMIN_USER.id) in card


def test_user_card_shows_waiting_request_and_unknown_user():
    w = World()

    async def script(w):
        await w.feed(join_request_update())
        await w.admin("/user olena")
        await w.admin("/user @ghost")
        await w.admin("/user")

    w.run(script)
    card, ghost, usage = (m.text for m in w.tg.admin())
    assert "Підписки немає" in card and "чекає оплати" in card and "— немає" in card
    assert "Не знайшов" in ghost and "Як: /user" in usage
