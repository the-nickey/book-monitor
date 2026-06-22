#!/usr/bin/env python3
"""Монитор книжных новинок и продаж → закрытый Telegram-канал.

Источники (у каждого свой интервал опроса):
  abel_new   — новые поступления abelbooks.ru (в наличии)         — 5 мин
  abel_sold  — проданные на abelbooks.ru (diff набора проданных)  — 1 час
  moscow     — букинист moscowbooks.ru по фильтру (год/неделя)    — 5 мин

К сайтам ходим напрямую под браузерным UA; в Telegram — через HTTP-прокси
(TG_PROXY), т.к. на российских серверах api.telegram.org заблокирован.
"""
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
from pathlib import Path

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
HERE = Path(__file__).resolve().parent
STATE_FILE = HERE / "state.json"
CONFIG_FILE = HERE / "config.json"
TG_PROXY = None  # прокси только для api.telegram.org (сайты — напрямую); ставится из конфига

ABEL_API = "https://abelbooks.ru/wp-json/wc/store/v1/products"
MOSCOW_HOST = "https://www.moscowbooks.ru"
MOSCOW_URL = MOSCOW_HOST + "/bookinist/?yf=1940&date_in=week"


def stamp():
    return time.strftime("%Y-%m-%d %H:%M:%S")


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
        except urllib.error.HTTPError:
            raise
        except OSError as e:  # таймаут/сетевой сбой — повторим
            last = e
            time.sleep(2)
    raise last


def abel_api(params):
    return json.loads(http_get(f"{ABEL_API}?{urllib.parse.urlencode(params)}"))


# ---------- источники: каждый возвращает список item ----------
# item = {id, title, price, url, tag, images:[url,...]}

def _abel_images(images):
    if not images:
        return []
    img = images[0]
    sized = []
    for tok in (img.get("srcset") or "").split(","):
        u, _, w = tok.strip().rpartition(" ")
        if w.endswith("w") and w[:-1].isdigit():
            sized.append((int(w[:-1]), u))
    order = [u for _, u in sorted((c for c in sized if c[0] <= 1280), key=lambda x: -x[0])]
    for extra in (img.get("thumbnail"), img.get("src")):
        if extra and extra not in order:
            order.append(extra)
    return order


def _abel_item(p, tag):
    prices = p.get("prices") or {}
    price = prices.get("price")
    minor = prices.get("currency_minor_unit", 0) or 0
    price_str = None
    if price not in (None, ""):
        val = int(price) / (10 ** minor) if minor else int(price)
        if val:  # у проданных цена обнулена в 0 — не показываем
            price_str = f"{val:,.0f}".replace(",", " ") + f" {prices.get('currency_symbol', '₽')}"
    return {
        "id": str(p["id"]),
        "title": html.unescape(p.get("name") or "Без названия"),
        "price": price_str,
        "url": p.get("permalink") or "https://abelbooks.ru/",
        "tag": tag,
        "images": _abel_images(p.get("images") or []),
    }


def source_abel_new():
    products = abel_api({"orderby": "date", "order": "desc", "per_page": 50, "stock_status": "instock"})
    return [_abel_item(p, "Абель – в продаже") for p in products]


def source_abel_sold():
    items, page = [], 1
    while page <= 30:
        batch = abel_api({"orderby": "date", "order": "desc", "per_page": 100,
                          "stock_status": "outofstock", "page": page})
        if not batch:
            break
        items += [_abel_item(p, "Абель – продано") for p in batch]
        if len(batch) < 100:
            break
        page += 1
        time.sleep(0.3)
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


# имя → (функция, интервал опроса в секундах по умолчанию)
SOURCES = {
    "abel_new":  {"fn": source_abel_new,  "interval": 300},
    "abel_sold": {"fn": source_abel_sold, "interval": 3600},
    "moscow":    {"fn": source_moscow,    "interval": 300},
}


# ---------- состояние ----------

def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text("utf-8"))
    old = HERE / "seen_ids.json"  # миграция со старой одно-источниковой версии
    if old.exists():
        return {"abel_new": [str(i) for i in json.loads(old.read_text("utf-8"))]}
    return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps({k: sorted(v) for k, v in state.items()}, ensure_ascii=False), "utf-8")


# ---------- Telegram (через прокси) ----------

def _tg_open(req, timeout):
    if TG_PROXY:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": TG_PROXY, "https": TG_PROXY}))
        return opener.open(req, timeout=timeout)
    return urllib.request.urlopen(req, timeout=timeout)


def format_message(item):
    head = f'{item["tag"]} <a href="{item["url"]}">{html.escape(item["title"])}</a>'
    return head + (f'\n{html.escape(item["price"])}' if item.get("price") else "")


def tg_call(token, method, data):
    body = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}", data=body)
    with _tg_open(req, 30) as r:
        return json.loads(r.read().decode("utf-8"))


def _send_photo(token, chat, caption, data):
    b = "----book" + os.urandom(8).hex()
    def part(name, value):
        return (f'--{b}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n').encode("utf-8")
    body = part("chat_id", chat) + part("caption", caption[:1024]) + part("parse_mode", "HTML")
    body += (f'--{b}\r\nContent-Disposition: form-data; name="photo"; '
             f'filename="cover.jpg"\r\nContent-Type: image/jpeg\r\n\r\n').encode("utf-8")
    body += data + b"\r\n" + f"--{b}--\r\n".encode("utf-8")
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendPhoto", data=body,
                                 headers={"Content-Type": f"multipart/form-data; boundary={b}"})
    with _tg_open(req, 60) as r:
        return json.loads(r.read().decode("utf-8"))


def notify(token, chat, item):
    text = format_message(item)
    for url in item.get("images") or []:
        try:
            data = http_get(url, binary=True)
        except Exception:
            continue
        if data[:3] != b"\xff\xd8\xff" and data[:4] != b"\x89PNG":  # не картинка (заглушка)
            continue
        try:
            if _send_photo(token, chat, text, data).get("ok"):
                return
        except urllib.error.HTTPError:
            continue  # размер/пропорции не подошли — пробуем следующий
    tg_call(token, "sendMessage", {"chat_id": chat, "text": text, "parse_mode": "HTML",
                                   "disable_web_page_preview": "true"})


def check_source(name, fetch, token, chat, state):
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
    save_state(state)  # сохраняем сразу — надёжно между источниками и рестартами
    return len(fresh), first_run


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
    """Каждый источник опрашивается по своему интервалу + лёгкий джиттер,
    чтобы не создавать в логах сайта ровный периодический пульс."""
    due = {name: 0.0 for name in SOURCES}
    while True:
        now = time.time()
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
    global TG_PROXY
    argv = sys.argv[1:]
    token, chat, proxy, intervals = load_config()
    TG_PROXY = proxy
    for name, sec in intervals.items():  # необязательный override интервалов из config
        if name in SOURCES:
            SOURCES[name]["interval"] = int(sec)

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
