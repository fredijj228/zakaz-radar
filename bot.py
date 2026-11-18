#!/usr/bin/env python3
"""
Заказ-радар — твой бот, который ищет заказы на фрилансе.

Что делает:
  1. Читает публичные Telegram-каналы с заказами (через t.me/s/<канал>)
     и RSS-ленты бирж.
  2. Оставляет только заказы с нужными словами (бот, сайт, лендинг...)
     и выкидывает мусор по стоп-словам (резюме, «сделаю бота» и т.п.).
  3. Присылает НОВЫЕ заказы тебе в Telegram с кнопками
     «Открыть заказ» и «Написать автору».

Запуск:
  python bot.py          — одна проверка (так запускает GitHub Actions)
  python bot.py --loop   — крутится сам, проверка каждые N минут (для ПК)
  python bot.py --dry    — ничего не отправляет, просто печатает найденное
  python bot.py --test   — шлёт тестовое сообщение, чтобы проверить токен

Нужны только стандартные библиотеки Python, ничего ставить не надо.
"""

import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_FILE = ROOT / "config.json"
SECRETS_FILE = ROOT / "secrets.json"
SEEN_FILE = ROOT / "seen.json"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

try:  # чтобы русские буквы и эмодзи нормально печатались в консоли Windows
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


# ───────────────────────── загрузка ─────────────────────────

def fetch(url, timeout=25):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept-Language": "ru,en;q=0.8",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


# ─────────────────── Telegram-каналы (t.me/s) ───────────────────

