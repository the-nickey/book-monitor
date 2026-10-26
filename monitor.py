#!/usr/bin/env python3
"""Монитор книжных новинок и продаж → закрытый Telegram-канал.

Источники (у каждого свой интервал опроса):
   abel       — полный каталог abelbooks.ru, поступления/продажи  — 5 мин
  (abelbooks с 24.09.2026 на Битриксе: парсим HTML каталога, сортировка new_desc —
   сначала книги в наличии по убыванию id, в хвосте — «Нет в наличии»)
  moscow     — букинист moscowbooks.ru по фильтру (год/неделя)    — 5 мин
  antique    — новые поступления antiquebooks.ru                  — 1 час

Прайс-база (books.csv): цена проданного обнуляется сайтом, поэтому запоминаем
цену книги, пока она в наличии; при продаже подставляем её + считаем, сколько
    дней книга провисела. История повторных поступлений — abel_state.json.

 К сайтам ходим напрямую под браузерным UA; Telegram поддерживает опциональный прокси.
"""
import csv
import copy
import html
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from pathlib import Path

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
HERE = Path(__file__).resolve().parent
STATE_FILE = HERE / "state.json"
CONFIG_FILE = HERE / "config.json"
BOOKS_FILE = HERE / "books.csv"
ABEL_STATE_FILE = HERE / "abel_state.json"
BOOK_FIELDS = ["id", "title", "price", "first_seen", "status", "sold_date", "days_on_shelf"]
TG_PROXY = None
BOOKS = {}  # id -> запись прайс-базы (см. BOOK_FIELDS); наполняется из abel в наличии

ABEL_HOST = "https://abelbooks.ru"
ABEL_CATALOG = ABEL_HOST + "/kategoriya/?sort=new_desc"
MOSCOW_HOST = "https://www.moscowbooks.ru"
MOSCOW_URL = MOSCOW_HOST + "/bookinist/?yf=1940&date_in=week"
ANTIQUE_HOST = "https://antiquebooks.ru"
ANTIQUE_URL = ANTIQUE_HOST + "/category.php?choice=newbooks"


def stamp():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def plural_days(n):
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} день"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return f"{n} дня"
    return f"{n} дней"


def load_config():
    cfg = json.loads(CONFIG_FILE.read_text("utf-8")) if CONFIG_FILE.exists() else {}
    token = os.environ.get("BOT_TOKEN") or cfg.get("bot_token")
    chat = os.environ.get("CHANNEL_ID") or cfg.get("channel_id")
    proxy = os.environ.get("TELEGRAM_PROXY") or cfg.get("telegram_proxy")
    return token, chat, proxy, cfg.get("intervals") or {}


def http_get(url, binary=False, retries=2):
    last = None
    for _ in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=45) as r:
                return r.read() if binary else r.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            if e.code < 500:
                raise
            last = e  # Битрикс abelbooks изредка отдаёт 500 — повторим
            time.sleep(5)
        except OSError as e:  # таймаут/сетевой сбой — повторим
            last = e
            time.sleep(2)
    raise last


def abel_page(page):
    """Страница каталога → (карточки, номер последней страницы). Карточка — item + флаг sold."""
    doc = http_get(ABEL_CATALOG if page == 1 else f"{ABEL_CATALOG}&PAGEN_1={page}")
    last = max(map(int, re.findall(r'PAGEN_1=(\d+)', doc)), default=page)
    cards = []
    for b in re.split(r'<div class="catalog-card" ', doc)[1:]:
        m = re.search(r'data-id="(\d+)"', b)
        if not m:
            continue
        name = re.search(r'data-name="([^"]*)"', b)
        price = re.search(r'data-price="(\d+)', b)
        href = re.search(r'href="(/product/[^"]+)"', b)
        imgm = re.search(r'<img[^>]*\bsrc="(/upload/[^"]+)"', b)
        url = ABEL_HOST + href.group(1) if href else ABEL_HOST + "/"
        cards.append({
            "id": m.group(1),
            "title": html.unescape(name.group(1)) if name and name.group(1) else "Без названия",
            "price": f"{int(price.group(1)):,}".replace(",", " ") + " ₽" if price and int(price.group(1)) else None,
            "url": url,
            "cover_page": url if href else None,
            "images": [ABEL_HOST + imgm.group(1)] if imgm else [],
            "sold": "_not_avail\"" in b,
        })
    return cards, last


