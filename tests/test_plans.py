"""Тести тарифів: справжній plans.yaml валідний, а типові помилки людини — ловляться."""
from datetime import timedelta

import pytest

from bot.plans import PLANS_PATH, format_duration, load_plans, parse_duration


@pytest.mark.parametrize("text, expected", [
    ("5m", timedelta(minutes=5)),
    ("12h", timedelta(hours=12)),
    ("30d", timedelta(days=30)),
    (" 1d ", timedelta(days=1)),
    ("0m", timedelta(0)),
])
def test_parse_duration_ok(text, expected):
    assert parse_duration(text) == expected


@pytest.mark.parametrize("text", ["30", "30 д", "1w", "-5m", "", None, "1.5d"])
def test_parse_duration_bad(text):
    with pytest.raises(ValueError):
        parse_duration(text)


def test_format_duration():
    assert format_duration(timedelta(days=30)) == "30 дн."
    assert format_duration(timedelta(minutes=90)) == "1 год 30 хв"
    assert format_duration(timedelta(0)) == "0 хв"


def test_real_plans_file_is_valid():
    """Справжній data/plans.yaml: якщо хтось його зламає — тест впаде ДО деплою."""
    plans = load_plans(PLANS_PATH)
    assert list(plans) == ["demo", "month", "quarter"]  # порядок як у файлі = порядок кнопок
    month = plans["month"]
    assert month.amount_kop == 49000  # гривні → копійки
    assert month.duration == timedelta(days=30)
    assert month.reminders == (timedelta(days=3), timedelta(days=1))
    assert plans["demo"].demo and not month.demo


def _write(tmp_path, body: str):
    path = tmp_path / "plans.yaml"
    path.write_text(body, encoding="utf-8")
    return path


GOOD = """
  - id: month
    title: "1 місяць"
    price: 490
    duration: 30d
    reminders: [3d, 1d]
    grace: 1d
"""


@pytest.mark.parametrize("bad, message", [
    ('  - id: Місяць\n    title: x\n    price: 1\n    duration: 1d', "id"),
    ('  - id: a\n    title: x\n    price: "490"\n    duration: 1d', "price"),
    ('  - id: a\n    title: x\n    price: 0\n    duration: 1d', "price"),
    ('  - id: a\n    title: x\n    price: 1\n    duration: 30 днів', "duration"),
    ('  - id: a\n    title: x\n    price: 1\n    duration: 1d\n    reminders: [2d]', "довшим за сам тариф"),
    ('  - id: a\n    title: x\n    price: 1\n    duration: 9d\n    reminders: [1d, 3d]', "від більшого"),
    ('  - id: a\n    title: x\n    price: 1\n    duration: 9d\n    reminders: [0m]', "більше нуля"),
    ('  - id: a\n    price: 1\n    duration: 1d', "title"),
])
def test_bad_plan_is_rejected(tmp_path, bad, message):
    with pytest.raises(ValueError, match=message):
        load_plans(_write(tmp_path, "plans:\n" + bad))


def test_duplicate_id(tmp_path):
    with pytest.raises(ValueError, match="повторюється"):
        load_plans(_write(tmp_path, "plans:" + GOOD + GOOD))


def test_all_errors_reported_at_once(tmp_path):
    """Людина має побачити ВСІ помилки одразу, а не виправляти по одній і перезапускати."""
    bad = "plans:\n  - id: a\n    title: x\n    price: -1\n    duration: 30 днів"
    with pytest.raises(ValueError) as exc:
        load_plans(_write(tmp_path, bad))
    assert "price" in str(exc.value) and "duration" in str(exc.value)


def test_empty_file(tmp_path):
    with pytest.raises(ValueError, match="немає жодного тарифу"):
        load_plans(_write(tmp_path, "plans: []"))


def test_demo_plan_hidden_without_demo_mode():
    from bot.plans import available_plans
    plans = load_plans()
    assert [p.id for p in available_plans(plans, demo_mode=True)] == ["demo", "month", "quarter"]
    assert [p.id for p in available_plans(plans, demo_mode=False)] == ["month", "quarter"]


def test_callback_data_fits_telegram_limit():
    """Telegram обмежує дані кнопки 64 байтами. Довгий id тарифу зламав би кнопки «мовчки»."""
    from bot.keyboards import PlanCb
    for plan in load_plans().values():
        assert len(PlanCb(action="view", plan_id=plan.id).pack().encode()) <= 64
