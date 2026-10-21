"""Тарифи: список → картка тарифу → «Оплатити».

Inline-кнопки «живуть» у чаті вічно. Людина може натиснути кнопку зі списку,
надісланого тиждень тому, коли тарифу вже немає або демо-режим вимкнено.
Тому КОЖНЕ натискання перевіряємо заново: чи тариф існує і чи доступний зараз.
Дані з кнопки — це лише «що людина хоче», а не «що їй дозволено».
"""
import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, Message

from bot import db, texts
from bot.config import Config
from bot.keyboards import BTN_PLANS, PlanCb, plan_card_keyboard, plans_keyboard
from bot.plans import Plan, available_plans
from bot.subscription import purchase_blocker, utcnow

log = logging.getLogger(__name__)
router = Router(name="plans")


@router.message(F.text == BTN_PLANS)
async def show_plans(message: Message, config: Config, plans: dict[str, Plan]) -> None:
    visible = available_plans(plans, config.demo_mode)
    await message.answer(texts.plans_text(visible), reply_markup=plans_keyboard(visible))


async def _edit(callback: CallbackQuery, text: str, markup) -> None:
    """Змінити повідомлення з кнопками. Якщо текст той самий (двічі натиснули) —
    Telegram відповідає помилкою «message is not modified»; це не проблема, ігноруємо."""
    try:
        await callback.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise


async def _show_list(callback: CallbackQuery, config: Config, plans: dict[str, Plan]) -> None:
    visible = available_plans(plans, config.demo_mode)
    await _edit(callback, texts.plans_text(visible), plans_keyboard(visible))


async def _unavailable(callback: CallbackQuery, config: Config, plans: dict[str, Plan]) -> None:
    # На кожне натискання Telegram чекає РІВНО ОДНУ відповідь (callback.answer) —
    # тому спершу пояснення, потім просто оновлюємо список, без другої відповіді.
    await callback.answer("Цей тариф більше недоступний. Ось актуальні 👇", show_alert=True)
    await _show_list(callback, config, plans)


@router.callback_query(PlanCb.filter(F.action == "list"))
async def cb_list(callback: CallbackQuery, config: Config, plans: dict[str, Plan]) -> None:
    await _show_list(callback, config, plans)
    await callback.answer()  # прибирає «годинник» на кнопці


def _find(plans: dict[str, Plan], plan_id: str, config: Config) -> Plan | None:
    plan = plans.get(plan_id)
    if plan is None or plan not in available_plans(plans, config.demo_mode):
        return None
    return plan


@router.callback_query(PlanCb.filter(F.action == "view"))
async def cb_view(callback: CallbackQuery, callback_data: PlanCb,
                  config: Config, plans: dict[str, Plan]) -> None:
    plan = _find(plans, callback_data.plan_id, config)
    if plan is None:
        return await _unavailable(callback, config, plans)
    await _edit(callback, texts.plan_card(plan), plan_card_keyboard(plan))
    await callback.answer()


@router.callback_query(PlanCb.filter(F.action == "buy"))
async def cb_buy(callback: CallbackQuery, callback_data: PlanCb,
                 config: Config, plans: dict[str, Plan]) -> None:
    plan = _find(plans, callback_data.plan_id, config)
    if plan is None:
        return await _unavailable(callback, config, plans)

    sub = await db.get_subscription(callback.from_user.id)
    blocker = purchase_blocker(plan, sub, plans.get(sub.plan_id) if sub else None, utcnow())
    if blocker:
        await callback.answer(blocker, show_alert=True)
        return

    log.info("BUY | user=%s | plan=%s", callback.from_user.id, plan.id)
    # Крок 3: тут створюватиметься платіж і рахунок у mono.
    await callback.answer("🛠 Оплата з'явиться на наступному кроці розробки.", show_alert=True)
