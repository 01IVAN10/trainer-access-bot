"""«Моя підписка»: статус, дата закінчення за київським часом, кнопка продовжити."""
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import Message

from bot import db, texts
from bot.config import Config
from bot.keyboards import BTN_MY, choose_plan_keyboard
from bot.plans import Plan
from bot.access import ChannelAccess
from bot.subscription import Status, get_status, has_access, utcnow

router = Router(name="my")


@router.message(F.text == BTN_MY)
@router.message(Command("my"))
async def my_subscription(message: Message, config: Config, plans: dict[str, Plan],
                          access: ChannelAccess) -> None:
    now = utcnow()
    sub = await db.get_subscription(message.from_user.id)
    plan = plans.get(sub.plan_id) if sub else None
    text = texts.subscription_text(sub, plan, now, config.timezone)

    active = sub is not None and not sub.removed and plan is not None and \
        get_status(sub.expires_at, plan.grace, now) == Status.ACTIVE
    # Посилання — лише тим, у кого є доступ: людина загубила повідомлення після оплати.
    link = access.invite_link if has_access(sub, plan, now) else None
    await message.answer(text, reply_markup=choose_plan_keyboard(
        "🔄 Продовжити" if active else "💳 Обрати тариф", channel_link=link))
