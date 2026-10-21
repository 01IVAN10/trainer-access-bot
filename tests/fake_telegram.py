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
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage, TelegramMethod
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from bot.config import Config

USER = User(id=111, is_bot=False, first_name="Олена", username="olena")
PRIVATE = Chat(id=111, type="private")
ADMIN_GROUP = Chat(id=-100222, type="supergroup", title="Адмінка")
_ids = count(1)


class FakeSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[TelegramMethod] = []

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, (SendMessage, EditMessageText)):
            # Telegram повертає надіслане повідомлення — повертаємо схоже.
            return Message(message_id=next(_ids), date=datetime.now(timezone.utc),
                           chat=Chat(id=method.chat_id or PRIVATE.id, type="private"),
                           text=method.text)
        return True  # answerCallbackQuery, setMyCommands тощо

    async def close(self) -> None:  # нема чого закривати
        pass

    async def stream_content(self, *args, **kwargs):  # pragma: no cover — не потрібне
        raise NotImplementedError

    # Зручності для перевірок
    def sent(self) -> list[SendMessage]:
        return [c for c in self.calls if isinstance(c, SendMessage)]

    def edited(self) -> list[EditMessageText]:
        return [c for c in self.calls if isinstance(c, EditMessageText)]

    def answers(self) -> list[AnswerCallbackQuery]:
        return [c for c in self.calls if isinstance(c, AnswerCallbackQuery)]


def make_config(demo_mode: bool = True) -> Config:
    return Config(bot_token="123:TEST", channel_id=-1001, admin_chat_id=-1002, mono_token="t",
                  support_contact="@support", demo_mode=demo_mode, timezone="Europe/Kyiv",
                  db_path=":memory:")


def make_bot() -> tuple[Bot, FakeSession]:
    session = FakeSession()
    return Bot("123:TEST", session=session), session


def text_update(text: str, chat: Chat = PRIVATE) -> Update:
    return Update(update_id=next(_ids), message=Message(
        message_id=next(_ids), date=datetime.now(timezone.utc), chat=chat, from_user=USER, text=text))


def callback_update(data: str) -> Update:
    msg = Message(message_id=next(_ids), date=datetime.now(timezone.utc), chat=PRIVATE,
                  from_user=User(id=999, is_bot=True, first_name="bot"), text="…")
    return Update(update_id=next(_ids), callback_query=CallbackQuery(
        id=str(next(_ids)), from_user=USER, chat_instance="ci", message=msg, data=data))


def buttons(method) -> list[str]:
    """Тексти inline-кнопок з відповіді бота."""
    markup = method.reply_markup
    return [b.text for row in getattr(markup, "inline_keyboard", []) for b in row]
