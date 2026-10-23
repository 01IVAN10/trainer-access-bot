"""
Тексти для АДМІН-ГРУПИ (крок 7). Окремо від bot/texts.py: там — що бачить клієнт каналу,
тут — що бачить власниця. Різні читачі, різні правила:
  • клієнту — ввічливо і «що робити далі»;
  • власниці — коротко, факти, і так, щоб можна було одразу діяти (ID, щоб скопіювати
    в /grant, посилання на профіль — щоб написати людині).

Усе, що прийшло від людей (ім'я, username), — через escape(): ім'я «<b>Олена</b>»
інакше зламало б розмітку повідомлення (parse_mode=HTML).
"""
from datetime import datetime, timedelta
from html import escape

from bot.plans import Plan, format_duration
from bot.stats import ENDING_SOON, Money, Stats
from bot.subscription import Status, Subscription, get_status, has_access
from bot.texts import format_date, format_left

PAYMENT_STATUS = {
    "created": "⏳ очікує оплати",
    "processing": "⏳ обробляється",
    "success": "✅ оплачено",
    "failure": "❌ не пройшла",
    "expired": "⌛ рахунок згорів",
}


def plural(n: int, one: str, few: str, many: str) -> str:
    """1 оплата, 2 оплати, 5 оплат, 11 оплат, 21 оплата — українська мова так рахує."""
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def uah(kop: int) -> str:
    """49000 → '490 грн', 147000 → '1 470 грн' (пробіл між тисячами, як пишуть у нас)."""
    return f"{kop // 100:,} грн".replace(",", " ")


def who(user: dict | None, user_id: int) -> str:
    """Ім'я-посилання на профіль + @username + ID. tg://user відкриває профіль навіть без username."""
    name = escape(user["full_name"]) if user else "без імені"
    line = f'<a href="tg://user?id={user_id}">{name}</a>'
    if user and user.get("username"):
        line += f" @{escape(user['username'])}"
    return f"{line} · ID <code>{user_id}</code>"


def payment_kind(prev: Subscription | None, prev_plan: Plan | None, now: datetime) -> str:
    """Що означає ця оплата для бізнесу. Нова людина, утримання чи повернення — три різні новини."""
    if prev is None:
        return "🆕 нова підписка"
    if has_access(prev, prev_plan, now):
        return "🔁 продовження"
    return "↩️ повернення після перерви"


def paid_text(user: dict | None, sub: Subscription, plan: Plan | None, payment: dict, kind: str,
              tz: str) -> str:
    title = escape(plan.title) if plan else payment["plan_id"]
    test = " 🧪 тест" if plan is not None and plan.demo else ""
    return (f"💰 <b>Оплата {uah(payment['amount_kop'])}</b>{test}\n"
            f"{who(user, sub.user_id)}\n"
            f"Тариф: {title} · {kind}\n"
            f"Доступ до: {format_date(sub.expires_at, tz)}\n"
            f"Платіж #{payment['id']}")


def removed_text(user: dict | None, sub: Subscription, plan: Plan | None, kicked: bool, tz: str) -> str:
    title = escape(plan.title) if plan else sub.plan_id
    where = "видалено з каналу" if kicked else "у каналі вже не було"
    return (f"👋 <b>Підписку не продовжено</b> — {where}\n"
            f"{who(user, sub.user_id)}\n"
            f"Тариф: {title}, закінчилась {format_date(sub.expires_at, tz)}")


def _money_line(label: str, m: Money) -> str:
    return f"{label}: <b>{uah(m.kop)}</b> ({m.count} {plural(m.count, 'оплата', 'оплати', 'оплат')})"


def stats_text(s: Stats, now: datetime, tz: str, demo_mode: bool) -> str:
    lines = [
        f"📊 <b>Статистика</b> · {format_date(now, tz)}",
        "",
        f"✅ Активних підписок: <b>{s.active}</b>",
        f"⚠️ У пільговому періоді: {s.grace}",
        f"⏰ Закінчуються за {format_left(ENDING_SOON)}: {s.ending_soon}",
        f"👋 Видалено з каналу (усього): {s.removed}",
        "",
        f"👥 Запускали бота: {s.users} · платили хоч раз: {s.paying_users}",
        "",
        "💰 <b>Виручка</b>",
        _money_line("Сьогодні", s.today),
        _money_line("7 днів", s.week),
        _money_line("30 днів", s.month),
    ]
    if s.demo_payments:
        lines.append(f"🧪 Демо-оплат за 30 днів: {s.demo_payments} (у виручку не входять)")
    if demo_mode:
        lines += ["", "<i>Демо-режим: усі оплати тестові, гроші не списувались.</i>"]
    return "\n".join(lines)


