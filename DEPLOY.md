# Деплой и координаты сервиса

**Прод крутится на VPS, НЕ на GitHub Actions.**

GitHub Actions пробовали (крон) — он дросселит расписание до ~1 прогона в 1–2 часа,
для нужной 5-минутной оперативности не годится. Переехали на VPS с `monitor.py --loop`.
Workflow в `.github/workflows/monitor.yml` оставлен только под ручной `workflow_dispatch`,
авто-расписание отключено. Репозиторий теперь — просто хранилище кода, деплой идёт с VPS.

## Координаты

- **Сервер:** VPS `193.124.114.2` (RuVDS, Ubuntu 24.04, 1 vCPU / 431 МБ, **зарубежная локация**).
  Root — по паролю (в личных заметках, не в git).
- **Код на сервере:** `/root/book-monitor`.
- **Служба:** systemd `book-monitor`, запускает `python3 monitor.py --loop`.
  - логи: `journalctl -u book-monitor -f`
  - статус / рестарт: `systemctl status book-monitor` · `systemctl restart book-monitor`
- **Репо:** `github.com/the-nickey/book-monitor` (public, только хранение кода).
- **Telegram** идёт через HTTP-прокси (`telegram_proxy` в `config.json`): в РФ 2026
  `api.telegram.org` заблокирован (ТСПУ). Поэтому локация сервера **обязана быть зарубежной**;
  сайты (abel/moscow/antique) с зарубежного IP доступны — проверено.

## Как задеплоено (с нуля)

1. Зарубежный VPS с Ubuntu; поставить `git` (`python3` уже есть, зависимостей у кода нет).
2. `git clone https://github.com/the-nickey/book-monitor /root/book-monitor`
3. Положить `config.json` (`bot_token`, `channel_id`, `telegram_proxy`, `interval_seconds`).
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

Локально: правки `monitor.py` → `git pull --rebase` → `git commit && git push`.
На сервере:
```
cd /root/book-monitor
cp -f state.json /root/state.backup.json
git fetch -q origin main && git reset --hard -q origin/main
cp -f /root/state.backup.json state.json
systemctl restart book-monitor
```
`config.json` и `books.csv` в `.gitignore` → `reset --hard` их не трогает.
Подмена самой `books.csv`: `systemctl stop` → scp → `start` (живая служба иначе перезапишет файл из памяти).

## Восстановление, если сервер умрёт

Локальная папка = полный бэкап (код + `config.json` + `books.csv` + `state.json`).
Новый **зарубежный** VPS → скопировать папку целиком → тот же юнит → `systemctl start`.
Продолжит с места, без пересева и без спама «старыми» новинками.
