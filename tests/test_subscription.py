"""Тести логіки підписки. Telegram і база не потрібні — лише дати.

Кожен тест — це бізнес-правило, сформульоване так, щоб його можна було
прочитати клієнту: «хто продовжує раніше — не втрачає днів».
"""
from datetime import datetime, timedelta, timezone

import pytest

from bot.plans import load_plans
from bot.subscription import (
    ActionKind, Status, Subscription, decide, get_status, new_expiry, next_reminder,
)

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
DAY = timedelta(days=1)
PLANS = load_plans()
MONTH, DEMO = PLANS["month"], PLANS["demo"]


# ---------- продовження ----------

def test_first_payment_counts_from_now():
    assert new_expiry(None, NOW, 30 * DAY) == NOW + 30 * DAY


def test_early_renewal_keeps_remaining_days():
    """Залишилось 5 днів, оплатив ще місяць → 35 днів, а не 30."""
    current = NOW + 5 * DAY
    assert new_expiry(current, NOW, 30 * DAY) == NOW + 35 * DAY


def test_late_renewal_counts_from_now():
    """Підписка минула 2 дні тому (людина в grace або вже видалена) → 30 днів від оплати."""
    current = NOW - 2 * DAY
    assert new_expiry(current, NOW, 30 * DAY) == NOW + 30 * DAY


def test_renewal_exactly_at_expiry():
    assert new_expiry(NOW, NOW, 30 * DAY) == NOW + 30 * DAY


# ---------- статус ----------

@pytest.mark.parametrize("now, expected", [
    (NOW - DAY, Status.ACTIVE),
    (NOW - timedelta(seconds=1), Status.ACTIVE),
    (NOW, Status.GRACE),                       # рівно в момент закінчення — вже grace
    (NOW + DAY - timedelta(seconds=1), Status.GRACE),
    (NOW + DAY, Status.EXPIRED),               # рівно в кінці grace — видаляємо
])
def test_status_boundaries(now, expected):
    """Межі — найчастіше місце помилок «на одиницю». Тому перевіряємо саме їх."""
    assert get_status(NOW, DAY, now) == expected


def test_status_none():
    assert get_status(None, DAY, NOW) == Status.NONE


def test_zero_grace_means_remove_at_expiry():
    assert get_status(NOW, timedelta(0), NOW) == Status.EXPIRED


# ---------- нагадування ----------

REM = (3 * DAY, DAY)
EXP = NOW + 10 * DAY


def test_no_reminder_too_early():
    assert next_reminder(EXP, REM, 0, EXP - 4 * DAY) is None


def test_first_reminder_3_days_before():
    assert next_reminder(EXP, REM, 0, EXP - 3 * DAY) == 1


def test_first_reminder_not_repeated():
    assert next_reminder(EXP, REM, 1, EXP - 2 * DAY) is None


def test_second_reminder_1_day_before():
    assert next_reminder(EXP, REM, 1, EXP - DAY) == 2


def test_bot_was_offline_sends_only_one():
    """Бот лежав 3 дні й пропустив обидва нагадування → одне повідомлення, не два."""
    assert next_reminder(EXP, REM, 0, EXP - timedelta(hours=12)) == 2


def test_no_reminders_after_expiry():
    assert next_reminder(EXP, REM, 0, EXP) is None


# ---------- що робити ----------

def _sub(**kw) -> Subscription:
    return Subscription(**({"user_id": 1, "plan_id": "month", "expires_at": EXP} | kw))


def test_decide_grace_notifies_once():
    assert decide(_sub(), MONTH, EXP).kind == ActionKind.NOTIFY_EXPIRED
    assert decide(_sub(expiry_notified=True), MONTH, EXP).kind == ActionKind.NOTHING


def test_decide_remove_after_grace():
    assert decide(_sub(expiry_notified=True), MONTH, EXP + DAY).kind == ActionKind.REMOVE


def test_decide_already_removed_does_nothing():
    """Інакше бот намагався б видаляти людину кожні 30 секунд вічно."""
    assert decide(_sub(removed=True), MONTH, EXP + 10 * DAY).kind == ActionKind.NOTHING


def test_offline_through_whole_grace_removes():
    """Бот лежав увесь grace → не пише «закінчилась», а одразу видаляє (повідомлення — при видаленні)."""
    assert decide(_sub(), MONTH, EXP + 2 * DAY).kind == ActionKind.REMOVE


# ---------- повний цикл ----------

def simulate(sub: Subscription, plan, start: datetime, end: datetime, step: timedelta):
    """Мініпланувальник: крутить годинник і виконує рішення decide(), як це робитиме бот.
    Повертає журнал подій: [(хвилин від старту, подія), ...]."""
    log, now = [], start
    while now <= end:
        action = decide(sub, plan, now)
        minute = round((now - start).total_seconds() / 60, 1)
        if action.kind == ActionKind.REMIND:
            sub.reminders_sent = action.reminders_sent
            log.append((minute, f"нагадування {action.reminders_sent}"))
        elif action.kind == ActionKind.NOTIFY_EXPIRED:
            sub.expiry_notified = True
            log.append((minute, "закінчилась"))
        elif action.kind == ActionKind.REMOVE:
            sub.removed = True
            log.append((minute, "видалено"))
        now += step
    return log


def test_demo_plan_full_cycle():
    """Демо-тариф від оплати до видалення — те, що побачить тестувальник за ~6 хвилин.

    Крок 30 с — як у планувальника. Кожна подія рівно один раз і в правильному порядку.
    """
    sub = Subscription(user_id=1, plan_id="demo", expires_at=new_expiry(None, NOW, DEMO.duration))
    log = simulate(sub, DEMO, NOW, NOW + timedelta(minutes=10), timedelta(seconds=30))
    assert log == [
        (3.0, "нагадування 1"),  # 5 хв − 2 хв
        (4.0, "нагадування 2"),  # 5 хв − 1 хв
        (5.0, "закінчилась"),
        (6.0, "видалено"),       # + grace 1 хв
    ]


def test_month_plan_early_renewal_resets_reminders():
    """Отримав перше нагадування → продовжив → нагадування для НОВОГО терміну знову прийдуть."""
    sub = Subscription(user_id=1, plan_id="month", expires_at=NOW + 30 * DAY)
    log = simulate(sub, MONTH, NOW, NOW + 27 * DAY, timedelta(hours=1))
    assert log == [(27 * 24 * 60.0, "нагадування 1")]

    # Продовжив на 28-й день (так робитиме db.apply_payment: новий термін + скинуті лічильники)
    renewed_at = NOW + 28 * DAY
    sub = Subscription(user_id=1, plan_id="month",
                       expires_at=new_expiry(sub.expires_at, renewed_at, MONTH.duration))
    assert sub.expires_at == NOW + 60 * DAY  # 2 дні, що лишались, не згоріли
    log = simulate(sub, MONTH, renewed_at, NOW + 62 * DAY, timedelta(hours=1))
    assert [event for _, event in log] == ["нагадування 1", "нагадування 2", "закінчилась", "видалено"]
