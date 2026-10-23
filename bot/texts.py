"""
Тексти для людей. ЧИСТІ функції: отримують дані → повертають рядок.

Навіщо окремий файл, а не текст прямо в хендлерах:
- тексти можна протестувати без Telegram (див. tests/test_texts.py);
- клієнт попросить «переформулюйте» — правиш в одному місці, не шукаючи по коду;
- хендлер лишається коротким: отримав подію → взяв дані → відповів.

Дати: у базі все в UTC, тут переводимо в часовий пояс людини (Europe/Kyiv).
"""
from datetime import datetime, timedelta
from html import escape
from zoneinfo import ZoneInfo

from bot.plans import Plan, format_duration
from bot.subscription import Status, Subscription, get_status

BUSINESS_NAME = "Сила вдома"


def format_date(dt: datetime, tz: str) -> str:
    """UTC → місцевий час: '20.11.2026 о 14:05'."""
    return dt.astimezone(ZoneInfo(tz)).strftime("%d.%m.%Y о %H:%M")


def _tz_note(tz: str) -> str:
    return " (за київським часом)" if tz == "Europe/Kyiv" else f" ({tz})"


def format_left(td: timedelta) -> str:
    """Скільки лишилось — без зайвої точності: '29 дн. 3 год', '5 год 12 хв', '3 хв'."""
    seconds = int(td.total_seconds())
    if seconds < 60:
        return "менше хвилини"
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days} дн. {hours} год" if hours else f"{days} дн."
    if hours:
        return f"{hours} год {minutes} хв" if minutes else f"{hours} год"
    return f"{minutes} хв"


def start_text(name: str, demo_mode: bool) -> str:
    text = (
        f"Привіт, {escape(name)}! 💪\n\n"
        f"Це бот закритого каналу <b>«{BUSINESS_NAME}»</b> — тренування вдома без залу: "
        "програми, відео з технікою, плани харчування.\n\n"
        "Тут можна оформити доступ, перевірити, до якої дати він діє, і продовжити його."
    )
    if demo_mode:
        text += ("\n\n<i>🧪 Це демо: бізнес вигаданий, оплата ТЕСТОВА — гроші не списуються. "
                 "Спробуйте тариф «Демо — 5 хвилин»: за кілька хвилин побачите весь цикл.</i>")
    return text


def help_text(support: str, demo_mode: bool) -> str:
    text = (
        "<b>Що вміє бот</b>\n"
        "💳 <b>Тарифи</b> — обрати й оплатити доступ до каналу\n"
        "📋 <b>Моя підписка</b> — до якої дати діє доступ\n\n"
        "Перед закінченням підписки я нагадаю, щоб ви встигли продовжити. "
        "Якщо продовжити раніше — дні, що лишились, не згорять.\n\n"
        f"Питання щодо оплати: /paysupport\n"
        f"Інші питання: {escape(support)}"
    )
    if demo_mode:
        text += "\n\n<i>🧪 Демо-бот: оплата тестова.</i>"
    return text


def paysupport_text(support: str, demo_mode: bool) -> str:
    text = (
        "<b>Підтримка з оплати</b>\n\n"
        f"Напишіть {escape(support)} і вкажіть:\n"
        "• коли платили (дата й приблизний час);\n"
        "• за який тариф;\n"
        "• що пішло не так.\n\n"
        "Ми перевіримо платіж і відповімо."
    )
    if demo_mode:
        text += "\n\n<i>🧪 У демо гроші не списуються — оплата лише тестова.</i>"
    return text


def plans_text(plans: list[Plan]) -> str:
    lines = [f"<b>Тарифи «{BUSINESS_NAME}»</b>", ""]
    for p in plans:
        lines.append(f"{'🧪' if p.demo else '•'} <b>{escape(p.title)}</b> — {p.price_uah} грн")
    lines += ["", "Оберіть тариф, щоб побачити деталі:"]
    return "\n".join(lines)


def plan_card(plan: Plan) -> str:
    lines = [f"<b>{escape(plan.title)}</b>", ""]
    if plan.description:
        lines += [escape(plan.description), ""]
    lines.append(f"💰 Ціна: <b>{plan.price_uah} грн</b>")
    lines.append(f"⏳ Доступ: {format_duration(plan.duration)}")
    if plan.reminders:
        lines.append(f"🔔 Нагадаю за {', '.join(format_duration(r) for r in plan.reminders)} до кінця")
    if plan.demo:
        lines += ["", "<i>🧪 Тестова оплата: гроші не списуються.</i>"]
    return "\n".join(lines)