def abel_original_image(url):
    return re.sub(r'/upload/resize_cache/iblock/([^/]+)/([^/]+)/\d+_\d+_\d+/',
                  r'/upload/iblock/\1/\2/', url)


# ---------- источники: каждый возвращает список item ----------
# item = {id, title, price, url, tag, images:[url,...]}

def source_abel():
    """Полный каталог: старый ID при возврате не поднимается в начало списка.

    Два работника ограничивают RAM и нагрузку; неполный/сломанный ответ не
    превращается в наблюдение. Отсутствие ID само по себе не считается продажей.
    """
    first, last = abel_page(1)
    if not first or not 1 <= last <= 300:
        raise ValueError("Абель: пустая или некорректная первая страница")

    def fetch(page):
        time.sleep(0.5)
        cards, _ = abel_page(page)
        if not cards:
            raise ValueError(f"Абель: пустая страница {page}, снимок отменён")
        return cards

    cards = {c["id"]: c for c in first}
    with ThreadPoolExecutor(max_workers=2) as pool:
        for batch in pool.map(fetch, range(2, last + 1)):
            for card in batch:
                old = cards.get(card["id"])
                if old and old["sold"] != card["sold"]:
                    raise ValueError("Абель: наличие изменилось во время обхода, повторим снимок")
                cards[card["id"]] = card
    return list(cards.values())


def source_moscow():
    items, ids, page = [], set(), 1
    while page <= 30:
        url = MOSCOW_URL if page == 1 else f"{MOSCOW_URL}&page={page}"
        try:
            doc = http_get(url)
        except urllib.error.HTTPError:
            break
        fresh_on_page = 0
        for b in re.split(r'class="catalog__item', doc)[1:]:
            m = re.search(r'data-productid="(\d+)"', b)
            if not m or m.group(1) in ids:
                continue
            pid = m.group(1)
            ids.add(pid)
            fresh_on_page += 1
            title = re.search(r'book-preview__title-link"[^>]*>\s*([^<]+?)\s*<', b)
            href = re.search(r'href="(/bookinist/book/\d+/)"', b)
            price = re.search(r'book-preview__price">\s*([^<]+?)\s*<', b)
            imgm = re.search(r'<img[^>]*\bsrc="(/image/[^"]+)"', b)
            items.append({
                "id": pid,
                "title": html.unescape(title.group(1)) if title else "Без названия",
                "price": price.group(1).strip() if price else None,
                "url": MOSCOW_HOST + (href.group(1) if href else "/bookinist/"),
                "tag": "Moscowbooks – в продаже",
                "images": [MOSCOW_HOST + imgm.group(1)] if imgm else [],
            })
        if fresh_on_page == 0:
            break
        page += 1
        time.sleep(0.3)
    return items


def source_antique():
    items, ids = [], set()
    try:
        doc = http_get(ANTIQUE_URL)
    except urllib.error.HTTPError:
        return items
    for b in re.split(r'<tr><td class="style_main_table_td"', doc)[1:]:
        m = re.search(r'ch=book&book=(\d+)', b)
        if not m or m.group(1) in ids:
            continue
        bid = m.group(1)
        ids.add(bid)
        title = re.search(r'class=title_author[^>]*><b>\s*(.+?)\s*</b>', b, re.S)
        price_m = re.search(r'Цена:\s*([\d][\d.\s]*?)\s*руб', b)
        imgm = re.search(r'src="(/pic/[^"]+_preview\.jpg)"', b)
        price = None
        if price_m:
            digits = re.sub(r'\D', '', price_m.group(1))
            if digits:
                price = f"{int(digits):,}".replace(",", " ") + " ₽"
        items.append({
            "id": bid,
            "title": html.unescape(re.sub(r'<[^>]+>', '', title.group(1)).strip()) if title else "Без названия",
            "price": price,
            "url": f"{ANTIQUE_HOST}/category.php?ch=book&book={bid}",
            "tag": "AntiqueBooks – новое поступление",
            "images": [ANTIQUE_HOST + imgm.group(1)] if imgm else [],
        })
    return items


# имя → (функция, интервал опроса в секундах по умолчанию)
SOURCES = {
    "abel":      {"fn": source_abel,      "interval": 300},
    "moscow":    {"fn": source_moscow,    "interval": 300},
    "antique":   {"fn": source_antique,   "interval": 3600},
}


# ---------- прайс-база (books.csv) ----------