def _sub_line(sub: Subscription | None, plan: Plan | None, now: datetime, tz: str) -> str:
    if sub is None:
        return "Підписки немає"
    title = escape(plan.title) if plan else sub.plan_id
    if sub.removed:
        return f"⛔️ Доступ закрито (закінчилась {format_date(sub.expires_at, tz)}; тариф {title})"
    status = get_status(sub.expires_at, plan.grace if plan else timedelta(0), now)
    if status == Status.ACTIVE:
        return f"✅ Активна до {format_date(sub.expires_at, tz)} (ще {format_left(sub.expires_at - now)}; {title})"
    if status == Status.GRACE:
        return f"⚠️ Пільговий період: закінчилась {format_date(sub.expires_at, tz)} ({title})"
    return f"⛔️ Закінчилась {format_date(sub.expires_at, tz)} — видалення за хвилину ({title})"


def user_card(user: dict, sub: Subscription | None, plan: Plan | None, payments: list[dict],
              actions: list[dict], plans: dict[str, Plan], waiting: bool, now: datetime, tz: str) -> str:
    """/user: усе про одну людину на одному екрані — для відповіді на «я заплатила, а доступу немає»."""
    lines = [f"👤 {who(user, user['user_id'])}",
             f"У боті з {user['first_seen'][:10]}",
             "",
             _sub_line(sub, plan, now, tz)]
    if waiting:
        lines.append("🚪 Подала заявку в канал — чекає оплати")
    lines += ["", "<b>Останні оплати</b>"]
    if not payments:
        lines.append("— немає")
    for p in payments:
        p_plan = plans.get(p["plan_id"])
        p_title = escape(p_plan.title) if p_plan else p["plan_id"]
        when = format_date(datetime.fromisoformat(p["created_at"]), tz)
        lines.append(f"#{p['id']} · {when} · {p_title}, {uah(p['amount_kop'])} — "
                     f"{PAYMENT_STATUS.get(p['status'], p['status'])}")
    if actions:
        lines += ["", "<b>Ручні дії адміна</b>"]
        for a in actions:
            when = format_date(datetime.fromisoformat(a["created_at"]), tz)
            verb = "🎁 видано доступ" if a["action"] == "grant" else "⛔️ закрито доступ"
            lines.append(f"{when} · {verb} ({escape(a['details'])}) · адмін <code>{a['admin_id']}</code>")
    return "\n".join(lines)


ADMIN_HELP = (
    "🛠 <b>Команди адміна</b> (працюють лише в цій групі)\n\n"
    "/stats — підписники, виручка, хто скоро закінчується\n"
    "/user <code>@username</code> або <code>ID</code> — усе про людину: підписка, оплати, ручні дії\n"
    "/grant <code>@username 30</code> — видати доступ на N днів без оплати "
    "(готівка, переказ, подарунок). Якщо підписка діє — дні додадуться\n"
    "/revoke <code>@username</code> — закрити доступ зараз і прибрати з каналу "
    "(повернення коштів, порушення правил)\n\n"
    "Сюди ж бот пише про кожну оплату і про тих, хто не продовжив."
)

NOT_FOUND = ("Не знайшов таку людину серед тих, хто запускав бота.\n"
             "Перевірте @username або ID. Якщо людина ще не натискала /start — попросіть її: "
             "без цього бот не може ні знайти її, ні написати їй.")


def granted_text(user: dict, sub: Subscription, plan: Plan | None, duration_days: int, delivered: bool,
                 tz: str, invite_link: str) -> str:
    title = escape(plan.title) if plan else sub.plan_id
    text = (f"🎁 Видано +{duration_days} дн. — {who(user, user['user_id'])}\n"
            f"Доступ до: <b>{format_date(sub.expires_at, tz)}</b> (нагадування й пільговий період — "
            f"як у тарифі «{title}»)\n"
            "Записано в журнал ручних дій.")
    if not delivered:
        text += (f"\n\n⚠️ Написати людині не вдалося (заблокувала бота). Передайте їй посилання "
                 f"на канал — заявку бот схвалить сам: {invite_link}")
    return text


def revoked_text(user: dict, had_access: bool, kicked: str, error: str | None) -> str:
    lines = [f"⛔️ Доступ закрито — {who(user, user['user_id'])}",
             ("Доступ був активний." if had_access else "Активного доступу й так не було.")
             + " Записано в журнал ручних дій."]
    if error:
        lines.append(f"\n⚠️ Але прибрати з каналу НЕ вдалося: {escape(error)}\n"
                     "У базі доступ уже закрито. Повторіть /revoke за хвилину або видаліть людину вручну.")
    elif kicked == "kicked":
        lines.append("Прибрано з каналу (повернутися зможе лише після нової оплати).")
    elif kicked == "protected":
        lines.append("Це адміністратор каналу — з каналу бот його не прибирає.")
    else:
        lines.append("У каналі людини вже не було.")
    return "\n".join(lines)
