#!/usr/bin/env python3
"""Монитор книжных новинок и продаж → закрытый Telegram-канал.

Источники (у каждого свой интервал опроса):
  abel_new   — новые поступления abelbooks.ru (в наличии)         — 5 мин
  abel_sold  — проданные на abelbooks.ru (diff «Нет в наличии»)   — 1 час
  (abelbooks с 24.09.2026 на Битриксе: парсим HTML каталога, сортировка new_desc —
   сначала книги в наличии по убыванию id, в хвосте — «Нет в наличии»)
  moscow     — букинист moscowbooks.ru по фильтру (год/неделя)    — 5 мин
  antique    — новые поступления antiquebooks.ru                  — 1 час

Прайс-база (books.csv): цена проданного обнуляется сайтом, поэтому запоминаем
цену книги, пока она в наличии; при продаже подставляем её + считаем, сколько
дней книга провисела (first_seen → sold_date).

К сайтам ходим напрямую под браузерным UA; в Telegram — через HTTP-прокси.
"""
import csv
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
from datetime import date
from pathlib import Path

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
HERE = Path(__file__).resolve().parent
STATE_FILE = HERE / "state.json"
CONFIG_FILE = HERE / "config.json"
BOOKS_FILE = HERE / "books.csv"
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


# ---------- источники: каждый возвращает список item ----------
# item = {id, title, price, url, tag, images:[url,...]}

def source_abel_new():
    """Новые сверху. Листаем дальше, пока вся страница незнакома прайс-базе (пачка загрузок)."""
    items, page = [], 1
    while page <= 5:
        cards, _ = abel_page(page)
        instock = [c for c in cards if not c["sold"]]
        items += [dict(c, tag="Абель – в продаже") for c in instock]
        if not instock or len(instock) < len(cards) or any(c["id"] in BOOKS for c in instock):
            break
        page += 1
        time.sleep(1)
    return items


def source_abel_sold():
    """Проданные — хвост каталога: идём с последней страницы назад, пока страница целиком «Нет в наличии»."""
    _, page = abel_page(1)
    items = []
    while page >= 1:
        cards, _ = abel_page(page)
        items += [dict(c, tag="Абель – продано") for c in cards if c["sold"]]
        if not cards or not all(c["sold"] for c in cards):
            break
        page -= 1
        time.sleep(0.5)
    return items


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
    "abel_new":  {"fn": source_abel_new,  "interval": 300},
    "abel_sold": {"fn": source_abel_sold, "interval": 3600},
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
    with open(BOOKS_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=BOOK_FIELDS)
        w.writeheader()
        w.writerows(BOOKS.values())


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


def notify(token, chat, item):
    text = format_message(item)
    images = list(item.get("images") or [])
    if item.get("cover_page"):  # в листинге превью 306px — крупную обложку берём из og:image товара
        try:
            m = re.search(r'og:image"\s+content="([^"]+)"', http_get(item["cover_page"]))
            if m:
                images.insert(0, m.group(1))
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
    tg_call(token, "sendMessage", {"chat_id": chat, "text": text, "parse_mode": "HTML",
                                   "disable_web_page_preview": "true"})


def check_source(name, fetch, token, chat, state):
    items = fetch()
    if name == "abel_new":
        record_instock(items)
        save_books()
    elif name == "abel_sold":
        enrich_sold(items)  # фиксирует делистинги и подставляет цену/дни
        save_books()
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
    STATE_FILE.write_text(json.dumps({k: sorted(v) for k, v in state.items()}, ensure_ascii=False), "utf-8")


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
    """Источники — по своим интервалам + джиттер. Прайс-база: первичный обстрел
    (если пустая) и снимок раз в сутки."""
    due = {name: 0.0 for name in SOURCES}
    snap_due = 0.0 if not BOOKS else time.time() + 86400
    while True:
        now = time.time()
        if now >= snap_due:
            try:
                n = refresh_books()
                print(f"[{stamp()}] [books] снимок цен: {n} в наличии, {len(BOOKS)} всего в базе")
            except Exception as e:
                print(f"[{stamp()}] [books] ошибка снимка: {e}", file=sys.stderr)
            snap_due = time.time() + 86400 * random.uniform(0.9, 1.1)
        for name, meta in SOURCES.items():
            if now < due[name]:
                continue
            try:
                n, first = check_source(name, meta["fn"], token, chat, state)
                print(f"[{stamp()}] [{name}] " + (f"seed {n} (без отправки)" if first else f"новых: {n}"))
            except Exception as e:
                print(f"[{stamp()}] [{name}] ошибка источника: {e}", file=sys.stderr)
            due[name] = time.time() + meta["interval"] * random.uniform(0.9, 1.1)
        time.sleep(random.uniform(45, 75))


def main():
    global TG_PROXY, BOOKS
    argv = sys.argv[1:]
    token, chat, proxy, intervals = load_config()
    TG_PROXY = proxy
    for name, sec in intervals.items():
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
        which = next((a for a in argv if a in SOURCES), None)
        for name in ([which] if which else list(SOURCES)):
            items = SOURCES[name]["fn"]()
            print(f"### {name}: {len(items)} позиций")
            for it in items[:5]:
                print(format_message(it) + "\n---")
        return

    if not token or not chat:
        sys.exit("Заполни bot_token и channel_id (config.json или env BOT_TOKEN/CHANNEL_ID)")

    if "--test" in argv:
        for name, meta in SOURCES.items():
            try:
                items = meta["fn"]()
                if items:
                    notify(token, chat, items[0])
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
