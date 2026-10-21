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


def choose_plan_keyboard(text: str = "💳 Обрати тариф") -> InlineKeyboardMarkup:
    """Кнопка під «Моя підписка»: веде до списку тарифів."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=text, callback_data=PlanCb(action="list").pack())],
    ])
