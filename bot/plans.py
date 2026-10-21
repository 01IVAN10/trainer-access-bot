"""
Тарифи: читаємо data/plans.yaml і ПЕРЕВІРЯЄМО при старті.

Логіка та сама, що з business.yaml в етапі 1: дані бізнесу — у файлі, який
власник може змінити без програміста. Але файл редагує людина, а людина помиляється
(«30 д» замість «30d», нагадування довше за сам тариф). Тому всі помилки шукаємо
ОДРАЗУ при запуску і показуємо списком — fail fast, як з .env.

Перевірка:  python -m bot.plans
"""
import re
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import yaml

PLANS_PATH = Path(__file__).resolve().parent.parent / "data" / "plans.yaml"

_DURATION = re.compile(r"^\s*(\d+)\s*([mhd])\s*$")
_UNITS = {"m": "minutes", "h": "hours", "d": "days"}
# id потрапляє в кнопки (callback_data) і в базу — лише безпечні символи.
_PLAN_ID = re.compile(r"^[a-z0-9_]{1,32}$")


def parse_duration(value) -> timedelta:
    """'5m' → 5 хвилин, '12h' → 12 годин, '30d' → 30 днів."""
    match = _DURATION.match(str(value))
    if not match:
        raise ValueError(f"неправильна тривалість {value!r} (очікую число + m/h/d, наприклад 30d)")
    number, unit = match.groups()
    return timedelta(**{_UNITS[unit]: int(number)})


def format_duration(td: timedelta) -> str:
    """Для повідомлень: 30 днів → '30 дн.', 90 хв → '1 год 30 хв'."""
    total_minutes = int(td.total_seconds() // 60)
    days, rest = divmod(total_minutes, 24 * 60)
    hours, minutes = divmod(rest, 60)
    parts = [f"{days} дн." if days else "", f"{hours} год" if hours else "", f"{minutes} хв" if minutes else ""]
    return " ".join(p for p in parts if p) or "0 хв"


@dataclass(frozen=True)
class Plan:
    id: str
    title: str
    description: str
    price_uah: int
    duration: timedelta
    reminders: tuple[timedelta, ...]  # від більшого до меншого
    grace: timedelta
    demo: bool = False

    @property
    def amount_kop(self) -> int:
        """Сума для mono — у КОПІЙКАХ. 490 грн → 49000. Інакше рахунок вийде на 4,90 грн."""
        return self.price_uah * 100


def _parse_plan(raw: dict, n: int) -> tuple[Plan | None, list[str]]:
    """Розбирає один тариф. Повертає (тариф або None, список помилок)."""
    where = f"тариф №{n} ({raw.get('id', 'без id')})"
    errors: list[str] = []

    plan_id = str(raw.get("id", ""))
    if not _PLAN_ID.match(plan_id):
        errors.append(f"{where}: id має бути з латиниці/цифр/_, а зараз {plan_id!r}")
    title = str(raw.get("title", "")).strip()
    if not title:
        errors.append(f"{where}: не задано title")

    price = raw.get("price")
    # bool — теж int у Python (True == 1), тому відсіюємо окремо.
    if not isinstance(price, int) or isinstance(price, bool) or price <= 0:
        errors.append(f"{where}: price має бути цілим числом гривень > 0, а зараз {price!r}")

    try:
        duration = parse_duration(raw.get("duration"))
        if duration <= timedelta(0):
            errors.append(f"{where}: duration має бути більше нуля")
    except ValueError as e:
        errors.append(f"{where}: duration — {e}")
        duration = None

    try:
        grace = parse_duration(raw.get("grace", "0m"))
    except ValueError as e:
        errors.append(f"{where}: grace — {e}")
        grace = None

    reminders: list[timedelta] = []
    for value in raw.get("reminders") or []:
        try:
            reminders.append(parse_duration(value))
        except ValueError as e:
            errors.append(f"{where}: reminders — {e}")
    if any(r <= timedelta(0) for r in reminders):
        errors.append(f"{where}: нагадування має бути більше нуля (0 = момент закінчення, "
                      "про нього бот пише окремо)")
    if duration and any(r >= duration for r in reminders):
        errors.append(f"{where}: нагадування не може бути довшим за сам тариф ({raw.get('duration')})")
    if reminders != sorted(set(reminders), reverse=True):
        errors.append(f"{where}: reminders — від більшого до меншого, без повторів (наприклад [3d, 1d])")

    if errors:
        return None, errors
    return Plan(
        id=plan_id,
        title=title,
        description=str(raw.get("description", "")).strip(),
        price_uah=price,
        duration=duration,
        reminders=tuple(reminders),
        grace=grace,
        demo=bool(raw.get("demo", False)),
    ), []


def available_plans(plans: dict[str, Plan], demo_mode: bool) -> list[Plan]:
    """Які тарифи показувати в меню. Демо-тариф — лише в демо-режимі (DEMO_MODE=1).

    Сам тариф з YAML не прибираємо: якщо хтось купив демо, а потім режим вимкнули,
    підписка має й далі знаходити свій тариф (нагадування, grace).
    """
    return [p for p in plans.values() if demo_mode or not p.demo]


def load_plans(path: Path = PLANS_PATH) -> dict[str, Plan]:
    """Завантажує тарифи. Будь-яка помилка → ValueError з УСІМА проблемами одразу.

    Повертаємо dict id → Plan: у базі зберігається лише id, а повний тариф
    шукаємо тут. Порядок як у файлі — у такому ж порядку бот покаже кнопки.
    """
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    raw_plans = data.get("plans") or []
    if not raw_plans:
        raise ValueError(f"У {path} немає жодного тарифу (розділ plans:)")

    plans: dict[str, Plan] = {}
    errors: list[str] = []
    for n, raw in enumerate(raw_plans, start=1):
        plan, plan_errors = _parse_plan(raw or {}, n)
        errors += plan_errors
        if plan and plan.id in plans:
            errors.append(f"тариф №{n}: id {plan.id!r} повторюється")
        elif plan:
            plans[plan.id] = plan
    if errors:
        raise ValueError("Помилки в тарифах:\n  • " + "\n  • ".join(errors))
    return plans


if __name__ == "__main__":
    for p in load_plans().values():
        reminders = ", ".join(format_duration(r) for r in p.reminders) or "—"
        print(f"✅ {p.id:8} {p.title:18} {p.price_uah:>5} грн | {format_duration(p.duration):>8} | "
              f"нагадування за: {reminders} | grace: {format_duration(p.grace)}{' | DEMO' if p.demo else ''}")
