# Деплой на GitHub Actions (бесплатно, 24/7)

`monitor.py` гоняется раннером GitHub каждые 15 минут. Токен — в Secrets, состояние
(`seen_ids.json`) — в репозитории. Фото работает (картинку качаем сами, не Telegram).

## 1. Создать репозиторий

Проще всего — поставить GitHub CLI и дать мне доделать:
```
brew install gh
gh auth login        # GitHub.com → HTTPS → войти через браузер
```
После этого скажи «gh готов» — я сам создам репо, запушу и заведу секреты.

Или вручную: создай **public** репозиторий на github.com (напр. `abel-monitor`) и запушь:
```
git remote add origin https://github.com/<логин>/abel-monitor.git
git push -u origin main
```
Public — потому что у Actions неограниченные бесплатные минуты, а код не секретный
(токен лежит в Secrets, не в коде).

## 2. Добавить секреты

Repo → **Settings → Secrets and variables → Actions → New repository secret**:
- `BOT_TOKEN` — токен бота
- `CHANNEL_ID` — id канала (`-100…`)

Локальный `config.json` в репо не попадает (он в `.gitignore`).

## 3. Запустить

Repo → **Actions** → workflow «abel-monitor» → **Run workflow** (ручная проверка).
Дальше крутится сам каждые 15 минут.

## Как хранится состояние

`seen_ids.json` лежит в репо как стартовое (твои текущие 50 id). После каждого прогона
workflow коммитит его обновление — так эфемерный раннер «помнит» виденное между запусками.
Эти же коммиты держат расписание активным (иначе GitHub усыпляет cron после 60 дней тишины).

## Минуты

Public-репо — бесплатно без лимита. Если сделаешь private, поставь в `monitor.yml`
`cron: '*/30 * * * *'` (лимит 2000 мин/мес).
