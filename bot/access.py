"""
Доступ до каналу (крок 5): посилання із заявками, схвалення, «висячі» заявки.

Чому заявки, а не просто посилання-запрошення:
  звичайне посилання пускає БУДЬ-КОГО, хто його отримав, — одна людина заплатила,
  переслала друзям, і канал безкоштовний. Посилання з заявкою (creates_join_request)
  нікого не пускає саме: Telegram надсилає боту «Олена хоче вступити», і бот вирішує.
  Переслане посилання марне — бот перевіряє не посилання, а людину (її user_id і підписку).

Посилання ОДНЕ на весь канал. Бот створює його один раз і пам'ятає в базі (settings) —
інакше кожен перезапуск створював би нове, і в каналі накопичувались би десятки посилань.
"""
import logging
from dataclasses import dataclass
from enum import Enum

from aiogram import Bot
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest

from bot import db

log = logging.getLogger(__name__)

LINK_NAME = "Бот: доступ за підпискою"  # так посилання підписане в налаштуваннях каналу


@dataclass(frozen=True)
class ChannelAccess:
    channel_id: int
    invite_link: str


class Granted(Enum):
    APPROVED = "approved"  # була висяча заявка — бот її щойно схвалив
    MEMBER = "member"      # людина вже в каналі (продовження підписки)
    LINK = "link"          # ще не в каналі — даємо посилання, заявку схвалимо автоматично


async def ensure_invite_link(bot: Bot, channel_id: int) -> ChannelAccess:
    """Взяти посилання з бази або створити (один раз). Ключ — з ID каналу: якщо змінити
    CHANNEL_ID, старе посилання (від іншого каналу) не підхопиться."""
    key = f"invite_link:{channel_id}"
    link = await db.get_setting(key)
    if link is None:
        created = await bot.create_chat_invite_link(channel_id, name=LINK_NAME, creates_join_request=True)
        link = created.invite_link
        await db.set_setting(key, link)
        log.info("Створено посилання на канал із заявками: %s", link)
    return ChannelAccess(channel_id=channel_id, invite_link=link)


async def grant_after_payment(bot: Bot, access: ChannelAccess, user_id: int) -> Granted:
    """Після оплати: впустити, якщо людина вже стукала; інакше — сказати, як зайти."""
    if await db.pop_join_request(user_id):  # рівно один виклик забере заявку
        try:
            await bot.approve_chat_join_request(access.channel_id, user_id)
            log.info("JOIN | user=%s | висячу заявку схвалено після оплати", user_id)
            return Granted.APPROVED
        except TelegramBadRequest as e:
            # Людина встигла скасувати заявку або її вже обробили вручну — дамо посилання.
            log.info("JOIN | user=%s | висячої заявки вже немає (%s)", user_id, e.message)
    try:
        member = await bot.get_chat_member(access.channel_id, user_id)
        if member.status in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR,
                             ChatMemberStatus.CREATOR, ChatMemberStatus.RESTRICTED):
            return Granted.MEMBER
    except TelegramAPIError as e:
        log.warning("Не вдалося перевірити, чи user=%s у каналі: %s", user_id, e)
    return Granted.LINK


class Kick(Enum):
    KICKED = "kicked"          # був у каналі — прибрали
    NOT_MEMBER = "not_member"  # у каналі вже немає (вийшла сама) — робити нічого
    PROTECTED = "protected"    # власник або адмін каналу — не чіпаємо (і Telegram не дасть)


async def kick(bot: Bot, access: ChannelAccess, user_id: int) -> Kick:
    """Прибрати людину з каналу так, щоб вона МОГЛА повернутися після оплати.

    ban + ОДРАЗУ unban: просто ban тримає людину в «Видалених» назавжди, і вона не зайде навіть
    із підпискою. unban з only_if_banned=True — щоб випадково не вигнати учасника.
    Помилку Telegram (TelegramAPIError) НЕ ловимо: що з нею робити, вирішує той, хто викликав
    (планувальник — тихо спробує ще; адмін-команда — скаже людині в адмін-групі).
    Спільна для планувальника (крок 6) і /revoke (крок 7).
    """
    member = await bot.get_chat_member(access.channel_id, user_id)
    if member.status in (ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR):
        return Kick.PROTECTED
    if member.status in (ChatMemberStatus.MEMBER, ChatMemberStatus.RESTRICTED):
        await bot.ban_chat_member(access.channel_id, user_id)
        await bot.unban_chat_member(access.channel_id, user_id, only_if_banned=True)
        return Kick.KICKED
    return Kick.NOT_MEMBER