def subscription_text(sub: Subscription | None, plan: Plan | None, now: datetime, tz: str) -> str:
    """Відповідь на «Моя підписка». Чотири стани — чотири різні тексти."""
    if sub is None:
        return "У вас поки немає підписки.\n\nОберіть тариф — і доступ до каналу відкриється одразу після оплати."

    # Тариф могли прибрати з plans.yaml — тоді вважаємо, що пільгового періоду немає.
    grace = plan.grace if plan else timedelta(0)
    title = escape(plan.title) if plan else sub.plan_id
    status = get_status(sub.expires_at, grace, now)
    until = format_date(sub.expires_at, tz)

    if status == Status.ACTIVE and not sub.removed:
        return (f"✅ <b>Підписка активна</b>\n\n"
                f"Тариф: {title}\n"
                f"Діє до: <b>{until}</b>{_tz_note(tz)}\n"
                f"Залишилось: {format_left(sub.expires_at - now)}\n\n"
                "Продовжити можна будь-коли — дні, що лишились, додадуться.")
    if status == Status.GRACE and not sub.removed:
        return (f"⚠️ <b>Підписка закінчилась</b> {until}\n\n"
                f"Доступ до каналу збережеться ще {format_left(sub.expires_at + grace - now)}. "
                "Продовжіть, щоб не втратити його.")
    return (f"⛔️ <b>Підписка закінчилась</b> {until}\n\n"
            "Доступ до каналу закрито. Оформіть тариф — і він відкриється знову.")


# ---------- оплата (крок 3) ----------

def invoice_text(plan: Plan, amount_kop: int, valid_until: datetime, tz: str, demo_mode: bool) -> str:
    lines = [
        f"🧾 <b>Рахунок: {escape(plan.title)}</b> — {amount_kop // 100} грн",
        "",
        "1. Натисніть <b>«💳 Оплатити»</b> — відкриється захищена сторінка monobank.",
        "2. Після оплати поверніться сюди і натисніть <b>«✅ Я оплатив — перевірити»</b>.",
        "",
        f"Рахунок дійсний до {format_date(valid_until, tz)}.",
    ]
    if demo_mode:
        lines += ["", "<i>🧪 Тестова оплата — гроші не списуються. Картка: "
                      "<code>4242 4242 4242 4242</code>, будь-який термін дії і CVV.</i>"]
    return "\n".join(lines)


# Що сказати після оплати — залежить від того, чи людина вже в каналі (bot/access.py → Granted).
_ACCESS_NOTE = {
    "approved": "🎉 Вашу заявку в канал схвалено — ви вже всередині!",
    # Не «продовжено»: сюди потрапляє і перша оплата, якщо інший шлях (звірка, webhook) уже
    # встиг схвалити заявку. Текст має бути правдивим в обох випадках.
    "member": "Ви вже в каналі — доступ активний 💪",
    "link": ("Натисніть <b>«🔑 Вступити в канал»</b> і подайте заявку — "
             "я схвалю її автоматично за кілька секунд."),
}


def payment_success_text(plan: Plan | None, sub: Subscription, tz: str, granted: str = "link") -> str:
    title = escape(plan.title) if plan else sub.plan_id
    return (f"✅ <b>Оплату отримано!</b>\n\n"
            f"Тариф: {title}\n"
            f"Доступ діє до: <b>{format_date(sub.expires_at, tz)}</b>{_tz_note(tz)}\n\n"
            f"{_ACCESS_NOTE[granted]}")


# ---------- заявки на вступ (крок 5) ----------

JOIN_APPROVED = f"✅ Заявку схвалено — ласкаво просимо в «{BUSINESS_NAME}»! 💪"


def join_needs_subscription(demo_mode: bool) -> str:
    text = (f"👋 Бачу вашу заявку в канал «{BUSINESS_NAME}».\n\n"
            "Щоб я її схвалив, потрібна активна підписка. Оберіть тариф — "
            "одразу після оплати впущу вас автоматично, заявку подавати вдруге не треба.")
    if demo_mode:
        text += "\n\n<i>🧪 Демо: спробуйте тариф «Демо — 5 хвилин», оплата тестова.</i>"
    return text


PAYMENT_PENDING = ("⏳ Оплату ще не отримано.\n\n"
                   "Якщо ви вже оплатили — зачекайте хвилину і натисніть «Перевірити» ще раз.")
PAYMENT_FAILED = ("❌ <b>Оплата не пройшла.</b>\n\n"
                  "Гроші не списано. Спробуйте ще раз або іншою карткою. Якщо не виходить — /paysupport")
PAYMENT_EXPIRED = "⌛ <b>Час на оплату цього рахунку минув.</b>\n\nСтворіть новий — це займе секунду."
PAYMENT_PROVIDER_DOWN = ("😕 Не вдалося зв'язатися з банком. Спробуйте за хвилину.\n"
                         "Якщо повторюється — /paysupport")
