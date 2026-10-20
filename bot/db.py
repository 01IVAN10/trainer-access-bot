"""
База даних (SQLite). Підхід з етапу 1: одне з'єднання на весь бот, «?»-параметри
замість f-рядків, PRAGMA foreign_keys. Таблиці нові:

  users          — хто писав боту
  payments       — КОЖНА спроба оплати (рахунок у mono), навіть неуспішна
  subscriptions  — поточний стан доступу: ОДИН рядок на користувача

Чому платежі й підписки — окремі таблиці:
- платіж — це ПОДІЯ («о 14:05 Олена заплатила 490 грн»), їх багато, вони не змінюються
  після завершення. Це історія і бухгалтерія: виручка, повернення, суперечки;
- підписка — це СТАН («доступ до 20.11»), він один і постійно оновлюється.
Підписка виводиться з платежів, але зберігати стан окремо простіше і швидше.

Час зберігаємо як текст ISO в UTC: '2026-10-20T16:05:00+00:00'. Однаковий формат →
рядки порівнюються як дати, і SQLite може фільтрувати «expires_at < ?».

ЗАМОК (_write_lock). З'єднання одне на весь бот, а обробників багато, і вони працюють
«навперемінно» (async). Транзакція належить З'ЄДНАННЮ, а не функції. Тож без замка:
  A: UPDATE платежу …(чекає)…            B: rollback()  ← скасовує і роботу A!
  A: INSERT підписки, commit()           → підписку продовжено, а платіж «не зараховано»
                                          → наступний виклик продовжить ще раз.
Це знайшов тест test_concurrent_apply_extends_only_once. Правило: КОЖЕН запис у базу —
лише під замком. Читання — без замка.

Перевірка:  python -m bot.db   (створить storage/bot.db і покаже таблиці)
"""
import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

from bot.plans import Plan
from bot.subscription import Subscription, new_expiry

log = logging.getLogger(__name__)

_db: aiosqlite.Connection | None = None
_write_lock: asyncio.Lock | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id    INTEGER PRIMARY KEY,          -- Telegram ID
    username   TEXT,
    full_name  TEXT NOT NULL,
    first_seen TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS payments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,  -- наш номер платежу
    user_id     INTEGER NOT NULL REFERENCES users(user_id),
    plan_id     TEXT NOT NULL,                       -- який тариф купували
    amount_kop  INTEGER NOT NULL,                    -- сума В КОПІЙКАХ, зафіксована в момент покупки
    invoice_id  TEXT UNIQUE,                         -- ID рахунку в mono (з'явиться на кроці 3)
    status      TEXT NOT NULL DEFAULT 'created'
                CHECK (status IN ('created', 'processing', 'success', 'failure', 'expired')),
    created_at  TEXT NOT NULL,
    applied_at  TEXT                                  -- коли зарахували в підписку; NULL = ще ні
);
CREATE INDEX IF NOT EXISTS idx_payments_user ON payments(user_id);

