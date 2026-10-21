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

## Розробка

```bash
pip install -r requirements-dev.txt

# Бот з автоперезапуском: зберіг файл у bot/ або data/ → бот перезапустився сам
watchfiles "python -m bot.main" bot data

# Тести (саме python -m pytest: гарантовано pytest з .venv, а не глобальний)
python -m pytest

# Тести з автоперезапуском — зручно, коли пишеш логіку
watchfiles "python -m pytest -q" bot tests data
```

База лежить у `storage/`, а не в `data/`: інакше кожен запис у базу перезапускав би бота
(watchfiles стежить за `data/`). Зміни в `.env` не відстежуються — після них перезапусти вручну.

## Тести

Логіка підписки (продовження, нагадування, пільговий період, видалення) і зарахування оплат
перевіряються без Telegram: час передається параметром, тож «місяць» проганяється за мілісекунди.
