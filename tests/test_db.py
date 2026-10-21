"""Тести бази: головне — платіж зараховується РІВНО ОДИН раз.

Використовуємо тимчасову базу в пам'яті (':memory:') — кожен тест з чистого аркуша,
справжній storage/bot.db не чіпаємо.

pytest сам не вміє запускати async-функції; щоб не тягнути плагін, кожен тест
викликає asyncio.run(...) — це той самий запуск, що в main.py.
"""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from bot import db
from bot.plans import load_plans

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
DAY = timedelta(days=1)
PLANS = load_plans()


def run(coro_fn):
    """Свіжа БД → тест → закрити."""
    async def wrapper():
        await db.init(":memory:")
        try:
            await db.upsert_user(1, "olena", "Олена")
            return await coro_fn()
        finally:
            await db.close()
    return asyncio.run(wrapper())


async def _paid(plan_id: str, now: datetime) -> int:
    pid = await db.create_payment(1, PLANS[plan_id], now)
    await db.set_payment_status(pid, "success")
    return pid


def test_payment_stores_amount_in_kopecks():
    async def t():
        pid = await db.create_payment(1, PLANS["month"], NOW)
        p = await db.get_payment(pid)
        assert (p["amount_kop"], p["status"], p["applied_at"]) == (49000, "created", None)
    run(t)


def test_successful_payment_creates_subscription():
    async def t():
        sub = await db.apply_payment(await _paid("month", NOW), PLANS, NOW)
        assert sub.expires_at == NOW + 30 * DAY
        assert (sub.plan_id, sub.reminders_sent, sub.removed) == ("month", 0, False)
    run(t)


def test_double_apply_extends_only_once():
    """Webhook прийшов двічі / людина натиснула «Перевірити оплату» — продовження одне."""
    async def t():
        pid = await _paid("month", NOW)
        assert await db.apply_payment(pid, PLANS, NOW) is not None
        assert await db.apply_payment(pid, PLANS, NOW + timedelta(seconds=1)) is None
        assert (await db.get_subscription(1)).expires_at == NOW + 30 * DAY
    run(t)


def test_concurrent_apply_extends_only_once():
    """Два виклики ОДНОЧАСНО (webhook і кнопка в ту ж секунду)."""
    async def t():
        pid = await _paid("month", NOW)
        results = await asyncio.gather(*(db.apply_payment(pid, PLANS, NOW) for _ in range(5)))
        assert sum(r is not None for r in results) == 1
        assert (await db.get_subscription(1)).expires_at == NOW + 30 * DAY
        # Без замка ці два рядки падали: rollback «програвшого» виклику скасовував
        # позначку «зараховано» переможця → наступний виклик продовжував удруге.
        assert (await db.get_payment(pid))["applied_at"] is not None
        assert await db.apply_payment(pid, PLANS, NOW) is None
    run(t)


@pytest.mark.parametrize("status", ["created", "processing", "failure", "expired"])
def test_unpaid_payment_is_not_applied(status):
    """Доступ — лише після success. «Я оплатив» на словах нічого не дає."""
    async def t():
        pid = await db.create_payment(1, PLANS["month"], NOW)
        await db.set_payment_status(pid, status)
        assert await db.apply_payment(pid, PLANS, NOW) is None
        assert await db.get_subscription(1) is None
    run(t)


def test_early_renewal_adds_to_current_and_resets_flags():
    async def t():
        await db.apply_payment(await _paid("month", NOW), PLANS, NOW)
        # Планувальник встиг надіслати нагадування
        await db._db.execute("UPDATE subscriptions SET reminders_sent = 1")
        await db._db.commit()
        later = NOW + 27 * DAY
        sub = await db.apply_payment(await _paid("quarter", later), PLANS, later)
        assert sub.expires_at == NOW + 120 * DAY  # 30 + 90, жоден день не згорів
        assert (sub.plan_id, sub.reminders_sent) == ("quarter", 0)
    run(t)


def test_renewal_after_removal_counts_from_now():
    async def t():
        await db.apply_payment(await _paid("month", NOW), PLANS, NOW)
        await db._db.execute("UPDATE subscriptions SET removed = 1, expiry_notified = 1")
        await db._db.commit()
        later = NOW + 40 * DAY
        sub = await db.apply_payment(await _paid("month", later), PLANS, later)
        assert sub.expires_at == later + 30 * DAY
        assert (sub.removed, sub.expiry_notified) == (False, False)
    run(t)


def test_naive_datetime_is_rejected():
    """Дата без часового поясу — тихий баг на годину-дві. Ловимо одразу."""
    async def t():
        with pytest.raises(ValueError, match="часовим поясом"):
            await db.create_payment(1, PLANS["month"], datetime(2026, 10, 20, 12, 0))
    run(t)


# ---------- крок 3: міграції, статуси, відкриті рахунки ----------

def test_migration_upgrades_old_database(tmp_path):
    """База з кроку 1 (без нових колонок) після старту нового коду отримує колонки
    і НЕ втрачає даних. Саме так оновиться база на Railway з живими підписниками."""
    import sqlite3
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript(db.SCHEMA)  # як створив код кроку 1: user_version = 0
    old.execute("INSERT INTO users (user_id, full_name) VALUES (1, 'Олена')")
    old.execute("INSERT INTO payments (user_id, plan_id, amount_kop, created_at) VALUES (1, 'month', 49000, 'x')")
    old.commit()
    old.close()

    async def t():
        await db.init(str(path))
        try:
            async with db._db.execute("PRAGMA user_version") as cur:
                version = (await cur.fetchone())[0]
            p = await db.get_payment(1)
            return version, p
        finally:
            await db.close()
    version, payment = asyncio.run(t())
    assert version == len(db.MIGRATIONS)
    assert payment["amount_kop"] == 49000 and "page_url" in payment and "valid_until" in payment


def test_migrations_run_once(tmp_path):
    """Другий запуск не намагається вдруге додати колонки (інакше — «duplicate column»)."""
    path = str(tmp_path / "bot.db")

    async def start_stop():
        await db.init(path)
        await db.close()
    asyncio.run(start_stop())
    asyncio.run(start_stop())  # не падає


def test_final_status_never_changes():
    """Webhook-и можуть прийти не по порядку: «processing» після «success» не має нічого зламати."""
    async def t():
        pid = await db.create_payment(1, PLANS["month"], NOW)
        assert await db.set_payment_status(pid, "processing")
        assert await db.set_payment_status(pid, "success")
        assert not await db.set_payment_status(pid, "processing")
        assert not await db.set_payment_status(pid, "failure")
        assert (await db.get_payment(pid))["status"] == "success"
    run(t)


def test_find_open_payment():
    async def t():
        pid = await db.create_payment(1, PLANS["month"], NOW)
        # без рахунку в банку — не «відкритий»
        assert await db.find_open_payment(1, "month", NOW) is None
        await db.set_invoice(pid, "inv1", "https://pay", NOW + timedelta(minutes=30))
        assert (await db.find_open_payment(1, "month", NOW))["id"] == pid
        assert await db.find_open_payment(1, "quarter", NOW) is None             # інший тариф
        assert await db.find_open_payment(1, "month", NOW + timedelta(minutes=29)) is None  # згорить за хвилину
        await db.set_payment_status(pid, "failure")
        assert await db.find_open_payment(1, "month", NOW) is None               # остаточний статус
    run(t)
