# book-monitor — контекст проекта

Монитор новых поступлений и продаж в антикварных книжных лавках → закрытый
Telegram-канал. Прод крутится 24/7 на VPS. Этот файл — полный контекст, чтобы
поднять/продолжить проект в любой сессии без опоры на память ассистента.

## Что делает

Четыре источника → один TG-канал, карточка на каждое новое событие:

| источник | что ловит | интервал |
|----------|-----------|----------|
| `abel_new`  | новые поступления abelbooks.ru (в наличии) | 5 мин |
| `abel_sold` | проданное на abelbooks (исчезло из наличия) | 1 час |
| `moscow`    | букинист moscowbooks.ru (фильтр год≥1940, за неделю) | 5 мин |
| `antique`   | новые поступления antiquebooks.ru | 1 час |

Формат карточки: `Источник – статус` + кликабельное название + цена. У проданных
дополнительно: `впервые заметили N дней назад – ДАТА` (+ «(в этот день стали
трекать историю)» для книг, замеченных только при первичном обстреле).

## Прод (сервер)

- **VPS 193.124.114.2** (RuVDS, Ubuntu 24.04, 1 vCPU / 431 МБ RAM). Root — по
  паролю, хранится в личных заметках, **не здесь и не в git**.
- systemd-служба `book-monitor` в `/root/book-monitor`, запускает `monitor.py --loop`.
  Логи: `journalctl -u book-monitor -f`. Управление: `systemctl restart|status book-monitor`.
- **Локация обязательно зарубежная**: в РФ 2026 `api.telegram.org` заблокирован
  (ТСПУ, таймаут с российских серверов). Сайты (abel/moscow/antique) с зарубежного
  IP доступны — проверено. РФ-IP проекту не нужен.
- Telegram идёт через **HTTP-прокси** (поле `telegram_proxy` в config.json), сайты —
  напрямую под браузерным UA. Прокси-креды — в config.json.

## Код (monitor.py — голый stdlib, zero-deps)

- Источники — функции `source_*()`, реестр `SOURCES = {имя: {fn, interval}}`.
  **Добавить сайт = одна функция + одна строка в SOURCES** (сначала разведать HTML/API).
- Детект новизны: множества id в `state.json` = `{источник: [id]}`. Первый запуск
  каждого источника — seed без отправки (без спама).
- Планировщик `run_loop`: каждый источник по своему интервалу + джиттер ±10%,
  базовый тик 45–75с (убрать ровный пульс в логах сайтов).
- **Прайс-база `books.csv`**: сайт обнуляет цену проданного → запоминаем цену, пока
  книга в наличии, подставляем при продаже + считаем days-on-shelf. Наполнение:
  снимок всех in-stock раз в сутки (`refresh_books`, без парсинга картинок → пик
  RAM ~40 МБ) + поток `abel_new`. Колонки: id,title,price,first_seen,status,sold_date,days_on_shelf.
- Картинки: Telegram сам скачать не может (сайт отдаёт его UA заглушку) → качаем
  сами, шлём multipart-загрузкой, размер из srcset ≤1280px; фолбэк — текст.
- Режимы: `--loop` (прод), `--dry [source]` (превью без отправки), `--test` (по 1
  карточке из каждого источника), `--snapshot` (обстрел прайс-базы + замер RAM).

## Ключевые находки (почему так)

- **abelbooks** — WooCommerce, открытый Store API `/wp-json/wc/store/v1/products`
  (id, name, prices, permalink, images, stock_status). Проданные — `stock_status=outofstock`,
  сортировка по дате *добавления* (не продажи) → детект по diff всего набора.
- **moscowbooks** — старый ASP, HTML-парсинг, id в `data-productid`, пагинация `&page=N`.
- **antiquebooks** — .php, HTML, UTF-8, id в `book=N`, цена `Цена: 200.000 руб.`.

## Файлы

- `monitor.py`, `.github/workflows/monitor.yml` — код (в git; GitHub the-nickey/book-monitor, public)
- `config.json` — токен + channel_id + прокси + интервалы (**в .gitignore, не в git**)
- `books.csv` — прайс-база ~2856 книг (в .gitignore, ведётся на сервере)
- `state.json` — что уже отправлено (в git как baseline)
- `com.user.abel-monitor.plist` — локальный launchd, в проде НЕ используется (мёртвый, со старым именем)

## Деплой / апдейт

1. Правки `monitor.py` локально → `git commit && git push` (сначала `git pull --rebase`).
2. На сервере:
   ```
   cd /root/book-monitor
   cp -f state.json /root/state.backup.json
   git fetch -q origin main && git reset --hard -q origin/main
   cp -f /root/state.backup.json state.json
   systemctl restart book-monitor
   ```
   (state.json tracked → сохраняем; books.csv/config.json untracked → reset их не трогает.)
- Подмена самой books.csv: `systemctl stop` → scp файла → `systemctl start` (живая
  служба иначе перезапишет файл из BOOKS в памяти).

## Восстановление, если сервер умрёт

Локальная папка = полный бэкап (код + config.json + books.csv + state.json). Поднять
новый **зарубежный** VPS, скопировать папку целиком, поставить тот же systemd-юнит,
`systemctl start`. Продолжит с места — без пересева и без спама «старыми» новинками.

## Грабли сервера

- `/usr/bin/time` не установлен (минимальный Ubuntu) — RAM мерить внутренним `resource` (`--snapshot`).
- RuVDS периодически флапает DNS (`Errno -3 name resolution`) — `http_get` retry(2) + пропуск цикла ловят.

## История / оговорки

- GitHub Actions (крон) пробовали — дросселит до ~1 прогона в 1–2 часа, для 5-минутной
  оперативности не годится → переехали на VPS `--loop`. GitHub-крон отключён (остался `workflow_dispatch`).
- `first_seen`: при первичном обстреле 04.07 у всех = дата обстрела. Обогащено из
  экспорта TG-канала: 36 книг получили реальную дату первого появления, 2820 помечены
  `2026-07-04 – стали трекать историю`. days-on-shelf по обогащённым считается от реальной даты.
- Продажи фиксирует только abelbooks. moscow/antique выбытие не помечают — там ловим только поступления.
- Разовая аналитика по каналу (экспорт 22.06–04.07): дашборд `book-analytics.html`.
