# abel-monitor

Монитор новых поступлений на [abelbooks.ru](https://abelbooks.ru/). Каждые N минут
опрашивает магазин и шлёт карточку нового товара в закрытый Telegram-канал.

Источник — открытый WooCommerce Store API сайта
(`/wp-json/wc/store/v1/products?orderby=date`): один лёгкий JSON-запрос, надёжнее
парсинга HTML и бережно к сайту. **Зависимостей нет**, нужен только `python3`.

## Настройка (10 минут, один раз)

1. **Создать бота:** напиши [@BotFather](https://t.me/BotFather) → `/newbot` → получишь токен вида `123456:ABC...`.
2. **Создать закрытый канал** в Telegram (или взять существующий).
3. **Добавить бота в канал админом** с правом «Публикация сообщений».
4. **Узнать ID канала:**
   - опубликуй любой пост в канал;
   - открой в браузере `https://api.telegram.org/bot<ТОКЕН>/getUpdates`;
   - найди `"chat":{"id":-100...}` — это и есть `channel_id` (с минусом).
5. **Заполнить конфиг:** скопируй `config.example.json` → `config.json`, впиши `bot_token` и `channel_id`.

## Запуск

Проверить, что данные тянутся (ничего не шлёт, печатает последние товары):
```
python3 monitor.py --dry
```

Проверить доставку в канал (шлёт последний товар принудительно, разово):
```
python3 monitor.py --test
```

Первый боевой запуск — помечает текущие товары как «уже виденные», ничего не шлёт:
```
python3 monitor.py
```

Дальше каждый запуск шлёт только то, что появилось с прошлого раза.

## Автозапуск каждые 15 минут (macOS, launchd)

1. Пути в `com.user.abel-monitor.plist` уже под `~/Desktop/abel-monitor/` — поправь, если перенесёшь.
2. `cp com.user.abel-monitor.plist ~/Library/LaunchAgents/`
3. `launchctl load ~/Library/LaunchAgents/com.user.abel-monitor.plist`
4. Логи — `monitor.log` / `monitor.err.log` рядом со скриптом.
   Снять: `launchctl unload ~/Library/LaunchAgents/com.user.abel-monitor.plist`

Альтернатива — гонять демоном в терминале: `python3 monitor.py --loop`.

## Файлы
- `monitor.py` — скрипт
- `config.json` — токен и канал (в `.gitignore`, не коммитить)
- `seen_ids.json` — память о виденных товарах (создаётся сама)

## Заметки
- Интервал — `interval_seconds` (по умолчанию 900 = 15 мин). Магазин антикварный,
  новинок немного — чаще дёргать сайт незачем.
- За проход ловится до `per_page` (50) новинок. Больше за один интервал для
  антиквариата нереально.