CREATE TABLE IF NOT EXISTS subscriptions (
    user_id         INTEGER PRIMARY KEY REFERENCES users(user_id),  -- один користувач = одна підписка
    plan_id         TEXT NOT NULL,       -- тариф останньої оплати (звідти нагадування і grace)
    expires_at      TEXT NOT NULL,       -- UTC ISO
    reminders_sent  INTEGER NOT NULL DEFAULT 0,
    expiry_notified INTEGER NOT NULL DEFAULT 0,   -- 0/1: SQLite не має типу BOOLEAN
    removed         INTEGER NOT NULL DEFAULT 0,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_subscriptions_expires ON subscriptions(expires_at);
"""


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        # Наївна дата (без часового поясу) — майже завжди баг. Краще впасти тут.
        raise ValueError("Очікую дату з часовим поясом (UTC), а прийшла без нього")
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


async def init(path: str) -> None:
    """Відкриває БД (створює, якщо нема) і таблиці. ':memory:' — тимчасова БД для тестів."""
    global _db, _write_lock
    # Замок створюємо тут, а не при імпорті: він «прив'язується» до циклу подій, у якому
    # працює. У тестах кожен asyncio.run() — новий цикл, тож і замок має бути новий.
    _write_lock = asyncio.Lock()
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    _db = await aiosqlite.connect(path)
    _db.row_factory = aiosqlite.Row
    await _db.execute("PRAGMA foreign_keys = ON")
    await _db.executescript(SCHEMA)
    await _db.commit()
    log.info("База даних: %s", path)


async def close() -> None:
    global _db
    if _db is not None:
        await _db.close()
        _db = None


# ---------- користувачі ----------

async def upsert_user(user_id: int, username: str | None, full_name: str) -> None:
    async with _write_lock:
        await _db.execute(
            """INSERT INTO users (user_id, username, full_name) VALUES (?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                   username = excluded.username,
                   full_name = excluded.full_name,
                   last_seen = CURRENT_TIMESTAMP""",
            (user_id, username, full_name),
        )
        await _db.commit()


# ---------- платежі ----------

async def create_payment(user_id: int, plan: Plan, now: datetime) -> int:
    """Записує намір оплатити ДО звернення в mono. Повертає наш номер платежу.

    Сума фіксується зараз: якщо власник завтра змінить ціну в YAML, ця оплата
    все одно буде на ту суму, яку людина побачила.
    """
    async with _write_lock:
        cur = await _db.execute(
            "INSERT INTO payments (user_id, plan_id, amount_kop, created_at) VALUES (?, ?, ?, ?)",
            (user_id, plan.id, plan.amount_kop, _iso(now)),
        )
        await _db.commit()
    return cur.lastrowid


async def set_payment_status(payment_id: int, status: str) -> None:
    async with _write_lock:
        await _db.execute("UPDATE payments SET status = ? WHERE id = ?", (status, payment_id))
        await _db.commit()


async def get_payment(payment_id: int) -> dict | None:
    async with _db.execute("SELECT * FROM payments WHERE id = ?", (payment_id,)) as cur:
        row = await cur.fetchone()
    return dict(row) if row else None


# ---------- підписки ----------

def _row_to_sub(row) -> Subscription:
    return Subscription(
        user_id=row["user_id"],
        plan_id=row["plan_id"],
        expires_at=_dt(row["expires_at"]),
        reminders_sent=row["reminders_sent"],
        expiry_notified=bool(row["expiry_notified"]),
        removed=bool(row["removed"]),
    )


async def get_subscription(user_id: int) -> Subscription | None:
    async with _db.execute("SELECT * FROM subscriptions WHERE user_id = ?", (user_id,)) as cur:
        row = await cur.fetchone()
    return _row_to_sub(row) if row else None


async def apply_payment(payment_id: int, plans: dict[str, Plan], now: datetime) -> Subscription | None:
    """Зараховує УСПІШНИЙ платіж у підписку. Рівно ОДИН раз.

    Повертає нову підписку або None, якщо платіж не успішний чи вже зарахований.

    Ідемпотентність — головне в оплатах: mono може надіслати webhook двічі, а людина
    паралельно натисне «Перевірити оплату». Без захисту — подвійне продовження.

    Захист — один атомарний UPDATE: «познач зарахованим, ЯКЩО ще не зарахований».
    База виконує його цілком; хто перший — той змінив рядок (rowcount = 1),
    другий отримає rowcount = 0 і нічого не зробить. Перевірка «if not applied:»
    у Python так не працює: між перевіркою і записом може вклинитися інший виклик.
    Плюс замок: щоб чужий rollback/commit не зачепив цю транзакцію (див. початок файлу).
    """
    async with _write_lock:  # уся транзакція — під замком (див. початок файлу)
        try:
            cur = await _db.execute(
                "UPDATE payments SET applied_at = ? "
                "WHERE id = ? AND status = 'success' AND applied_at IS NULL",
                (_iso(now), payment_id),
            )
            if cur.rowcount != 1:
                await _db.rollback()
                return None

            payment = await get_payment(payment_id)
            plan = plans.get(payment["plan_id"])
            if plan is None:  # тариф видалили з plans.yaml, а оплата за нього прийшла
                raise ValueError(f"Платіж #{payment_id}: тарифу {payment['plan_id']!r} немає в plans.yaml")
            current = await get_subscription(payment["user_id"])
            expires = new_expiry(current.expires_at if current else None, now, plan.duration)

            # Новий період → лічильники нагадувань і прапорці скидаємо.
            await _db.execute(
                """INSERT INTO subscriptions (user_id, plan_id, expires_at, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(user_id) DO UPDATE SET
                       plan_id = excluded.plan_id,
                       expires_at = excluded.expires_at,
                       reminders_sent = 0,
                       expiry_notified = 0,
                       removed = 0,
                       updated_at = excluded.updated_at""",
                (payment["user_id"], plan.id, _iso(expires), _iso(now)),
            )
            # Зарахування платежу і продовження підписки — ОДНА транзакція:
            # або обидва записи збережуться, або жоден (помилка посередині → rollback).
            await _db.commit()
        except Exception:
            await _db.rollback()
            raise
    log.info("Платіж #%s зараховано: user=%s, тариф=%s, до %s",
             payment_id, payment["user_id"], plan.id, _iso(expires))
    return await get_subscription(payment["user_id"])


if __name__ == "__main__":
    import asyncio

    async def _show() -> None:
        await init("storage/bot.db")
        async with _db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name") as cur:
            print("Таблиці:", ", ".join(r["name"] for r in await cur.fetchall()))
        await close()

    asyncio.run(_show())