def load_books():
    if not BOOKS_FILE.exists():
        return {}
    with open(BOOKS_FILE, newline="", encoding="utf-8") as f:
        return {row["id"]: row for row in csv.DictReader(f)}


def save_books():
    tmp = BOOKS_FILE.with_suffix(".csv.tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=BOOK_FIELDS)
        w.writeheader()
        w.writerows(BOOKS.values())
    tmp.replace(BOOKS_FILE)


def record_instock(items):
    """Книги в наличии — запоминаем/обновляем цену и дату первой встречи."""
    today = date.today().isoformat()
    for it in items:
        rec = BOOKS.get(it["id"])
        if rec is None:
            BOOKS[it["id"]] = {"id": it["id"], "title": it["title"], "price": it["price"] or "",
                               "first_seen": today, "status": "instock", "sold_date": "", "days_on_shelf": ""}
        else:
            if it["price"]:
                rec["price"] = it["price"]
            rec["title"] = it["title"]
            if rec["status"] == "sold":  # снова в наличии — сняли пометку продажи
                rec.update(status="instock", sold_date="", days_on_shelf="")


def enrich_sold(items):
    """Проданные — фиксируем делистинг и обогащаем карточку ценой/днями из базы."""
    today = date.today().isoformat()
    for it in items:
        rec = BOOKS.get(it["id"])
        if not rec:
            continue  # историческая продажа (до запуска базы) — данных нет
        if rec["status"] != "sold":
            rec["status"] = "sold"
            rec["sold_date"] = today
            try:
                rec["days_on_shelf"] = str(max((date.fromisoformat(today) - date.fromisoformat(rec["first_seen"][:10])).days, 0))
            except ValueError:
                rec["days_on_shelf"] = ""
        if not it.get("price") and rec.get("price"):
            it["price"] = rec["price"]
        it["first_seen"] = rec.get("first_seen") or ""


def refresh_books():
    """Снимок всех книг в наличии → пополнение прайс-базы (первичный обстрел и раз в сутки)."""
    total, page = 0, 1
    while page <= 300:
        cards, last = abel_page(page)
        instock = [c for c in cards if not c["sold"]]
        record_instock(instock)
        total += len(instock)
        if len(instock) < len(cards) or page >= last:
            break
        page += 1
        time.sleep(0.5)
    save_books()
    return total


# ---------- Абель: состояния, история и очередь доставки ----------

def save_json(path, value):
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(path)


def load_abel_state():
    if not ABEL_STATE_FILE.exists():
        return None
    data = json.loads(ABEL_STATE_FILE.read_text("utf-8"))
    if (data.get("version") != 1 or not isinstance(data.get("items"), dict) or not data["items"]
            or not isinstance(data.get("pending"), list) or not isinstance(data.get("next_event"), int)):
        raise ValueError("Некорректный abel_state.json; автоматический пересев запрещён")
    return data


def observe_abel(items, previous, books, today=None, observed_at=None):
    """Чистая подготовка снимка/событий: без записи файлов и отправки сообщений."""
    today = today or date.today().isoformat()
    observed_at = observed_at or datetime.now(timezone.utc).isoformat()
    baseline = previous is None
    data = copy.deepcopy(previous) if previous is not None else {
        "version": 1, "items": {}, "pending": [], "next_event": 1,
    }
    if not items:
        raise ValueError("Абель: пустой снимок, состояние сохранено")
    for item in items:
        pid = item["id"]
        old = data["items"].get(pid)
        legacy = books.get(pid, {})
        status = "sold" if item["sold"] else "instock"
        returned = old is not None and old["status"] == "sold" and status == "instock"
        cycle = (old or {}).get("cycle", 0) + (1 if returned or (old is None and status == "instock") else 0)
        # Цена старого экземпляра не выдаётся за цену нового поступления.
        price = item.get("price")
        if not price and not returned:
            price = (old or {}).get("price") or (legacy.get("price") if baseline or old is None else None)
        cycle_started = (old or {}).get("cycle_started", "")
        if returned or (old is None and status == "instock"):
            cycle_started = (legacy.get("first_seen") or today) if baseline and legacy.get("status") == "instock" else today
        elif baseline and legacy.get("first_seen"):
            cycle_started = legacy["first_seen"]
        first_seen = (old or {}).get("first_seen") or legacy.get("first_seen") or (today if status == "instock" else "")
        current = {
            "status": status, "title": item["title"], "url": item["url"],
            "price": price or "", "images": [abel_original_image(url) for url in item.get("images") or []],
            "first_seen": first_seen, "cycle_started": cycle_started, "cycle": cycle,
            "observed_at": observed_at,
            "sold_date": today if status == "sold" and (old is None or old["status"] != "sold") else (old or {}).get("sold_date", ""),
            "history": list((old or {}).get("history", [])),
        }
        if not current["images"] and old and not returned:
            current["images"] = list(old["images"])
        if status == "instock":
            current["sold_date"] = ""
        elif baseline and legacy.get("sold_date"):
            current["sold_date"] = legacy["sold_date"]
        price_changed = bool(old and price and old["price"] != price)
        # Пропавшая картинка в одном ответе не считается заменой фотографии.
        photo_changed = bool(old and old["images"] and current["images"] and old["images"] != current["images"])
        changed = old is None or old["status"] != status or price_changed or photo_changed
        if changed:
            current["history"].append({
                "at": observed_at, "status": status, "price": current["price"],
                "images": current["images"], "cycle": cycle,
            })
        tag = None
        if not baseline:
            if returned:
                tag = "Абель – снова в продаже"
            elif status == "sold" and (old is None or old["status"] != "sold"):
                tag = "Абель – продано"
            elif status == "instock" and old is None:
                tag = "Абель – в продаже"
            elif status == "instock" and (price_changed or photo_changed):
                tag = "Абель – " + ("изменилась цена и фото" if price_changed and photo_changed else "изменилась цена" if price_changed else "обновлено фото")
        if tag:
            event = dict(item, tag=tag, price=price, images=current["images"], cover_page=item["url"],
                         first_seen=cycle_started if status == "sold" else "",
                         price_unknown="Цена не сохранена" if status == "sold" else "Цена не указана")
            if price_changed:
                event["previous_price"] = old["price"]
            data["pending"].append({"id": data["next_event"], "item": event, "observed_at": observed_at})
            data["next_event"] += 1
        data["items"][pid] = current
    data["updated_at"] = observed_at
    return data


def sync_abel_books(data):
    """Совместимая CSV-проекция; история циклов остаётся в abel_state.json."""
    for pid, item in data["items"].items():
        days = ""
        if item["status"] == "sold" and item["cycle_started"] and item["sold_date"]:
            days = str(max(0, (date.fromisoformat(item["sold_date"]) - date.fromisoformat(item["cycle_started"][:10])).days))
        BOOKS[pid] = {
            "id": pid, "title": item["title"], "price": item["price"],
            "first_seen": item["first_seen"], "status": item["status"],
            "sold_date": item["sold_date"], "days_on_shelf": days,
        }


def check_abel(fetch, token, chat, state):
    old = load_abel_state()
    data = observe_abel(fetch(), old, BOOKS)
    # Сначала сохраняем наблюдения и очередь, затем доставляем. Ошибка Telegram
    # и рестарт процесса не теряют событие и цену предыдущего цикла.
    save_json(ABEL_STATE_FILE, data)
    sync_abel_books(data)
    save_books()
    for name, status in [("abel_new", "instock"), ("abel_sold", "sold")]:
        state[name] = set(state.get(name, [])) | {pid for pid, it in data["items"].items() if it["status"] == status}
    save_state(state)
    sent = 0
    for event in list(data["pending"]):
        try:
            notify(token, chat, event["item"])
        except Exception as e:
            print(f"[abel] send error {event['item']['id']}: {e}", file=sys.stderr)
            break  # сохраняем порядок циклов при недоступном Telegram
        data["pending"].remove(event)
        save_json(ABEL_STATE_FILE, data)
        sent += 1
        time.sleep(1)
    print(f"[{stamp()}] [abel] карточек: {len(data['items'])}, в очереди: {len(data['pending'])}")
    return sent, old is None


# ---------- Telegram (через прокси) ----------

def _tg_open(req, timeout):
    if TG_PROXY:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": TG_PROXY, "https": TG_PROXY}))
        return opener.open(req, timeout=timeout)
    return urllib.request.urlopen(req, timeout=timeout)


def format_message(item):
    head = f'{item["tag"]} <a href="{item["url"]}">{html.escape(item["title"])}</a>'
    extras = []
    if item.get("price"):
        extras.append(html.escape(item["price"]))
    elif item.get("price_unknown"):
        extras.append(html.escape(item["price_unknown"]))
    if item.get("previous_price"):
        extras.append("раньше " + html.escape(item["previous_price"]))
    fs = item.get("first_seen")
    if fs:
        iso = fs[:10]  # обе формы (ISO и пометка «2026-07-04 – …») начинаются с даты
        try:
            n = (date.today() - date.fromisoformat(iso)).days
            ago = "сегодня" if n <= 0 else f"{plural_days(n)} назад"
            s = f"впервые заметили {ago} – {iso}"
            if "стали трекать" in fs:
                s += " (в этот день стали трекать историю)"
            extras.append(s)
        except ValueError:
            extras.append(f"впервые заметили {fs}")
    return head + ("\n" + " · ".join(extras) if extras else "")


def tg_call(token, method, data):
    body = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}", data=body)
    with _tg_open(req, 30) as r:
        return json.loads(r.read().decode("utf-8"))


