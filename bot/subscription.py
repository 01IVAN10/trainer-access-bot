"""
Логіка підписки — «мозок» бота. ЧИСТІ функції: без бази, без Telegram, без годинника.

Чому так:
1. Час — це ПАРАМЕТР (now), а не datetime.now() всередині. Тому в тестах ми
   «перемотуємо» місяць за мілісекунду, а не чекаємо 30 днів.
2. Функції лише ВИРІШУЮТЬ, що робити (нагадати / попередити / видалити).
   Робить це інший код (планувальник, крок 6). Вирішувати легко тестувати;
   виконувати — ні (потрібен Telegram).

Увесь час — в UTC (timezone-aware). У Київ переводимо лише для показу людині.
Приклад, чому це важливо: 25.10.2026 Україна переходить на зимовий час. Підписка
«30 днів» у UTC — рівно 30×24 години, а в київському часі «закінчення» зсунеться
на годину. Якщо рахувати в місцевому часі, легко отримати 29 днів 23 години.

Життєвий цикл (приклад для тарифу 1 місяць, grace 1 день):
  оплата ── активна ──[за 3 дні]──[за 1 день]── закінчилась ── grace (1 день) ── видалення
                      нагадування  нагадування  «доступ ще є»                    з каналу
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum

from bot.plans import Plan


def utcnow() -> datetime:
    """ЄДИНЕ місце, де бот питає «котра година». Логіка нижче отримує now параметром."""
    return datetime.now(timezone.utc)


class Status(Enum):
    NONE = "none"        # ніколи не платив
    ACTIVE = "active"    # оплачено, термін не минув
    GRACE = "grace"      # термін минув, але пільговий період ще триває — доступ є
    EXPIRED = "expired"  # усе минуло — має бути видалений з каналу


@dataclass
class Subscription:
    """Стан підписки одного користувача (рядок таблиці subscriptions)."""
    user_id: int
    plan_id: str                 # тариф ОСТАННЬОЇ оплати: з нього беремо нагадування і grace
    expires_at: datetime         # UTC
    reminders_sent: int = 0      # скільки нагадувань уже надіслано в цьому періоді
    expiry_notified: bool = False  # чи писали «підписка закінчилась»
    removed: bool = False        # чи вже видалили з каналу


def new_expiry(current: datetime | None, now: datetime, duration: timedelta) -> datetime:
    """Нова дата закінчення після оплати.

    - не було підписки або вона вже минула → рахуємо від ЗАРАЗ;
    - підписка ще діє → додаємо до поточного кінця: хто продовжує раніше, не втрачає днів.

    Пільговий період НЕ «повертаємо»: хто заплатив на 1 день пізніше, отримує
    30 днів від моменту оплати, а не від старої дати. Grace — це ввічливість, а не борг.
    """
    if current is None or current <= now:
        return now + duration
    return current + duration


def get_status(expires_at: datetime | None, grace: timedelta, now: datetime) -> Status:
    if expires_at is None:
        return Status.NONE
    if now < expires_at:
        return Status.ACTIVE
    if now < expires_at + grace:
        return Status.GRACE
    return Status.EXPIRED


def next_reminder(expires_at: datetime, reminders: tuple[timedelta, ...],
                  sent: int, now: datetime) -> int | None:
    """Чи пора надіслати нагадування. Повертає НОВЕ значення лічильника або None.

    reminders = (3 дні, 1 день). Нагадування №k «настає» в момент expires_at - reminders[k].
    Рахуємо, скільки нагадувань уже мало настати, і порівнюємо з тим, скільки надіслано.

    Якщо бот був вимкнений і пропустив кілька — надсилаємо ОДНЕ (найсвіжіше),
    а не три повідомлення поспіль: людині потрібна актуальна інформація, а не спам.
    """
    if now >= expires_at:  # після закінчення — вже не «нагадування», а «закінчилась»
        return None
    due = sum(1 for r in reminders if now >= expires_at - r)
    return due if due > sent else None


class ActionKind(Enum):
    NOTHING = "nothing"
    REMIND = "remind"                 # «до кінця підписки N днів — продовжіть»
    NOTIFY_EXPIRED = "notify_expired"  # «підписка закінчилась, доступ ще N днів»
    REMOVE = "remove"                 # видалити з каналу


@dataclass(frozen=True)
class Action:
    kind: ActionKind
    reminders_sent: int = 0  # для REMIND: нове значення лічильника, яке треба зберегти


def decide(sub: Subscription, plan: Plan, now: datetime) -> Action:
    """Що зробити з цією підпискою ЗАРАЗ. Викликатиме планувальник (крок 6) для кожної підписки.

    Функція ідемпотентна в парі зі збереженням стану: якщо планувальник виконав дію і
    записав прапорець, наступний виклик поверне NOTHING — без дублів після перезапуску.
    """
    if sub.removed:
        return Action(ActionKind.NOTHING)
    status = get_status(sub.expires_at, plan.grace, now)
    if status == Status.EXPIRED:
        return Action(ActionKind.REMOVE)
    if status == Status.GRACE:
        if sub.expiry_notified:
            return Action(ActionKind.NOTHING)
        return Action(ActionKind.NOTIFY_EXPIRED)
    count = next_reminder(sub.expires_at, plan.reminders, sub.reminders_sent, now)
    if count is not None:
        return Action(ActionKind.REMIND, reminders_sent=count)
    return Action(ActionKind.NOTHING)


def purchase_blocker(plan: Plan, sub: Subscription | None, sub_plan: Plan | None,
                     now: datetime) -> str | None:
    """Чи можна купити цей тариф ЗАРАЗ. None — можна; інакше — пояснення для людини.

    Правило (узгоджено 21.10): демо-тариф — лише для тих, у кого зараз немає ПЛАТНОГО доступу.
    Інакше підписка «переїхала» б на демо: тариф став би demo, і пільговий період
    скоротився б з 1 дня до 1 хвилини — людина, яка заплатила за місяць, постраждала б.
    Повторювати демо можна скільки завгодно (гроші тестові).
    Платний тариф можна купити завжди: діючі дні додаються, а не згоряють.
    """
    if not plan.demo or sub is None or sub.removed or sub_plan is None or sub_plan.demo:
        return None
    if get_status(sub.expires_at, sub_plan.grace, now) in (Status.ACTIVE, Status.GRACE):
        return "У вас уже є оплачена підписка 🙂 Демо-тариф — для тих, хто ще не підписаний."
    return None


def has_access(sub: Subscription | None, plan: Plan | None, now: datetime) -> bool:
    """Чи пускати людину в канал ЗАРАЗ. Активна підписка або пільговий період — так.

    Тариф могли прибрати з plans.yaml — тоді пільгового періоду немає (але й не падаємо).
    """
    if sub is None or sub.removed:
        return False
    grace = plan.grace if plan else timedelta(0)
    return get_status(sub.expires_at, grace, now) in (Status.ACTIVE, Status.GRACE)
