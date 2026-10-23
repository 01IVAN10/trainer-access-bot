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
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError

from bot import admin_texts, db, texts
from bot.access import ChannelAccess, Kick, kick
from bot.config import Config
from bot.keyboards import channel_keyboard, choose_plan_keyboard, renew_keyboard
from bot.notify import tell_admins
from bot.plans import Plan, available_plans
from bot.subscription import ActionKind, Subscription, decide, has_access, utcnow

log = logging.getLogger(__name__)

INTERVAL_SECONDS = 30
# Крок 8: скільки заявка в канал може «висіти» без оплати. Досить, щоб людина встигла
# дочекатися зарплати чи порадитися вдома; не так довго, щоб у каналі збиралися сотні заявок
# від тих, хто просто подивився.
JOIN_REQUEST_TTL = timedelta(hours=48)
# Тариф прибрали з plans.yaml, а підписка за нього ще є: без нагадувань і без grace.
_NO_PLAN = Plan(id="?", title="", description="", price_uah=1, duration=timedelta(days=1),
                reminders=(), grace=timedelta(0))


async def run_once(bot: Bot, plans: dict[str, Plan], config: Config, access: ChannelAccess,
                   now: datetime) -> dict[str, int]:
    """Один прохід. now — параметром (як у всій логіці): тести «перемотують» час."""
    horizon = max((r for p in plans.values() for r in p.reminders), default=timedelta(0))
    done = {"remind": 0, "expired": 0, "removed": 0, "declined": 0}
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
    try:
        done["declined"] = await _decline_stale_requests(bot, access, now)
    except Exception:
        log.exception("Планувальник: помилка під час відхилення старих заявок")
    return done


async def _decline_stale_requests(bot: Bot, access: ChannelAccess, now: datetime) -> int:
    """Заявки без оплати, старші за JOIN_REQUEST_TTL, — відхилити (крок 8).

    Порядок: СПЕРШУ відхилити в Telegram, ПОТІМ стерти в базі. Навпаки небезпечно: стерли,
    а Telegram не відповів — заявка висить у каналі, а бот про неї забув назавжди.
    Telegram каже «такої заявки немає» (людина скасувала, схвалили вручну, щойно оплатила й
    схвалив інший шлях) — це не помилка: мовчки прибираємо запис, людині нічого не пишемо.
    Гонка з оплатою безпечна: оплата забирає запис першою (pop) → approve впаде (заявку вже
    відхилено) → людина отримає кнопку «Вступити в канал» і подасть нову — її схвалять одразу.
    """
    declined = 0
    for user_id in await db.stale_join_requests(now - JOIN_REQUEST_TTL):
        try:
            await bot.decline_chat_join_request(access.channel_id, user_id)
        except TelegramBadRequest as e:
            # Заявки вже немає: скасувала сама, схвалили вручну або щойно — після оплати. Писати
            # «вашу заявку скасовано» НЕ можна: людина може бути вже в каналі. Мовчки прибираємо запис.
            log.info("JOIN | user=%s — заявки в Telegram уже немає (%s)", user_id, e.message)
            await db.pop_join_request(user_id)
            continue
        except TelegramAPIError as e:
            log.warning("JOIN | user=%s — не вдалося відхилити (%s), спробую пізніше", user_id, e)
            continue  # запис лишається → наступний прохід спробує ще
        if await db.pop_join_request(user_id):  # рівно один прохід (і не оплата) забрав запис
            declined += 1
            log.info("JOIN | user=%s — заявку без оплати відхилено через %s", user_id, JOIN_REQUEST_TTL)
            await _say(bot, user_id, texts.join_request_expired(JOIN_REQUEST_TTL), choose_plan_keyboard())
    return declined


def _renewable(plan: Plan | None, plans: dict[str, Plan], config: Config) -> Plan | None:
    """Тариф для кнопки «продовжити», якщо його зараз можна купити (демо — лише в DEMO_MODE)."""
    return plan if plan is not None and plan in available_plans(plans, config.demo_mode) else None


async def _remove(bot: Bot, sub: Subscription, plans: dict[str, Plan], access: ChannelAccess,
                  config: Config) -> bool:
    if not await db.claim_removal(sub.user_id, sub.expires_at):
        return False  # за цю мить людина продовжила підписку (або інший прохід уже видалив)

    try:
        result = await kick(bot, access, sub.user_id)  # ban + одразу unban — див. bot/access.py
    except TelegramAPIError as e:
        # Telegram не відповів / тимчасова помилка → знімаємо позначку, спробуємо через 30 с.
        log.warning("REMOVE | user=%s — не вдалося (%s), повторю пізніше", sub.user_id, e)
        await db.release_removal(sub.user_id, sub.expires_at)
        return False
    if result == Kick.PROTECTED:
        # Власника й адмінів не чіпаємо (і Telegram не дасть). Позначку лишаємо — щоб не пробувати щоразу.
        log.info("REMOVE | user=%s — адмін/власник каналу, не видаляємо", sub.user_id)
        return False
    log.info("REMOVE | user=%s — %s (підписка до %s)", sub.user_id,
             "видалено з каналу" if result == Kick.KICKED else "у каналі вже немає", sub.expires_at)

    # Гонка «оплатив саме в мить видалення»: оплата встигла між нашою заявкою і баном.
    fresh = await db.get_subscription(sub.user_id)
    if has_access(fresh, plans.get(fresh.plan_id) if fresh else None, utcnow()):
        log.warning("REMOVE | user=%s оплатив під час видалення — даємо посилання знову", sub.user_id)
        await _say(bot, sub.user_id, texts.PAID_DURING_REMOVAL, channel_keyboard(access.invite_link, "link"))
        return True
    await _say(bot, sub.user_id, texts.REMOVED_TEXT, choose_plan_keyboard())
    # Крок 7: власниці — хто не продовжив. Це не «шум»: тим, хто пішов, варто написати особисто.
    await tell_admins(bot, config, admin_texts.removed_text(
        await db.get_user(sub.user_id), sub, plans.get(sub.plan_id), result == Kick.KICKED, config.timezone))
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
                log.info("Планувальник: нагадувань %(remind)s, «закінчилась» %(expired)s, видалено %(removed)s, "
                         "старих заявок відхилено %(declined)s",
                         done)
        except Exception:
            log.exception("Планувальник: несподівана помилка")  # цикл не має померти
        await asyncio.sleep(INTERVAL_SECONDS)
