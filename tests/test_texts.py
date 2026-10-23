"""Тексти для людей: що саме побачить підписник у різних станах."""
from datetime import datetime, timedelta, timezone

from bot.plans import load_plans
from bot.subscription import Subscription
from bot.texts import format_date, format_left, plan_card, subscription_text

PLANS = load_plans()
NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
KYIV = "Europe/Kyiv"


def test_date_shown_in_kyiv_time():
    # 12:00 UTC 20 жовтня = 15:00 у Києві (літній час, UTC+3)
    assert format_date(NOW, KYIV) == "20.10.2026 о 15:00"


def test_date_after_dst_switch():
    """25.10.2026 Україна переходить на зимовий час (UTC+2). Та сама 12:00 UTC — вже 14:00."""
    assert format_date(datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc), KYIV) == "01.11.2026 о 14:00"


def test_format_left():
    assert format_left(timedelta(days=29, hours=3, minutes=12)) == "29 дн. 3 год"
    assert format_left(timedelta(days=2)) == "2 дн."
    assert format_left(timedelta(hours=5, minutes=12)) == "5 год 12 хв"
    # округлення до найближчої хвилини (планувальник запізнюється до 30 с — див. test_scheduler)
    assert format_left(timedelta(minutes=3, seconds=40)) == "4 хв"
    assert format_left(timedelta(minutes=3, seconds=20)) == "3 хв"
    assert format_left(timedelta(seconds=40)) == "1 хв"
    assert format_left(timedelta(seconds=29)) == "менше хвилини"
    assert format_left(timedelta(days=2, hours=23, minutes=59, seconds=40)) == "3 дн."


def test_no_subscription():
    assert "немає підписки" in subscription_text(None, None, NOW, KYIV)


def _sub(**kw):
    return Subscription(**({"user_id": 1, "plan_id": "month", "expires_at": NOW + timedelta(days=10)} | kw))


def test_active():
    text = subscription_text(_sub(), PLANS["month"], NOW, KYIV)
    assert "активна" in text and "30.10.2026 о 14:00" in text and "10 дн." in text


def test_grace_shows_time_until_removal():
    sub = _sub(expires_at=NOW - timedelta(hours=6))  # минула 6 год тому, grace 1 день
    text = subscription_text(sub, PLANS["month"], NOW, KYIV)
    assert "закінчилась" in text and "ще 18 год" in text


def test_expired_and_removed():
    assert "закрито" in subscription_text(_sub(expires_at=NOW - timedelta(days=5)), PLANS["month"], NOW, KYIV)
    assert "закрито" in subscription_text(_sub(removed=True), PLANS["month"], NOW, KYIV)


def test_plan_removed_from_yaml_does_not_crash():
    """Тариф прибрали з plans.yaml, а підписка за нього ще є — бот не має падати."""
    assert "активна" in subscription_text(_sub(plan_id="old_plan"), None, NOW, KYIV)


def test_demo_card_says_test_payment():
    assert "Тестова оплата" in plan_card(PLANS["demo"])
    assert "Тестова оплата" not in plan_card(PLANS["month"])
    assert "490 грн" in plan_card(PLANS["month"])