def _image_type(data):
    if data[:3] == b"\xff\xd8\xff":
        return "jpg", "image/jpeg"
    if data[:4] == b"\x89PNG":
        return "png", "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":  # abelbooks отдаёт только webp
        return "webp", "image/webp"
    return None


def _send_photo(token, chat, caption, data):
    ext, mime = _image_type(data)
    b = "----book" + os.urandom(8).hex()
    def part(name, value):
        return (f'--{b}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n').encode("utf-8")
    body = part("chat_id", chat) + part("caption", caption[:1024]) + part("parse_mode", "HTML")
    body += (f'--{b}\r\nContent-Disposition: form-data; name="photo"; '
             f'filename="cover.{ext}"\r\nContent-Type: {mime}\r\n\r\n').encode("utf-8")
    body += data + b"\r\n" + f"--{b}--\r\n".encode("utf-8")
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendPhoto", data=body,
                                 headers={"Content-Type": f"multipart/form-data; boundary={b}"})
    with _tg_open(req, 60) as r:
        return json.loads(r.read().decode("utf-8"))


def abel_cover_images(doc):
    # og:image бывает устаревшей миниатюрой прежнего экземпляра. Битрикс PICT
    # содержит основную фотографию текущей галереи в исходном разрешении.
    images = []
    main = re.search(r"'PICT'\s*:\s*\{[^}]*'SRC'\s*:\s*'([^']+)'", doc)
    if main:
        images.append(urllib.parse.urljoin(ABEL_HOST, html.unescape(main.group(1))))
    og = re.search(r'og:image"\s+content="([^"]+)"', doc)
    if og:
        images.append(html.unescape(og.group(1)))
    return images


