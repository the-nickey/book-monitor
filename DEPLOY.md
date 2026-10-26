# Деплой и координаты сервиса

**Прод крутится на VPS, НЕ на GitHub Actions.**

GitHub Actions пробовали (крон) — он дросселит расписание до ~1 прогона в 1–2 часа,
для нужной 5-минутной оперативности не годится. Переехали на VPS с `monitor.py --loop`.
Workflow в `.github/workflows/monitor.yml` оставлен только под ручной `workflow_dispatch`,
авто-расписание отключено. Репозиторий теперь — просто хранилище кода, деплой идёт с VPS.

## Координаты

- **Сервер:** VPS `193.124.114.2` (RuVDS, Ubuntu 24.04, 1 vCPU / 431 МБ, **РФ — Королёв,
  AS48347 Mediasoft**; проверено через ipinfo 29.07.2026 — вопреки прежней записи «зарубежная»).
  Root — по паролю (в личных заметках, не в git) + ssh-ключ `~/.ssh/id_rsa` мака (положен 29.07.2026).
- **Код на сервере:** `/root/book-monitor`.
- **Служба:** systemd `book-monitor`, запускает `python3 monitor.py --loop`.
  - логи: `journalctl -u book-monitor -f`
  - статус / рестарт: `systemctl status book-monitor` · `systemctl restart book-monitor`
- **Репо:** `github.com/the-nickey/book-monitor` (public, только хранение кода).
- **Telegram из РФ:** `api.telegram.org` заблокирован ТСПУ (дефолтный IP `149.154.166.110`
  дропается), но блок пер-IP и дырявый. Сейчас доставка идёт **напрямую через пин в
  `/etc/hosts`**: `149.154.167.220 api.telegram.org` (этот DC открыт; сделано 29.07.2026).
  До этого был платный HTTP-прокси (`telegram_proxy` в `config.json`) — умер ~22.07.2026
  с 407 (канал молчал неделю), из серверного конфига удалён (бэкап: `/root/config.backup.json`).
  Поле `telegram_proxy` кодом по-прежнему поддерживается — запасной путь, если пин отвалится.
  Сайты (abel/moscow/antique) с РФ-IP доступны напрямую.

## Как задеплоено (с нуля)

1. VPS с Ubuntu; поставить `git` (`python3` уже есть, зависимостей у кода нет). Зарубежная
   локация проще (Telegram напрямую, без обходов); на РФ-VPS нужен обход ТСПУ —
   пин в `/etc/hosts` на открытый TG-IP или живой `telegram_proxy` (см. «Координаты»).
2. `git clone https://github.com/the-nickey/book-monitor /root/book-monitor`
3. Положить `config.json` (`bot_token`, `channel_id`, `interval_seconds`; `telegram_proxy` — только если идём через прокси).
4. systemd-юнит `/etc/systemd/system/book-monitor.service`:
   ```ini
   [Unit]
   After=network-online.target
   Wants=network-online.target
   [Service]
   Type=simple
   Environment=PYTHONUNBUFFERED=1
   WorkingDirectory=/root/book-monitor
   ExecStart=/usr/bin/python3 /root/book-monitor/monitor.py --loop
   Restart=always
   RestartSec=15
   [Install]
   WantedBy=multi-user.target
   ```
   `PYTHONUNBUFFERED=1` обязателен — иначе `print` буферизуется и логи не видны в journald.
5. `systemctl daemon-reload && systemctl enable --now book-monitor`
6. Первый запуск сам делает первичный обстрел прайс-базы (`books.csv`) и seed состояния —
   молча, без спама.

## Апдейт кода

Локально: правки → `python3 -B -m unittest -v test_monitor` → commit/push.
На сервере:
```
cd /root/book-monitor
systemctl stop book-monitor
backup=/root/book-monitor-backup-$(date -u +%Y%m%dT%H%M%SZ)
mkdir -m 700 "$backup"
cp -a monitor.py state.json books.csv config.json "$backup"/
[ ! -f abel_state.json ] || cp -a abel_state.json "$backup"/
git fetch -q origin main && git merge --ff-only origin/main
systemctl start book-monitor
```
При ошибке обновления восстановить прежний код из backup и запустить службу.
`config.json`, `books.csv` и `abel_state.json` — рабочие данные вне Git.
`state.json` отслеживается Git, но обновление кода его не меняет.
Подмена данных: `systemctl stop` → копирование → `start` (служба иначе перезапишет их).

С 26.10 Абель обходит весь каталог примерно каждые 5 минут, два запроса параллельно.
Первый запуск без `abel_state.json` запоминает каталог молча; дальнейшие запуски
используют сохранённую историю и очередь доставки. Не удалять этот файл при
обновлении. Проверка снимка и ожидающих событий без отправки и записи:
`python3 monitor.py --abel-preview`.

## Восстановление, если сервер умрёт

Нужен свежий серверный бэкап: код + `config.json` + `books.csv` + `state.json` +
`abel_state.json`. Перенос локальных файлов от 25.10 не содержит новую историю.
Новый VPS (лучше **зарубежный** — TG без обходов) → скопировать папку целиком → тот же
юнит → `systemctl start`. Продолжит с места, без пересева и без спама «старыми» новинками.