class TgChannelParser(HTMLParser):
    """Разбирает веб-версию публичного канала t.me/s/<name>."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.posts = []
        self._post = None
        self._depth = 0  # >0 — мы внутри блока с текстом поста

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        classes = (a.get("class") or "").split()

        # начало нового поста
        if tag == "div" and "data-post" in a and "tgme_widget_message" in classes:
            self._post = {"id": a["data-post"], "text": [], "links": []}
            self.posts.append(self._post)
            self._depth = 0
            return
        if self._post is None:
            return

        if self._depth:  # внутри текста поста
            if tag == "div":
                self._depth += 1
            elif tag == "br":
                self._post["text"].append("\n")
            elif tag == "a" and a.get("href"):
                self._post["links"].append(a["href"])
            return

        # блок текста поста (но не цитата из ответа)
        if (tag == "div" and "tgme_widget_message_text" in classes
                and "js-message_reply_text" not in classes):
            self._depth = 1

    def handle_endtag(self, tag):
        if self._depth and tag == "div":
            self._depth -= 1
            if self._depth == 0 and self._post is not None:
                self._post["text"].append("\n")

    def handle_data(self, data):
        if self._depth and self._post is not None:
            self._post["text"].append(data)


def parse_tg_channel(html_text, channel):
    p = TgChannelParser()
    p.feed(html_text)
    orders = []
    for post in p.posts:
        text = clean_text("".join(post["text"]))
        if not text:
            continue
        orders.append({
            "id": "tg:" + post["id"],
            "title": first_line(text),
            "text": text,
            "url": "https://t.me/" + post["id"],
            "source": "@" + channel,
            "contact": find_contact(text, post["links"], channel),
        })
    return orders


def get_tg_channel(channel):
    channel = channel.lstrip("@").strip()
    raw = fetch(f"https://t.me/s/{channel}")
    return parse_tg_channel(raw.decode("utf-8", errors="replace"), channel)


# ───────────────────────── RSS бирж ─────────────────────────

def parse_rss(raw, name):
    root = ET.fromstring(raw)  # кодировку XML разберёт сам (в т.ч. windows-1251)
    orders = []
    for item in root.iter("item"):
        title = clean_text(strip_html(item.findtext("title") or ""))
        link = (item.findtext("link") or "").strip()
        desc = clean_text(strip_html(item.findtext("description") or ""))
        guid = (item.findtext("guid") or link or title).strip()
        if not (title or desc):
            continue
        orders.append({
            "id": "rss:" + guid,
            "title": title or first_line(desc),
            "text": desc,
            "url": link,
            "source": name,
            "contact": None,
        })
    orders.reverse()  # в RSS новые сверху — переворачиваем, чтобы слать по порядку
    return orders


def get_rss(feed):
    return parse_rss(fetch(feed["url"]), feed.get("name") or feed["url"])


# ───────────────────────── помощники ─────────────────────────

def strip_html(s):
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    return html.unescape(s)


def clean_text(s):
    s = s.replace("\xa0", " ")
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n\s*\n\s*\n+", "\n\n", s)
    return s.strip()


def first_line(s, limit=120):
    line = s.strip().split("\n", 1)[0].strip()
    return line if len(line) <= limit else line[:limit - 1] + "…"


USERNAME_RE = re.compile(r"(?<![\w/])@([A-Za-z][A-Za-z0-9_]{3,31})\b")
TME_RE = re.compile(r"^https?://(?:t\.me|telegram\.me)/([A-Za-z][A-Za-z0-9_]{3,31})/?$", re.I)


def find_contact(text, links, channel):
    """Ищет в посте, кому писать: сначала живого человека, потом бота."""
    names = []
    for href in links:
        m = TME_RE.match(href.strip())
        if m:
            names.append(m.group(1))
    names += USERNAME_RE.findall(text)

    skip = {channel.lower(), "joinchat", "share", "addstickers", "proxy", "s"}
    people, bots = [], []
    for n in names:
        if n.lower() in skip:
            continue
        (bots if n.lower().endswith("bot") else people).append(n)
    best = (people or bots or [None])[0]
    return f"https://t.me/{best}" if best else None


def build_matcher(words):
    """Слово должно начинаться с начала слова: «бот» найдёт «бота», «чат-бот»,
    но НЕ «работа»."""
    parts = [r"(?<!\w)" + re.escape(w.strip().lower()) for w in words if w.strip()]
    return re.compile("|".join(parts), re.I) if parts else None


def fingerprint(order):
    """Один и тот же заказ часто кидают в несколько каналов — ловим дубли."""
    t = re.sub(r"\W+", "", (order["title"] + order["text"]).lower())[:300]
    return "h:" + hashlib.sha1(t.encode()).hexdigest()[:16]


# ───────────────────────── Telegram API ─────────────────────────

def tg_api(token, method, payload, retry=True):
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        if e.code == 429 and retry:  # слишком часто — Telegram просит подождать
            wait = json.loads(body).get("parameters", {}).get("retry_after", 5)
            time.sleep(wait + 1)
            return tg_api(token, method, payload, retry=False)
        raise RuntimeError(f"Telegram ответил {e.code}: {body}") from None


def format_order(order, max_len):
    e = html.escape
    body = order["text"]
    if body.startswith(order["title"]):
        body = body[len(order["title"]):].strip()
    if len(body) > max_len:
        body = body[:max_len].rstrip() + "…"
    parts = [f"🔔 <b>{e(order['title'])}</b>"]
    if body:
        parts.append(e(body))
    parts.append(f"📍 {e(order['source'])}")
    text = "\n\n".join(parts)

    buttons = []
    if order.get("url"):
        buttons.append({"text": "📄 Открыть заказ", "url": order["url"]})
    if order.get("contact"):
        buttons.append({"text": "✉️ Написать автору", "url": order["contact"]})
    return text, buttons


def send_order(token, chat_id, order, max_len):
    text, buttons = format_order(order, max_len)
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if buttons:
        payload["reply_markup"] = {"inline_keyboard": [buttons]}
    tg_api(token, "sendMessage", payload)


# ───────────────────────── настройки ─────────────────────────

def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def load_secrets():
    s = load_json(SECRETS_FILE, {})
    token = os.environ.get("TELEGRAM_TOKEN") or s.get("telegram_token", "")
    chat_id = os.environ.get("CHAT_ID") or s.get("chat_id", "")
    if not token or "ВСТАВЬ" in token or not str(chat_id).strip() or "ВСТАВЬ" in str(chat_id):
        sys.exit("❌ Нет токена или chat_id. Заполни secrets.json "
                 "(или переменные TELEGRAM_TOKEN и CHAT_ID). См. README.md")
    return token.strip(), str(chat_id).strip()


# ───────────────────────── главная проверка ─────────────────────────

def collect(cfg):
    orders = []
    for ch in cfg.get("telegram_channels", []):
        try:
            got = get_tg_channel(ch)
            print(f"  ✓ @{ch.lstrip('@')}: {len(got)} постов")
            orders += got
        except Exception as ex:
            print(f"  ✗ @{ch.lstrip('@')}: {ex}")
    for feed in cfg.get("rss_feeds", []):
        try:
            got = get_rss(feed)
            print(f"  ✓ {feed.get('name', feed['url'])}: {len(got)} заказов")
            orders += got
        except Exception as ex:
            print(f"  ✗ {feed.get('name', feed['url'])}: {ex}")
    return orders


def run_once(cfg, dry=False):
    print(time.strftime("%H:%M:%S"), "— проверяю источники…")
    want = build_matcher(cfg.get("keywords", []))
    stop = build_matcher(cfg.get("stop_words", []))

    first_run = not SEEN_FILE.exists()
    seen_list = load_json(SEEN_FILE, {"ids": []})["ids"]
    seen = set(seen_list)

    fresh = []
    for o in collect(cfg):
        fp = fingerprint(o)
        if o["id"] in seen or fp in seen:
            continue
        seen.add(o["id"]); seen.add(fp)
        seen_list += [o["id"], fp]
        full = (o["title"] + "\n" + o["text"]).lower()
        if want and not want.search(full):
            continue
        if stop and stop.search(full):
            continue
        fresh.append(o)

    limit = cfg.get("first_run_send", 3) if first_run else cfg.get("max_per_run", 15)
    to_send = fresh[-limit:] if limit else []
    print(f"  Новых подходящих: {len(fresh)}, отправляю: {len(to_send)}")

    if dry:
        for o in to_send:
            text, buttons = format_order(o, cfg.get("max_text_length", 700))
            print("-" * 50); print(text)
            for b in buttons:
                print(f"  [{b['text']}] → {b['url']}")
        return

    token, chat_id = load_secrets()
    if first_run:
        tg_api(token, "sendMessage", {
            "chat_id": chat_id,
            "text": "🚀 Радар заказов запущен! Вот последние подходящие заказы, "
                    "дальше буду присылать только новые.",
        })
    for o in to_send:
        try:
            send_order(token, chat_id, o, cfg.get("max_text_length", 700))
            time.sleep(1.1)  # не больше 1 сообщения в секунду
        except Exception as ex:
            print("  ✗ не отправилось:", ex)

    SEEN_FILE.write_text(
        json.dumps({"ids": seen_list[-6000:]}, ensure_ascii=False, indent=0),
        encoding="utf-8",
    )


def main():
    cfg = load_json(CONFIG_FILE, None)
    if cfg is None:
        sys.exit("❌ Не найден config.json рядом с bot.py")
    args = set(sys.argv[1:])

    if "--test" in args:
        token, chat_id = load_secrets()
        tg_api(token, "sendMessage", {"chat_id": chat_id,
                                      "text": "✅ Всё работает! Бот видит тебя."})
        print("Тестовое сообщение отправлено, проверь Telegram.")
        return

    if "--loop" in args:
        every = max(1, int(cfg.get("interval_minutes", 5)))
        print(f"Радар работает, проверка каждые {every} мин. Закрой окно, чтобы остановить.")
        while True:
            try:
                run_once(cfg)
            except Exception as ex:
                print("  ✗ ошибка:", ex)
            time.sleep(every * 60)
    else:
        run_once(cfg, dry="--dry" in args)


if __name__ == "__main__":
    main()
