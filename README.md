# book-monitor

Монитор новых поступлений и продаж в антикварных книжных лавках → закрытый
Telegram-канал. Четыре источника (abelbooks — поступления и продажи, moscowbooks,
antiquebooks). Голый `python3`, зависимостей нет.

**Прод крутится 24/7 на VPS** — не на GitHub Actions и не на локальном launchd
(это ранние итерации, от них отказались).
Координаты сервера и деплой → **[DEPLOY.md](DEPLOY.md)**.
Полный контекст (архитектура, прайс-база, грабли, восстановление) → **[CONTEXT.md](CONTEXT.md)**.

## Локально погонять

```
python3 monitor.py --dry [источник]   # превью без отправки: abel_new | abel_sold | moscow | antique
python3 monitor.py --test             # по одной карточке из каждого источника в канал
python3 monitor.py --snapshot         # обстрел прайс-базы + замер RAM
python3 monitor.py --loop             # боевой цикл (так же запускается на сервере)
```
Нужен `config.json` (скопировать из `config.example.json`, вписать `bot_token`,
`channel_id`, `telegram_proxy`).

## Файлы
- `monitor.py` — весь код
- `config.json` — токен/канал/прокси (в `.gitignore`, не коммитить)
- `books.csv` — прайс-база для цен проданного (в `.gitignore`, ведётся на сервере)
- `state.json` — что уже отправлено
- `CONTEXT.md`, `DEPLOY.md` — контекст проекта и деплой
