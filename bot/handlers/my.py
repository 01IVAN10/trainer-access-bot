"""«Моя підписка»: статус, дата закінчення за київським часом, кнопка продовжити."""
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import Message

from bot import db, texts
from bot.config import Config
from bot.keyboards import BTN_MY, choose_plan_keyboard
from bot.plans import Plan
from bot.subscription import Status, get_status, utcnow

router = Router(name="my")


@router.message(F.text == BTN_MY)
@router.message(Command("my"))
async def my_subscription(message: Message, config: Config, plans: dict[str, Plan]) -> None:
    now = utcnow()
    sub = await db.get_subscription(message.from_user.id)
    plan = plans.get(sub.plan_id) if sub else None
    text = texts.subscription_text(sub, plan, now, config.timezone)

    active = sub is not None and not sub.removed and plan is not None and \
        get_status(sub.expires_at, plan.grace, now) == Status.ACTIVE
    await message.answer(text, reply_markup=choose_plan_keyboard("🔄 Продовжити" if active else "💳 Обрати тариф"))
