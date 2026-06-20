#!/usr/bin/env python3
"""Монитор новых поступлений abelbooks.ru → закрытый Telegram-канал."""
import html
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://abelbooks.ru/wp-json/wc/store/v1/products"
UA = "abel-monitor/1.0 (personal new-arrivals notifier)"
HERE = Path(__file__).resolve().parent
STATE_FILE = HERE / "seen_ids.json"
CONFIG_FILE = HERE / "config.json"


def load_config():
    cfg = json.loads(CONFIG_FILE.read_text("utf-8")) if CONFIG_FILE.exists() else {}
    token = os.environ.get("BOT_TOKEN") or cfg.get("bot_token")
    chat = os.environ.get("CHANNEL_ID") or cfg.get("channel_id")
    interval = int(os.environ.get("INTERVAL") or cfg.get("interval_seconds", 900))
    per_page = int(cfg.get("per_page", 50))
    return token, chat, interval, per_page


def load_seen():
    if STATE_FILE.exists():
        return set(json.loads(STATE_FILE.read_text("utf-8")))
    return set()


def save_seen(seen):
    STATE_FILE.write_text(json.dumps(sorted(seen)), "utf-8")


def fetch_latest(per_page):
    params = urllib.parse.urlencode(
        {"orderby": "date", "order": "desc", "per_page": per_page}
    )
    req = urllib.request.Request(f"{API}?{params}", headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def format_message(p):
    name = html.escape(p.get("name") or "Без названия")
    prices = p.get("prices") or {}
    price = prices.get("price")
    minor = prices.get("currency_minor_unit", 0) or 0
    symbol = prices.get("currency_symbol", "₽")
    cats = ", ".join(html.escape(c["name"]) for c in (p.get("categories") or [])[:3])

    lines = [f"📚 <b>{name}</b>"]
    if price not in (None, ""):
        val = int(price) / (10 ** minor) if minor else int(price)
        lines.append(f"💰 {val:,.0f}".replace(",", " ") + f" {symbol}")
    if cats:
        lines.append(f"🏷 {cats}")
    lines.append(f'<a href="{p.get("permalink") or "https://abelbooks.ru/"}">Открыть на сайте</a>')
    return "\n".join(lines)


def tg_call(token, method, data):
    body = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}", data=body)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def _download(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def _image_candidates(image):
    """URL обложек от предпочтительного размера к запасному.

    Telegram не может скачать фото с сайта по URL (отдаёт заглушку), а оригинал
    из src слишком большой для sendPhoto. Поэтому берём из srcset валидный размер.
    """
    sized = []
    for token in (image.get("srcset") or "").split(","):
        url, _, w = token.strip().rpartition(" ")
        if w.endswith("w") and w[:-1].isdigit():
            sized.append((int(w[:-1]), url))
    order = [u for _, u in sorted((c for c in sized if c[0] <= 1280), key=lambda x: -x[0])]
    for extra in (image.get("thumbnail"), image.get("src")):
        if extra and extra not in order:
            order.append(extra)
    return order


def _send_photo(token, chat, caption, data):
    b = "----abel" + os.urandom(8).hex()
    def part(name, value):
        return (f'--{b}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n').encode("utf-8")
    body = part("chat_id", chat) + part("caption", caption[:1024]) + part("parse_mode", "HTML")
    body += (f'--{b}\r\nContent-Disposition: form-data; name="photo"; '
             f'filename="cover.jpg"\r\nContent-Type: image/jpeg\r\n\r\n').encode("utf-8")
    body += data + b"\r\n" + f"--{b}--\r\n".encode("utf-8")
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendPhoto", data=body,
                                 headers={"Content-Type": f"multipart/form-data; boundary={b}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def notify(token, chat, product):
    text = format_message(product)
    for url in _image_candidates((product.get("images") or [{}])[0]):
        try:
            data = _download(url)
        except Exception:
            continue
        if data[:3] != b"\xff\xd8\xff":  # сайт отдал не jpeg (заглушку) — следующий
            continue
        try:
            if _send_photo(token, chat, text, data).get("ok"):
                return
        except urllib.error.HTTPError:
            continue  # размер/пропорции не подошли — пробуем меньший
    tg_call(token, "sendMessage", {"chat_id": chat, "text": text, "parse_mode": "HTML"})


def check_once(token, chat, per_page, seen):
    products = fetch_latest(per_page)
    first_run = not seen
    fresh = [p for p in products if p["id"] not in seen]
    for p in sorted(fresh, key=lambda x: x["id"]):
        if not first_run:
            try:
                notify(token, chat, p)
                time.sleep(1)  # бережём Telegram rate limit
            except Exception as e:
                print(f"send error {p['id']}: {e}", file=sys.stderr)
                continue
        seen.add(p["id"])
    if fresh:
        save_seen(seen)
    return len(fresh), first_run


def main():
    argv = sys.argv[1:]
    token, chat, interval, per_page = load_config()

    if "--test" in argv:
        if not token or not chat:
            sys.exit("Для --test нужны bot_token и channel_id в config.json")
        products = fetch_latest(1)
        if not products:
            sys.exit("API не вернул товаров")
        notify(token, chat, products[0])
        print("test: отправил последний товар в канал — проверь Telegram")
        return

    if "--dry" in argv:
        for p in fetch_latest(min(per_page, 10)):
            print(format_message(p) + "\n---")
        return

    if not token or not chat:
        sys.exit("Заполни bot_token и channel_id в config.json (см. config.example.json)")

    seen = load_seen()
    loop = "--loop" in argv
    while True:
        try:
            n, first = check_once(token, chat, per_page, seen)
            stamp = time.strftime("%Y-%m-%d %H:%M:%S")
            print(f"[{stamp}] " + (f"первый запуск: засеяно {n}, без отправки" if first else f"новых: {n}"))
        except Exception as e:
            print(f"[{time.strftime('%H:%M:%S')}] ошибка: {e}", file=sys.stderr)
        if not loop:
            break
        time.sleep(interval)


if __name__ == "__main__":
    main()
