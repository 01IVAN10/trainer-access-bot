"""
Фальшивий Telegram для тестів хендлерів.

Ідея: aiogram надсилає КОЖЕН запит до Telegram через «сесію» (session.make_request).
Підміняємо сесію на свою: вона нічого не відправляє в інтернет, а записує, що бот
хотів зробити («надіслати повідомлення з таким текстом і кнопками»), і повертає
правдоподібну відповідь. Хендлери при цьому — СПРАВЖНІ, разом з фільтрами, роутерами,
middleware і базою.

Так ми перевіряємо «що побачить людина» без телефона і без живого бота.
"""
from datetime import datetime, timezone
from itertools import count

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.methods import (
    AnswerCallbackQuery, ApproveChatJoinRequest, BanChatMember, CreateChatInviteLink, EditMessageText, GetChatMember,
    GetMe, SendMessage, TelegramMethod, UnbanChatMember,
)
from aiogram.types import (
    CallbackQuery, Chat, ChatInviteLink, ChatJoinRequest, ChatMemberBanned, ChatMemberLeft, ChatMemberMember,
    ChatMemberOwner, Message, Update, User,
)

from bot.access import ChannelAccess

from bot.config import Config

USER = User(id=111, is_bot=False, first_name="Олена", username="olena")
PRIVATE = Chat(id=111, type="private")
ADMIN_CHAT_ID = -1002                                           # той самий, що в make_config()
ADMIN_CHAT = Chat(id=ADMIN_CHAT_ID, type="supergroup", title="Адмінка")
ADMIN_USER = User(id=777, is_bot=False, first_name="Тренерка", username="trainer")
ADMIN_GROUP = Chat(id=-100222, type="supergroup", title="Чужа група")  # НЕ адмінка: бот має мовчати
CHANNEL = Chat(id=-1001, type="channel", title="Сила вдома")   # той самий ID, що в make_config()
INVITE_LINK = "https://t.me/+TestInviteLink"
ACCESS = ChannelAccess(channel_id=CHANNEL.id, invite_link=INVITE_LINK)
BOT_USER = User(id=999, is_bot=True, first_name="bot", username="test_bot")
_ids = count(1)


class FakeSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[TelegramMethod] = []
        self.in_channel: set[int] = set()   # хто «вже в каналі» (для getChatMember)
        self.approve_fails = False          # «заявку вже скасували» → approve падає
        self.cannot_message: set[int] = set()  # кому бот НЕ може писати (не натискав /start)
        self.owners: set[int] = set()          # власник каналу (Telegram не дасть його видалити)
        self.banned: set[int] = set()          # «Видалені» (чорний список каналу)
        self.ban_fails = False                 # Telegram тимчасово не відповідає
        self.on_ban = None                     # гачок: «щось сталося саме в мить бану» (гонки)
        self.explode_for: set[int] = set()     # несподівана помилка для цих людей (не TelegramAPIError)

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, SendMessage) and method.chat_id in self.cannot_message:
            raise TelegramForbiddenError(method=method, message="Forbidden: bot can't initiate conversation with a user")
        if isinstance(method, (SendMessage, EditMessageText)):
            # Telegram повертає надіслане повідомлення — повертаємо схоже.
            return Message(message_id=next(_ids), date=datetime.now(timezone.utc),
                           chat=Chat(id=method.chat_id or PRIVATE.id, type="private"),
                           text=method.text)
        if isinstance(method, GetMe):
            return BOT_USER
        if isinstance(method, CreateChatInviteLink):
            return ChatInviteLink(invite_link=INVITE_LINK, creator=BOT_USER, name=method.name,
                                  creates_join_request=bool(method.creates_join_request),
                                  is_primary=False, is_revoked=False)
        if isinstance(method, GetChatMember):
            if method.user_id in self.explode_for:
                raise RuntimeError("несподіванка")
            user = User(id=method.user_id, is_bot=False, first_name="x")
            if method.user_id in self.owners:
                return ChatMemberOwner(user=user, is_anonymous=False)
            if method.user_id in self.banned:
                return ChatMemberBanned(user=user, until_date=0)
            return ChatMemberMember(user=user) if method.user_id in self.in_channel else ChatMemberLeft(user=user)
        if isinstance(method, BanChatMember):
            if self.ban_fails:
                raise TelegramBadRequest(method=method, message="Bad Request: try again later")
            if method.user_id in self.owners:  # як справжній Telegram: власника не видалиш
                raise TelegramBadRequest(method=method, message="Bad Request: can't remove chat owner")
            if self.on_ban:
                await self.on_ban()
            self.in_channel.discard(method.user_id)
            self.banned.add(method.user_id)
            return True
        if isinstance(method, UnbanChatMember):
            if method.only_if_banned is not True:  # без цього unban вигнав би й учасника — бот так не робить
                raise AssertionError("unban_chat_member має бути з only_if_banned=True")
            self.banned.discard(method.user_id)
            return True
        if isinstance(method, ApproveChatJoinRequest):
            if self.approve_fails:
                raise TelegramBadRequest(method=method, message="Bad Request: HIDE_REQUESTER_MISSING")
            self.in_channel.add(method.user_id)
            return True
        return True  # answerCallbackQuery, setMyCommands тощо

    async def close(self) -> None:  # нема чого закривати
        pass

    async def stream_content(self, *args, **kwargs):  # pragma: no cover — не потрібне
        raise NotImplementedError

    # Зручності для перевірок.
    # sent() — що побачили КЛІЄНТИ (усе, крім адмін-групи); admin() — що побачила власниця.
    # Окремо, бо це різні читачі: тест «людині одне повідомлення» не має ламатися від того,
    # що бот ще й написав в адмінку (крок 7).
    def sent(self) -> list[SendMessage]:
        return [c for c in self.calls if isinstance(c, SendMessage) and c.chat_id != ADMIN_CHAT_ID]

    def admin(self) -> list[SendMessage]:
        return [c for c in self.calls if isinstance(c, SendMessage) and c.chat_id == ADMIN_CHAT_ID]

    def edited(self) -> list[EditMessageText]:
        return [c for c in self.calls if isinstance(c, EditMessageText)]

    def answers(self) -> list[AnswerCallbackQuery]:
        return [c for c in self.calls if isinstance(c, AnswerCallbackQuery)]

    def of(self, method_type) -> list:
        return [c for c in self.calls if isinstance(c, method_type)]


