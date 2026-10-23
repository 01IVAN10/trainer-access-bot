"""
Планувальник (крок 6). Час — параметр, тож «прожити» 6 хвилин демо чи місяць — мілісекунди.
Telegram — фальшивий: перевіряємо, кому що бот написав і кого прибрав з каналу.
"""
import asyncio
from datetime import timedelta

from aiogram.methods import BanChatMember, UnbanChatMember

from bot import db
from bot.plans import load_plans
from bot.scheduler import run_once
from bot.subscription import utcnow
from tests.fake_telegram import ADMIN_CHAT_ID, ACCESS, USER, buttons, make_bot, make_config

PLANS = load_plans()
M = timedelta(minutes=1)
D = timedelta(days=1)


class World:
    """База + фальшивий Telegram + годинник, який ми крутимо самі."""

    def __init__(self, plan_id="demo"):
        self.plan_id = plan_id

    def run(self, script):
        async def go():
            await db.init(":memory:")
            self.bot, self.tg = make_bot()
            self.t0 = utcnow().replace(microsecond=0)
            try:
                await db.upsert_user(USER.id, USER.username, USER.first_name)
                await self.pay(self.t0)                     # людина оплатила в момент t0
                self.tg.in_channel.add(USER.id)             # і зайшла в канал
                return await script(self)
            finally:
                await db.close()
        return asyncio.run(go())

    async def pay(self, at, plan_id=None):
        pid = await db.create_payment(USER.id, PLANS[plan_id or self.plan_id], at)
        await db.set_payment_status(pid, "success")
        return await db.apply_payment(pid, PLANS, at)

    async def tick(self, after: timedelta):
        """Один прохід планувальника в момент t0 + after."""
        return await run_once(self.bot, PLANS, make_config(), ACCESS, self.t0 + after)



def test_demo_full_cycle_minute_by_minute():
    """Те, що побачить тестувальник демо: 2 нагадування → «закінчилась» → видалення. Кожне — раз."""
    w = World()

    async def script(w):
        log = []
        for seconds in range(0, 9 * 60, 30):               # 9 хвилин кроками по 30 с, як справжній
            done = await w.tick(timedelta(seconds=seconds))
            log += [(seconds / 60, k) for k, v in done.items() for _ in range(v)]
        return log, await db.get_subscription(USER.id)

    log, sub = w.run(script)
    assert log == [(3.0, "remind"), (4.0, "remind"), (5.0, "expired"), (6.0, "removed")]
    assert sub.removed
    sent = [m.text for m in w.tg.sent()]
    assert "через 2 хв" in sent[0] and "через 1 хв" in sent[1]     # нагадування за 2 і за 1 хв
    assert sent[2].startswith("⚠️") and "ще 1 хв" in sent[2]        # «закінчилась, доступ ще 1 хв»
    assert sent[3].startswith("⛔️") and len(sent) == 4
    # власниця дізнається лише про видалення (не про кожне нагадування) — і рівно один раз
    assert len(w.tg.admin()) == 1 and "Підписку не продовжено" in w.tg.admin()[0].text
    # видалили так, щоб людина МОГЛА повернутися: ban + unban, а не просто ban
    assert len(w.tg.of(BanChatMember)) == 1 and len(w.tg.of(UnbanChatMember)) == 1
    assert USER.id not in w.tg.in_channel and USER.id not in w.tg.banned


def test_late_tick_still_says_round_minutes():
    """Живий тест 23.10: планувальник «прокидається» НЕ рівно в момент нагадування, а до 30 с
    пізніше. Було: за 1 хв 35 с до кінця → «через 1 хв» (відкидали секунди), а за 35 с →
    «через менше хвилини». Обіцяли «за 2 і за 1 хв» — маємо й писати 2 і 1."""
    w = World()

    async def script(w):
        for seconds in range(25, 9 * 60, 30):               # кожен прохід — на 25 с пізніше «ідеального»
            await w.tick(timedelta(seconds=seconds))

    w.run(script)
    sent = [m.text for m in w.tg.sent()]
    assert "через 2 хв" in sent[0] and "через 1 хв" in sent[1]
    assert "ще 1 хв" in sent[2]


