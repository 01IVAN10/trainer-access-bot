"""
/stats — цифри для власниці каналу. ЧИСТА функція (як decide() на кроці 1): дані й час —
параметрами, жодної бази чи Telegram. Тому межі («сьогодні» починається опівночі ЗА КИЄВОМ,
а не за UTC) перевіряються тестом за мілісекунду.

Що рахуємо і чому саме це — питання бізнесу, а не коду:
  • скільки людей зараз платять (активні) і скільки «на межі» (пільговий період);
  • скільки підписок закінчиться найближчими днями — кому варто написати особисто;
  • виручка за сьогодні / 7 / 30 днів — гроші, які реально дійшли (applied_at), а не рахунки;
  • конверсія: скільки запускали бота → скільки хоч раз заплатили.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from bot.plans import Plan
from bot.subscription import Status, Subscription, get_status

ENDING_SOON = timedelta(days=3)


@dataclass(frozen=True)
class Money:
    count: int = 0
    kop: int = 0

    def add(self, kop: int) -> "Money":
        return Money(self.count + 1, self.kop + kop)


@dataclass(frozen=True)
class Stats:
    users: int            # запускали бота
    paying_users: int     # хоч раз заплатили
    active: int           # оплачено, термін не минув
    grace: int            # термін минув, але доступ ще є
    ending_soon: int      # активні, що закінчуються за ENDING_SOON
    removed: int          # видалені з каналу (не продовжили або /revoke)
    today: Money
    week: Money
    month: Money
    demo_payments: int    # тестові оплати демо-тарифу за 30 днів (у виручку не входять)


def local_midnight(now: datetime, tz: str) -> datetime:
    """Початок «сьогодні» за місцевим часом, у UTC. 00:30 за Києвом — це ще ВЧОРА за UTC:
    без цього оплата о пів на першу ночі потрапила б у «вчора»."""
    local = now.astimezone(ZoneInfo(tz))
    return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)


def compute(subs: list[Subscription], paid: list[dict], plans: dict[str, Plan], now: datetime, tz: str,
            users: int, paying_users: int, removed: int) -> Stats:
    active = grace = ending_soon = 0
    for sub in subs:
        plan = plans.get(sub.plan_id)
        status = get_status(sub.expires_at, plan.grace if plan else timedelta(0), now)
        if status == Status.ACTIVE:
            active += 1
            if sub.expires_at - now <= ENDING_SOON:
                ending_soon += 1
        elif status == Status.GRACE:
            grace += 1
        # EXPIRED, але ще не removed — планувальник видалить за пів хвилини; не рахуємо ніде.

    midnight = local_midnight(now, tz)
    today, week, month, demo = Money(), Money(), Money(), 0
    for p in paid:
        at = datetime.fromisoformat(p["applied_at"])
        if at < now - timedelta(days=30):
            continue
        plan = plans.get(p["plan_id"])
        if plan is not None and plan.demo:
            demo += 1  # тестові гроші не змішуємо зі справжніми
            continue
        month = month.add(p["amount_kop"])
        if at >= now - timedelta(days=7):
            week = week.add(p["amount_kop"])
        if at >= midnight:
            today = today.add(p["amount_kop"])

    return Stats(users=users, paying_users=paying_users, active=active, grace=grace,
                 ending_soon=ending_soon, removed=removed, today=today, week=week, month=month,
                 demo_payments=demo)