def notify(token, chat, item):
    text = format_message(item)
    images = list(item.get("images") or [])
    snapshot_images = [abel_original_image(url) for url in images
                       if "/upload/resize_cache/iblock/" in url or "/upload/iblock/" in url]
    if item.get("cover_page") and snapshot_images:
        # У очереди может быть несколько циклов одного ID. Берём фото именно
        # зафиксированного события, а не экземпляра, который сейчас на странице.
        images = list(dict.fromkeys(snapshot_images + images))
    elif item.get("cover_page"):
        try:
            images = abel_cover_images(http_get(item["cover_page"])) + images
        except Exception:
            pass
    for url in images:
        try:
            data = http_get(url, binary=True)
        except Exception:
            continue
        if not _image_type(data):  # не картинка (заглушка)
            continue
        try:
            if _send_photo(token, chat, text, data).get("ok"):
                return
        except urllib.error.HTTPError as e:
            print(f"sendPhoto {e.code} {url}", file=sys.stderr)
            continue  # размер/пропорции/формат не подошли — пробуем следующий
    response = tg_call(token, "sendMessage", {"chat_id": chat, "text": text, "parse_mode": "HTML",
                                             "disable_web_page_preview": "true"})
    if not response.get("ok"):
        raise RuntimeError("Telegram отклонил сообщение: " + str(response.get("description", "unknown error")))


def check_source(name, fetch, token, chat, state):
    if name == "abel":
        return check_abel(fetch, token, chat, state)
    items = fetch()
    first_run = name not in state
    seen = set(state.get(name, []))
    fresh = [it for it in items if it["id"] not in seen]
    for it in sorted(fresh, key=lambda x: int(x["id"])):
        if not first_run:
            try:
                notify(token, chat, it)
                time.sleep(1)
            except Exception as e:
                print(f"[{name}] send error {it['id']}: {e}", file=sys.stderr)
                continue
        seen.add(it["id"])
    state[name] = seen
    save_state(state)
    return len(fresh), first_run


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text("utf-8"))
    old = HERE / "seen_ids.json"
    if old.exists():
        return {"abel_new": [str(i) for i in json.loads(old.read_text("utf-8"))]}
    return {}