def test_reminder_has_one_tap_renewal():
    w = World(plan_id="month")

    async def script(w):
        await w.tick(30 * D - 3 * D)

    w.run(script)
    assert buttons(w.tg.sent()[0]) == ["🔄 Продовжити «1 місяць» — 490 грн", "💳 Усі тарифи"]


def test_renewal_after_first_reminder_cancels_everything_old():
    """Нагадали за 3 дні → людина продовжила → «закінчилась» і видалення за СТАРОЮ датою не стаються."""
    w = World(plan_id="month")

    async def script(w):
        await w.tick(27 * D)                              # перше нагадування
        await w.pay(w.t0 + 27 * D + M, "month")           # продовжила
        for day in (29, 30, 31, 32):                      # дні, коли спрацювало б старе
            await w.tick(day * D)
        return await db.get_subscription(USER.id)

    sub = w.run(script)
    assert not sub.removed and len(w.tg.sent()) == 1 and w.tg.of(BanChatMember) == []
    assert sub.expires_at == w.t0 + 60 * D                # 3 дні, що лишались, не згоріли


def test_bot_was_down_through_whole_grace():
    """Бот лежав 3 дні після кінця місячної підписки → прокинувся → одразу видаляє (без «закінчилась»)."""
    w = World(plan_id="month")

    async def script(w):
        await w.tick(33 * D)

    w.run(script)
    assert len(w.tg.sent()) == 1 and w.tg.sent()[0].text.startswith("⛔️")
    assert len(w.tg.of(BanChatMember)) == 1


def test_restart_does_not_duplicate():
    """Два проходи в ту саму мить (напр. після перезапуску) — повідомлення одне."""
    w = World()

    async def script(w):
        await w.tick(3 * M)
        await w.tick(3 * M)

    w.run(script)
    assert len(w.tg.sent()) == 1


def test_owner_is_never_kicked():
    """Власник каналу теж купив демо (як Ваня) — минуло, але з власного каналу його не видаляємо."""
    w = World()

    async def script(w):
        w.tg.owners.add(USER.id)
        await w.tick(10 * M)
        await w.tick(11 * M)                               # і не пробуємо щоразу
        return await db.get_subscription(USER.id)

    sub = w.run(script)
    assert w.tg.of(BanChatMember) == [] and sub.removed
    # і не пишемо власниці «⛔️ доступ закрито» про її ж канал
    assert not any(m.text.startswith("⛔️") for m in w.tg.sent())
    assert w.tg.admin() == []                              # і в адмінку «не продовжила» — теж ні


def test_user_already_left_channel():
    """Людина сама вийшла з каналу — банити нікого, але повідомити про кінець підписки варто."""
    w = World()

    async def script(w):
        w.tg.in_channel.discard(USER.id)
        await w.tick(10 * M)

    w.run(script)
    assert w.tg.of(BanChatMember) == [] and w.tg.sent()[-1].text.startswith("⛔️")


def test_telegram_failure_on_kick_is_retried():
    """Telegram не відповів у мить видалення → позначку знято → наступний прохід видаляє."""
    w = World()

    async def script(w):
        w.tg.ban_fails = True
        await w.tick(10 * M)
        still_there = USER.id in w.tg.in_channel and not (await db.get_subscription(USER.id)).removed
        w.tg.ban_fails = False
        await w.tick(10 * M + 30 * timedelta(seconds=1))
        return still_there, await db.get_subscription(USER.id)

    still_there, sub = w.run(script)
    assert still_there and sub.removed and USER.id not in w.tg.in_channel


def test_paid_exactly_during_removal():
    """Найгірша гонка: оплата прийшла рівно між «вирішив видалити» і баном.
    Людину вже вигнали — бот одразу дає посилання знову, а не «доступ закрито»."""
    w = World()

    async def script(w):
        async def paid_now():
            await w.pay(w.t0 + 10 * M)
        w.tg.on_ban = paid_now
        await w.tick(10 * M)
        return await db.get_subscription(USER.id)

    sub = w.run(script)
    assert not sub.removed                                  # оплата скинула позначку видалення
    last = w.tg.sent()[-1]
    assert "продовжили підписку саме в ту мить" in last.text and buttons(last) == ["🔑 Вступити в канал"]


