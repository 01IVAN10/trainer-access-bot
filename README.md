# «Сила вдома» — бот платного доступу до закритого каналу (демо)

> 🚧 У розробці (етап 2). Повний опис — після кроку 10.

Бот продає доступ до приватного Telegram-каналу: тарифи → оплата (monobank, тестовий режим) →
доступ через заявку на вступ → нагадування про продовження → автоматичне видалення тих, хто не продовжив.

## Запуск локально

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # заповнити BOT_TOKEN і MONO_TOKEN
python -m tools.find_chats  # дізнатися CHANNEL_ID і ADMIN_CHAT_ID
python -m tools.check_setup # перевірити все
python -m bot.main
```

## Тести

```bash
pip install -r requirements-dev.txt
pytest
```

Логіка підписки (продовження, нагадування, пільговий період, видалення) і зарахування оплат
перевіряються без Telegram: час передається параметром, тож «місяць» проганяється за мілісекунди.