def save_state(state):
    save_json(STATE_FILE, {k: sorted(v) for k, v in state.items()})


def check_once(token, chat, state):
    total = 0
    for name, meta in SOURCES.items():
        try:
            n, first = check_source(name, meta["fn"], token, chat, state)
            print(f"[{name}] " + (f"seed {n} (без отправки)" if first else f"новых: {n}"))
            total += 0 if first else n
        except Exception as e:
            print(f"[{name}] ошибка источника: {e}", file=sys.stderr)
    return total


def run_loop(token, chat, state):
    """Полный снимок Абеля и остальные источники по своим интервалам."""
    due = {name: 0.0 for name in SOURCES}
    while True:
        for name, meta in SOURCES.items():
            if time.time() < due[name]:
                continue
            started = time.time()
            try:
                n, first = check_source(name, meta["fn"], token, chat, state)
                print(f"[{stamp()}] [{name}] " + (f"seed {n} (без отправки)" if first else f"новых: {n}"))
            except Exception as e:
                print(f"[{stamp()}] [{name}] ошибка источника: {e}", file=sys.stderr)
            due[name] = max(time.time() + 15, started + meta["interval"] * random.uniform(0.9, 1.1))
        time.sleep(random.uniform(15, 25))


def main():
    global TG_PROXY, BOOKS
    argv = sys.argv[1:]
    token, chat, proxy, intervals = load_config()
    TG_PROXY = proxy
    for name, sec in intervals.items():
        if name == "abel_new":
            name = "abel"
        elif name == "abel_sold":
            continue  # поступления и продажи теперь используют один снимок
        if name in SOURCES:
            SOURCES[name]["interval"] = int(sec)
    BOOKS = load_books()

    if "--snapshot" in argv:  # первичный обстрел прайс-базы + замер пика RAM
        import resource
        n = refresh_books()
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        mb = rss / 1048576 if sys.platform == "darwin" else rss / 1024  # mac: байты, linux: КБ
        print(f"снимок: {n} в наличии, {len(BOOKS)} в базе | пик RAM ~{mb:.0f} МБ")
        return

    if "--dry" in argv:
        which = next((a for a in argv if a in SOURCES or a in ("abel_new", "abel_sold")), None)
        for name in ([which] if which else list(SOURCES)):
            if name in ("abel", "abel_new", "abel_sold"):
                items = source_abel()
                items = [dict(it, tag="Абель – продано" if it["sold"] else "Абель – в продаже",
                              price_unknown="Цена не сохранена" if it["sold"] else "Цена не указана") for it in items
                         if name == "abel" or it["sold"] == (name == "abel_sold")]
                enrich_sold([it for it in items if it["sold"]])
            else:
                items = SOURCES[name]["fn"]()
            print(f"### {name}: {len(items)} позиций")
            for it in items[:5]:
                print(format_message(it) + "\n---")
        return

    if "--abel-preview" in argv:
        previous = load_abel_state()
        data = observe_abel(source_abel(), previous, BOOKS)
        print(f"Абель: {len(data['items'])} карточек, baseline={previous is None}, очередь={len(data['pending'])}")
        for event in data["pending"]:
            print(format_message(event["item"]))
        return

    if not token or not chat:
        sys.exit("Заполни bot_token и channel_id (config.json или env BOT_TOKEN/CHANNEL_ID)")

    if "--test" in argv:
        for name, meta in SOURCES.items():
            try:
                items = meta["fn"]()
                if items:
                    item = items[0]
                    if name == "abel":
                        item = dict(item, tag="Абель – продано" if item["sold"] else "Абель – в продаже",
                                    cover_page=item["url"], price_unknown="Цена не сохранена" if item["sold"] else "Цена не указана")
                        if item["sold"]:
                            enrich_sold([item])
                    notify(token, chat, item)
                    time.sleep(1)
                    print(f"[{name}] тест отправлен: {items[0]['title'][:50]}")
            except Exception as e:
                print(f"[{name}] ошибка теста: {e}", file=sys.stderr)
        return

    state = load_state()
    if "--loop" in argv:
        run_loop(token, chat, state)
    else:
        n = check_once(token, chat, state)
        print(f"[{stamp()}] итого новых: {n}")


if __name__ == "__main__":
    main()