def make_config(demo_mode: bool = True) -> Config:
    return Config(bot_token="123:TEST", channel_id=-1001, admin_chat_id=ADMIN_CHAT_ID, mono_token="t",
                  support_contact="@support", demo_mode=demo_mode, timezone="Europe/Kyiv",
                  db_path=":memory:")


def make_bot() -> tuple[Bot, FakeSession]:
    session = FakeSession()
    return Bot("123:TEST", session=session), session


def text_update(text: str, chat: Chat = PRIVATE, user: User = USER) -> Update:
    return Update(update_id=next(_ids), message=Message(
        message_id=next(_ids), date=datetime.now(timezone.utc), chat=chat, from_user=user, text=text))


def callback_update(data: str, user: User = USER) -> Update:
    msg = Message(message_id=next(_ids), date=datetime.now(timezone.utc), chat=PRIVATE,
                  from_user=User(id=999, is_bot=True, first_name="bot"), text="…")
    return Update(update_id=next(_ids), callback_query=CallbackQuery(
        id=str(next(_ids)), from_user=user, chat_instance="ci", message=msg, data=data))


def join_request_update(user: User = USER, chat: Chat = CHANNEL) -> Update:
    """Людина натиснула посилання із заявкою → Telegram надсилає боту chat_join_request."""
    return Update(update_id=next(_ids), chat_join_request=ChatJoinRequest(
        chat=chat, from_user=user, user_chat_id=user.id, date=datetime.now(timezone.utc),
        invite_link=ChatInviteLink(invite_link=INVITE_LINK, creator=BOT_USER, creates_join_request=True,
                                   is_primary=False, is_revoked=False)))


def buttons(method) -> list[str]:
    """Тексти inline-кнопок з відповіді бота."""
    markup = method.reply_markup
    return [b.text for row in getattr(markup, "inline_keyboard", []) for b in row]


_DISPATCHER = None


def get_dispatcher():
    """ОДИН диспетчер на всі тести. Роутери в aiogram — одиночки: підключити роутер
    до другого диспетчера не можна (RuntimeError: Router is already attached).
    Конфіг, тарифи і банк тести передають у кожен feed_update окремо."""
    global _DISPATCHER
    if _DISPATCHER is None:
        from bot.app import build_dispatcher
        from bot.plans import load_plans
        from tests.fake_payments import FakeProvider
        _DISPATCHER = build_dispatcher(make_config(), load_plans(), FakeProvider(), flood_limit=1000)
        _DISPATCHER["access"] = ACCESS  # як main.py після створення посилання
    return _DISPATCHER
