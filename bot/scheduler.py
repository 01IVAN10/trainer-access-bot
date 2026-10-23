"""
Планувальник (крок 6): раз на 30 секунд — по всіх підписках, що скоро закінчуються.

«Мозок» написано ще на кроці 1: subscription.decide() каже, ЩО робити (нагадати /
«закінчилась» / видалити). Тут — «руки»: виконати це в Telegram і записати в базу.
Вирішувати й виконувати — окремо: рішення протестовані без Telegram, а тут лише дії.

Головна складність — ГОНКИ. Людина може оплатити саме тієї секунди, коли планувальник
вирішив її видалити. Тому кожна дія: спершу «заявка» в базі (claim_* з умовою на
expires_at, який ми бачили), і лише якщо заявка пройшла — дія. Продовжила за цю мить —
expires_at уже інший, заявка не пройде, бот нічого не зробить.

Порядок для повідомлень і для видалення — різний, і це свідомо:
  нагадування: спершу позначка, потім повідомлення. Впали посередині → одне нагадування
               загубиться (краще, ніж слати двічі щоразу після збою);
  видалення:   спершу позначка, потім дія; дія не вдалась → позначку ЗНІМАЄМО, наступний
               прохід спробує ще. Видалити — обов'язково: це те, за що платять інші.
"""
import asyncio
import logging
from datetime import datetime, timedelta

from aiogram import Bot
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError

from bot import db, texts
from bot.access import ChannelAccess
from bot.config import Config
from bot.keyboards import channel_keyboard, choose_plan_keyboard, renew_keyboard
from bot.plans import Plan, available_plans
from bot.subscription import ActionKind, Subscription, decide, has_access, utcnow

log = logging.getLogger(__name__)

INTERVAL_SECONDS = 30
# Тариф прибрали з plans.yaml, а підписка за нього ще є: без нагадувань і без grace.
_NO_PLAN = Plan(id="?", title="", description="", price_uah=1, duration=timedelta(days=1),
                reminders=(), grace=timedelta(0))


async def run_once(bot: Bot, plans: dict[str, Plan], config: Config, access: ChannelAccess,
                   now: datetime) -> dict[str, int]:
    """Один прохід. now — параметром (як у всій логіці): тести «перемотують» час."""
    horizon = max((r for p in plans.values() for r in p.reminders), default=timedelta(0))
    done = {"remind": 0, "expired": 0, "removed": 0}
    for sub in await db.subscriptions_due(now, horizon):
        plan = plans.get(sub.plan_id)
        action = decide(sub, plan or _NO_PLAN, now)
        try:
            if action.kind == ActionKind.REMIND:
                if await db.claim_reminder(sub.user_id, sub.expires_at, action.reminders_sent):
                    await _say(bot, sub.user_id, texts.reminder_text(plan, sub, now, config.timezone),
                               renew_keyboard(_renewable(plan, plans, config)))
                    done["remind"] += 1
            elif action.kind == ActionKind.NOTIFY_EXPIRED:
                if await db.claim_expiry_notice(sub.user_id, sub.expires_at):
                    await _say(bot, sub.user_id, texts.expired_grace_text(plan, sub, now, config.timezone),
                               renew_keyboard(_renewable(plan, plans, config)))
                    done["expired"] += 1
            elif action.kind == ActionKind.REMOVE:
                if await _remove(bot, sub, plans, access, config):
                    done["removed"] += 1
        except Exception:
            # Одна «погана» підписка не має зупиняти обробку решти.
            log.exception("Планувальник: помилка для user=%s", sub.user_id)
    return done


def _renewable(plan: Plan | None, plans: dict[str, Plan], config: Config) -> Plan | None:
    """Тариф для кнопки «продовжити», якщо його зараз можна купити (демо — лише в DEMO_MODE)."""
    return plan if plan is not None and plan in available_plans(plans, config.demo_mode) else None


async def _remove(bot: Bot, sub: Subscription, plans: dict[str, Plan], access: ChannelAccess,
                  config: Config) -> bool:
    if not await db.claim_removal(sub.user_id, sub.expires_at):
        return False  # за цю мить людина продовжила підписку (або інший прохід уже видалив)

    try:
        member = await bot.get_chat_member(access.channel_id, sub.user_id)
        if member.status in (ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR):
            # Власника й адмінів не чіпаємо (і Telegram не дасть). Позначку лишаємо — щоб не пробувати щоразу.
            log.info("REMOVE | user=%s — адмін/власник каналу, не видаляємо", sub.user_id)
            return False
        if member.status in (ChatMemberStatus.MEMBER, ChatMemberStatus.RESTRICTED):
            # ban + ОДРАЗУ unban = «вигнати без вічного бану». Просто ban — і людина не
            # зможе повернутися навіть після оплати (Telegram тримає її в «Видалених»).
            await bot.ban_chat_member(access.channel_id, sub.user_id)
            await bot.unban_chat_member(access.channel_id, sub.user_id, only_if_banned=True)
            log.info("REMOVE | user=%s — видалено з каналу (підписка до %s)", sub.user_id, sub.expires_at)
        else:
            log.info("REMOVE | user=%s — у каналі вже немає (%s)", sub.user_id, member.status)
    except TelegramAPIError as e:
        # Telegram не відповів / тимчасова помилка → знімаємо позначку, спробуємо через 30 с.
        log.warning("REMOVE | user=%s — не вдалося (%s), повторю пізніше", sub.user_id, e)
        await db.release_removal(sub.user_id, sub.expires_at)
        return False

    # Гонка «оплатив саме в мить видалення»: оплата встигла між нашою заявкою і баном.
    fresh = await db.get_subscription(sub.user_id)
    if has_access(fresh, plans.get(fresh.plan_id) if fresh else None, utcnow()):
        log.warning("REMOVE | user=%s оплатив під час видалення — даємо посилання знову", sub.user_id)
        await _say(bot, sub.user_id, texts.PAID_DURING_REMOVAL, channel_keyboard(access.invite_link, "link"))
        return True
    await _say(bot, sub.user_id, texts.REMOVED_TEXT, choose_plan_keyboard())
    return True


async def _say(bot: Bot, user_id: int, text: str, reply_markup=None) -> None:
    try:
        await bot.send_message(user_id, text, reply_markup=reply_markup)
    except TelegramForbiddenError:
        log.info("Не вдалося написати user=%s: заблокував бота або не натискав Start", user_id)
    except TelegramAPIError as e:
        log.warning("Не вдалося написати user=%s: %s", user_id, e)


async def run_forever(bot: Bot, plans: dict[str, Plan], config: Config, access: ChannelAccess) -> None:
    while True:
        try:
            done = await run_once(bot, plans, config, access, utcnow())
            if any(done.values()):
                log.info("Планувальник: нагадувань %(remind)s, «закінчилась» %(expired)s, видалено %(removed)s",
                         done)
        except Exception:
            log.exception("Планувальник: несподівана помилка")  # цикл не має померти
        await asyncio.sleep(INTERVAL_SECONDS)
