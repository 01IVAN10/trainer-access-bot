"""
Клавіатури. Тут два різні види кнопок — і це свідомий вибір:

ReplyKeyboard (головне меню внизу екрана) — як у барбершопі: завжди перед очима,
натискання приходить як звичайний текст.

InlineKeyboard (кнопки ПІД повідомленням) — для тарифів. Натискання приходить як
callback з даними («показати тариф month»), і бот може ЗМІНИТИ те саме повідомлення:
список → картка тарифу → назад до списку. Чат не засмічується десятком повідомлень.
"""
from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

from bot.plans import Plan

BTN_PLANS = "💳 Тарифи"
BTN_MY = "📋 Моя підписка"
BTN_HELP = "❓ Допомога"

main_menu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BTN_PLANS), KeyboardButton(text=BTN_MY)],
        [KeyboardButton(text=BTN_HELP)],
    ],
    resize_keyboard=True,
    input_field_placeholder="Оберіть пункт меню",
)


class PlanCb(CallbackData, prefix="plan"):
    """Дані inline-кнопки. aiogram сам упакує їх у рядок 'plan:view:month' і розпакує назад.

    Обмеження Telegram: callback_data — до 64 байтів. Тому в кнопці лише id тарифу,
    а не назва чи ціна (та й ціну з кнопки брати не можна — її можна підробити).
    """
    action: str           # list | view | buy
    plan_id: str = ""


def plans_keyboard(plans: list[Plan]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=f"{p.title} — {p.price_uah} грн",
                                  callback_data=PlanCb(action="view", plan_id=p.id).pack())]
            for p in plans]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def plan_card_keyboard(plan: Plan) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"💳 Оплатити {plan.price_uah} грн",
                              callback_data=PlanCb(action="buy", plan_id=plan.id).pack())],
        [InlineKeyboardButton(text="⬅️ Усі тарифи", callback_data=PlanCb(action="list").pack())],
    ])


def choose_plan_keyboard(text: str = "💳 Обрати тариф", channel_link: str | None = None) -> InlineKeyboardMarkup:
    """Кнопки під «Моя підписка»: (посилання на канал, якщо доступ є) + до тарифів."""
    rows = []
    if channel_link:
        rows.append([InlineKeyboardButton(text="🔑 Посилання на канал", url=channel_link)])
    rows.append([InlineKeyboardButton(text=text, callback_data=PlanCb(action="list").pack())])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def renew_keyboard(plan: Plan | None) -> InlineKeyboardMarkup:
    """Під нагадуванням: продовжити ТОЙ САМИЙ тариф одним натисканням (+ інші тарифи).

    Один дотик замість «Тарифи → обрати → Оплатити» — менше кроків, більше продовжень.
    plan = None (тариф прибрали або він зараз недоступний) → лише список тарифів.
    """
    rows = []
    if plan is not None:
        rows.append([InlineKeyboardButton(text=f"🔄 Продовжити «{plan.title}» — {plan.price_uah} грн",
                                          callback_data=PlanCb(action="buy", plan_id=plan.id).pack())])
    rows.append([InlineKeyboardButton(text="💳 Усі тарифи", callback_data=PlanCb(action="list").pack())])
    return InlineKeyboardMarkup(inline_keyboard=rows)


START_PLANS = "plans"  # /start plans — відкрити бота одразу з тарифами


def start_bot_keyboard(bot_username: str) -> InlineKeyboardMarkup:
    """Для людини, яка ще НЕ запускала бота (прийшла через заявку в канал).

    Не callback-кнопка, а ПОСИЛАННЯ в бот: t.me/<бот>?start=plans. Telegram відкриє чат
    з ботом і кнопкою «Start». Поки людина її не натисне, бот не може писати їй нових
    повідомлень (лише у відповідь на заявку — 5 хв і доки заявку не оброблено). А нам
    писати ще доведеться: «оплату отримано», нагадування, «підписка закінчилась».
    """
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
        text="💳 Обрати тариф", url=f"https://t.me/{bot_username}?start={START_PLANS}")]])


def channel_keyboard(channel_link: str, granted: str) -> InlineKeyboardMarkup:
    """Після оплати. Те саме посилання і для нових, і для тих, хто вже в каналі:
    учасникові Telegram просто відкриє канал, новому — запропонує подати заявку."""
    text = "🔑 Вступити в канал" if granted == "link" else "📢 Відкрити канал"
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=text, url=channel_link)]])


class PayCb(CallbackData, prefix="pay"):
    """Кнопка «Я оплатив — перевірити». У ній лише НАШ номер платежу.

    Номер можна підробити (вписати чужий) — тому хендлер перевіряє, що платіж
    належить саме тому, хто натиснув.
    """
    action: str        # check
    payment_id: int


def invoice_keyboard(page_url: str, amount_uah: int, payment_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        # url-кнопка: Telegram сам відкриє сторінку оплати, бот про натискання не дізнається
        [InlineKeyboardButton(text=f"💳 Оплатити {amount_uah} грн", url=page_url)],
        [InlineKeyboardButton(text="✅ Я оплатив — перевірити",
                              callback_data=PayCb(action="check", payment_id=payment_id).pack())],
        [InlineKeyboardButton(text="⬅️ Усі тарифи", callback_data=PlanCb(action="list").pack())],
    ])


def retry_keyboard(plan_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Спробувати ще раз",
                              callback_data=PlanCb(action="buy", plan_id=plan_id).pack())],
        [InlineKeyboardButton(text="⬅️ Усі тарифи", callback_data=PlanCb(action="list").pack())],
    ])
