"""Тарифи: список → картка тарифу. «Оплатити» — у handlers/pay.py.

Inline-кнопки «живуть» у чаті вічно. Людина може натиснути кнопку зі списку,
надісланого тиждень тому, коли тарифу вже немає або демо-режим вимкнено.
Тому КОЖНЕ натискання перевіряємо заново: чи тариф існує і чи доступний зараз.
Дані з кнопки — це лише «що людина хоче», а не «що їй дозволено».
"""
from aiogram import F, Router
from aiogram.types import CallbackQuery, Message

from bot import texts
from bot.config import Config
from bot.handlers.common import edit
from bot.keyboards import BTN_PLANS, PlanCb, plan_card_keyboard, plans_keyboard
from bot.plans import Plan, available_plans

router = Router(name="plans")


@router.message(F.text == BTN_PLANS)
async def show_plans(message: Message, config: Config, plans: dict[str, Plan]) -> None:
    visible = available_plans(plans, config.demo_mode)
    await message.answer(texts.plans_text(visible), reply_markup=plans_keyboard(visible))


async def _show_list(callback: CallbackQuery, config: Config, plans: dict[str, Plan]) -> None:
    visible = available_plans(plans, config.demo_mode)
    await edit(callback, texts.plans_text(visible), plans_keyboard(visible))


async def unavailable(callback: CallbackQuery, config: Config, plans: dict[str, Plan]) -> None:
    # На кожне натискання Telegram чекає РІВНО ОДНУ відповідь (callback.answer) —
    # тому спершу пояснення, потім просто оновлюємо список, без другої відповіді.
    await callback.answer("Цей тариф більше недоступний. Ось актуальні 👇", show_alert=True)
    await _show_list(callback, config, plans)


@router.callback_query(PlanCb.filter(F.action == "list"))
async def cb_list(callback: CallbackQuery, config: Config, plans: dict[str, Plan]) -> None:
    await _show_list(callback, config, plans)
    await callback.answer()  # прибирає «годинник» на кнопці


def find_plan(plans: dict[str, Plan], plan_id: str, config: Config) -> Plan | None:
    plan = plans.get(plan_id)
    if plan is None or plan not in available_plans(plans, config.demo_mode):
        return None
    return plan


@router.callback_query(PlanCb.filter(F.action == "view"))
async def cb_view(callback: CallbackQuery, callback_data: PlanCb,
                  config: Config, plans: dict[str, Plan]) -> None:
    plan = find_plan(plans, callback_data.plan_id, config)
    if plan is None:
        return await unavailable(callback, config, plans)
    await edit(callback, texts.plan_card(plan), plan_card_keyboard(plan))
    await callback.answer()