def test_blocked_bot_does_not_break_scheduler():
    w = World()

    async def script(w):
        w.tg.cannot_message.add(USER.id)
        for minute in (3, 4, 5, 6, 7):
            await w.tick(minute * M)
        return await db.get_subscription(USER.id)

    sub = w.run(script)
    assert sub.removed and USER.id not in w.tg.in_channel  # видалення важливіше за повідомлення
    # Спроб написати — рівно 4 (2 нагадування, «закінчилась», «закрито»): кожна НЕвдала
    # спроба теж позначена в базі, тож бот не «долбить» заблоковану людину щопроходу.
    assert len(w.tg.sent()) == 4


def test_comes_back_after_removal():
    """Видалили → через день оплатив знову → підписка жива, позначки скинуто, заявку схвалять."""
    from bot.subscription import has_access
    w = World()

    async def script(w):
        await w.tick(10 * M)
        sub = await w.pay(w.t0 + D)
        return sub

    sub = w.run(script)
    assert not sub.removed and sub.reminders_sent == 0 and not sub.expiry_notified
    assert has_access(sub, PLANS["demo"], w.t0 + D + M)


def test_renewal_between_decision_and_removal():
    """Гонка: планувальник прочитав підписку (минула) → у цю мить прийшла оплата → «заявка»
    на видалення має НЕ пройти, бо дата закінчення вже інша. Людину не чіпаємо."""
    w = World()

    async def script(w):
        original = db.subscriptions_due

        async def due_then_paid(now, horizon):
            subs = await original(now, horizon)      # планувальник побачив стару дату…
            await w.pay(now)                          # …і саме тоді людина оплатила
            return subs
        db.subscriptions_due = due_then_paid
        try:
            await w.tick(10 * M)
        finally:
            db.subscriptions_due = original
        return await db.get_subscription(USER.id)

    sub = w.run(script)
    assert not sub.removed and w.tg.of(BanChatMember) == [] and USER.id in w.tg.in_channel


def test_one_broken_subscription_does_not_stop_others():
    """У першої людини щось несподівано вибухнуло — другу все одно треба видалити."""
    from aiogram.types import User
    other = User(id=222, is_bot=False, first_name="Інша")
    w = World()

    async def script(w):
        await db.upsert_user(other.id, None, other.first_name)
        pid = await db.create_payment(other.id, PLANS["demo"], w.t0)
        await db.set_payment_status(pid, "success")
        await db.apply_payment(pid, PLANS, w.t0)
        w.tg.in_channel.add(other.id)
        w.tg.explode_for.add(USER.id)
        await w.tick(10 * M)

    w.run(script)
    assert other.id not in w.tg.in_channel


def test_claims_refuse_stale_expiry_date():
    """Рівень бази: усі три «заявки» не проходять, якщо дата закінчення вже не та."""
    w = World()

    async def script(w):
        old = (await db.get_subscription(USER.id)).expires_at
        await w.pay(w.t0 + M)                          # продовжив → нова дата
        return (await db.claim_reminder(USER.id, old, 1), await db.claim_expiry_notice(USER.id, old),
                await db.claim_removal(USER.id, old))

    assert w.run(script) == (False, False, False)


def test_admin_chat_down_does_not_break_removal():
    """Бота прибрали з адмін-групи. Видалення й повідомлення людині мають відпрацювати як завжди:
    сповіщення власниці — «на додачу», а не умова (мутація «tell_admins не ловить помилку» жила
    без цього тесту: для оплати страхує billing, а тут страховки немає)."""
    w = World()

    async def script(w):
        w.tg.cannot_message.add(ADMIN_CHAT_ID)
        return await w.tick(10 * M)

    done = w.run(script)
    assert done["removed"] == 1 and w.tg.sent()[-1].text.startswith("⛔️")
