"""
Заявки на вступ у канал. Людина натиснула посилання-запрошення → Telegram НЕ пускає її
одразу, а надсилає боту подію chat_join_request. Бот вирішує:

  підписка активна (або пільговий період)  → схвалити + привітати
  підписки немає                           → НЕ відхиляти, а лишити заявку висіти
                                             і пояснити людині, як оформити доступ

Чому не відхиляти: людина вже зробила крок «хочу в канал». Якщо відхилити, після оплати
їй доведеться шукати посилання й подавати заявку вдруге. А так — оплатила, і бот сам
схвалить заявку, що висить (bot/access.py → grant_after_payment). Менше кроків — більше оплат.

Писати людині, яка ніколи не натискала /start, бот може ЛИШЕ у відповідь на заявку:
5 хвилин і лише ДОКИ заявку не оброблено. Після схвалення — вже ні.
Живий тест 23.10 це показав: звірка схвалила заявку Мами, а «оплату отримано» Telegram
не пропустив (Forbidden: bot can't initiate conversation). Кнопки під повідомленням
працюють (бот редагує СВОЄ повідомлення), а нові повідомлення — ні. Тому в повідомленні
про заявку — посилання в бот (t.me/бот?start=plans): людина натискає «Start», і далі
бот може писати їй будь-коли.
"""
import logging

from aiogram import Bot, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import ChatJoinRequest

from bot import db, texts
from bot.access import ChannelAccess
from bot.config import Config
from bot.keyboards import start_bot_keyboard
from bot.plans import Plan
from bot.subscription import has_access, utcnow

log = logging.getLogger(__name__)
router = Router(name="join")


@router.chat_join_request()
async def on_join_request(request: ChatJoinRequest, bot: Bot, config: Config,
                          plans: dict[str, Plan], access: ChannelAccess) -> None:
    if request.chat.id != access.channel_id:
        return  # бот може бути адміном і в інших чатах — це не наша справа
    user = request.from_user
    # Людина могла жодного разу не писати боту — але платіж (FK на users) їй ще знадобиться.
    await db.upsert_user(user.id, user.username, user.full_name)

    sub = await db.get_subscription(user.id)
    plan = plans.get(sub.plan_id) if sub else None
    if has_access(sub, plan, utcnow()):
        try:
            await request.approve()
        except TelegramBadRequest as e:
            # Напр., заявку вже схвалили вручну в каналі. Не страшно — людина в каналі.
            log.info("JOIN | user=%s | схвалити не вдалося: %s", user.id, e.message)
            return
        log.info("JOIN | user=%s | схвалено (підписка до %s)", user.id, sub.expires_at)
        await _say(bot, request.user_chat_id, texts.JOIN_APPROVED)
    else:
        await db.save_join_request(user.id, utcnow())
        log.info("JOIN | user=%s | немає підписки — заявка чекає оплати", user.id)
        # Кнопка — ПОСИЛАННЯ в бот, а не callback (див. start_bot_keyboard): людина має
        # натиснути «Start», інакше потім бот не зможе написати їй ні про оплату, ні нагадування.
        me = await bot.me()
        await _say(bot, request.user_chat_id, texts.join_needs_subscription(config.demo_mode),
                   reply_markup=start_bot_keyboard(me.username))


async def _say(bot: Bot, chat_id: int, text: str, reply_markup=None) -> None:
    try:
        await bot.send_message(chat_id, text, reply_markup=reply_markup)
    except TelegramAPIError as e:
        log.warning("JOIN | не вдалося написати %s: %s", chat_id, e)
