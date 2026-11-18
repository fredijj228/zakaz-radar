#!/usr/bin/env python3
"""
Заказ-радар — собирает заказы на ботов, mini apps и сайты.

Что делает за один запуск:
  1. Читает источники: публичные Telegram-каналы (t.me/s/<канал>),
     биржу Kwork и RSS бирж (FL.ru).
  2. Оставляет только заказы по твоей теме, выкидывает вакансии SMM,
     резюме и спам.
  3. Определяет тип заказа (бот / mini app / сайт / парсер / ИИ) и бюджет.
  4. Складывает свежие заказы в orders.json — его показывает мини-апп.
  5. Новые заказы присылает тебе в Telegram (если включено).

Запуск:
  python bot.py          — одна проверка (так запускает GitHub Actions)
  python bot.py --loop   — крутится сам, проверка каждые N минут (для ПК)
  python bot.py --dry    — ничего не отправляет и не сохраняет, только печатает
  python bot.py --test   — тестовое сообщение, чтобы проверить токен

Нужен только стандартный Python 3.9+, ничего ставить не надо.
"""

import email.utils
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
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_FILE = ROOT / "config.json"
SECRETS_FILE = ROOT / "secrets.json"
SEEN_FILE = ROOT / "seen.json"
ORDERS_FILE = ROOT / "orders.json"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
MSK = timezone(timedelta(hours=3))

try:  # русские буквы и эмодзи в консоли Windows
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def now_utc():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


def parse_iso(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def fetch(url, timeout=25):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept-Language": "ru,en;q=0.8",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


# ─────────────────────── Telegram-каналы ───────────────────────

class TgChannelParser(HTMLParser):
    """Разбирает веб-версию публичного канала t.me/s/<name>."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.posts = []
        self._post = None
        self._depth = 0  # >0 — внутри текста поста

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        classes = (a.get("class") or "").split()
        if tag == "div" and "data-post" in a and "tgme_widget_message" in classes:
            self._post = {"id": a["data-post"], "text": [], "links": [], "date": None}
            self.posts.append(self._post)
            self._depth = 0
            return
        if self._post is None:
            return
        if self._depth:
            if tag == "div":
                self._depth += 1
            elif tag == "br":
                self._post["text"].append("\n")
            elif tag == "a" and a.get("href"):
                self._post["links"].append(a["href"])
            return
        if (tag == "div" and "tgme_widget_message_text" in classes
                and "js-message_reply_text" not in classes):
            self._depth = 1
        elif tag == "time" and a.get("datetime"):
            self._post["date"] = a["datetime"]

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
            "date": iso(parse_iso(post["date"])),
            "budget": None,
            "offers": None,
        })
    return orders


def get_tg_channel(channel):
    channel = channel.lstrip("@").strip()
    raw = fetch(f"https://t.me/s/{channel}")
    return parse_tg_channel(raw.decode("utf-8", errors="replace"), channel)


# ─────────────────────────── Kwork ───────────────────────────

def extract_json_array(text, key):
    """Достаёт JSON-массив "key":[...] из кода страницы."""
    start = text.find(f'"{key}":[')
    if start < 0:
        return None
    i = start + len(key) + 3
    depth, in_str, esc = 0, False, False
    for p in range(i, len(text)):
        c = text[p]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c in "[{":
            depth += 1
        elif c in "]}":
            depth -= 1
            if depth == 0:
                return json.loads(text[i:p + 1])
    return None


def parse_kwork(html_text):
    wants = extract_json_array(html_text, "wants") or []
    orders = []
    for w in wants:
        try:
            price = float(w.get("priceLimit") or 0) or None
        except (TypeError, ValueError):
            price = None
        date = None
        if w.get("date_active"):
            try:
                date = datetime.strptime(w["date_active"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=MSK)
            except ValueError:
                pass
        desc = clean_text(strip_html(w.get("description") or ""))
        orders.append({
            "id": f"kwork:{w.get('id')}",
            "title": clean_text(w.get("name") or first_line(desc)),
            "text": desc,
            "url": f"https://kwork.ru/projects/{w.get('id')}",
            "source": "Kwork",
            "contact": None,
            "date": iso(date),
            "budget": int(price) if price else None,
            "offers": w.get("kwork_count"),
        })
    return orders


def get_kwork(cfg):
    orders = []
    for cat in cfg.get("categories", [11]):
        for page in range(1, int(cfg.get("pages", 2)) + 1):
            url = f"https://kwork.ru/projects?c={cat}" + (f"&page={page}" if page > 1 else "")
            orders += parse_kwork(fetch(url).decode("utf-8", errors="replace"))
    return orders


# ──────────────────────────── RSS ────────────────────────────

def parse_rss(raw, name):
    root = ET.fromstring(raw)  # кодировку (в т.ч. windows-1251) XML разберёт сам
    orders = []
    for item in root.iter("item"):
        title = clean_text(strip_html(item.findtext("title") or ""))
        link = (item.findtext("link") or "").strip()
        desc = clean_text(strip_html(item.findtext("description") or ""))
        guid = (item.findtext("guid") or link or title).strip()
        if not (title or desc):
            continue
        date = None
        if item.findtext("pubDate"):
            try:
                date = email.utils.parsedate_to_datetime(item.findtext("pubDate"))
            except (TypeError, ValueError):
                pass
        budget = None
        m = re.search(r"\(\s*Бюджет:?\s*([^)]*)\)\s*$", title, re.I)
        if m:
            budget = parse_money(m.group(1))
            title = title[:m.start()].strip()
        orders.append({
            "id": "rss:" + guid,
            "title": title or first_line(desc),
            "text": desc,
            "url": link,
            "source": name,
            "contact": None,
            "date": iso(date),
            "budget": budget,
            "offers": None,
        })
    orders.reverse()
    return orders


def get_rss(feed):
    return parse_rss(fetch(feed["url"]), feed.get("name") or feed["url"])


# ───────────────────────── помощники ─────────────────────────

def strip_html(s):
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"</p>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    return html.unescape(s)


def clean_text(s):
    s = s.replace("\xa0", " ").replace("\r", "")
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r" *\n *", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def first_line(s, limit=140):
    line = s.strip().split("\n", 1)[0].strip()
    return line if len(line) <= limit else line[:limit - 1] + "…"


USERNAME_RE = re.compile(r"(?<![\w/])@([A-Za-z][A-Za-z0-9_]{3,31})\b")
TME_RE = re.compile(r"^https?://(?:t\.me|telegram\.me)/([A-Za-z][A-Za-z0-9_]{3,31})/?(\?\S*)?$", re.I)


def find_contact(text, links, channel):
    """Кому писать: сначала живой человек, потом бот (в т.ч. ссылка вида bot?start=…)."""
    found = []
    for h in links:
        m = TME_RE.match(h.strip())
        if m:
            found.append((m.group(1), h.strip()))
    found += [(n, f"https://t.me/{n}") for n in USERNAME_RE.findall(text)]
    skip = {channel.lower(), "joinchat", "share", "addstickers", "proxy", "s", "addlist", "boost"}
    people, bots = [], []
    for name, url in found:
        if name.lower() in skip:
            continue
        (bots if name.lower().endswith("bot") else people).append(url)
    return (people or bots or [None])[0]


HASHTAG_LINE_RE = re.compile(r"^(\s*#[\w-]+[,.;]?)+\s*$")


def strip_service_lines(text, ignore_rx):
    """Убирает служебные строки каналов: подписи вида «Контакт заказчика — в боте»
    и строки из одних хэштегов, чтобы они не влияли на поиск слов."""
    keep = []
    for line in text.split("\n"):
        if HASHTAG_LINE_RE.match(line):
            continue
        if ignore_rx and ignore_rx.search(line.lower()):
            continue
        keep.append(line)
    return clean_text("\n".join(keep))


MONEY_RE = re.compile(
    r"(\d{1,3}(?:[   .,]\d{3})+|\d+(?:[.,]\d+)?)\s*"
    r"(к|k|тыс\.?|т\.?\s?р\.?)?\s*"
    r"(₽|руб\w*|р\.|rub|rur)",
    re.I,
)


BUDGET_WORD_RE = re.compile(
    r"(?:бюджет|оплата|цена|стоимость|плачу|заплачу)\s*:?\s*(?:до|от|около|~)?\s*"
    r"(\d{1,3}(?:[   .,]\d{3})+|\d+(?:[.,]\d+)?)\s*(к|k|тыс\.?|т\.?\s?р\.?)?(?!\w)",
    re.I,
)


def parse_money(s):
    m = MONEY_RE.search(s or "") or BUDGET_WORD_RE.search(s or "")
    if not m:
        return None
    num = m.group(1)
    if re.fullmatch(r"\d{1,3}(?:[   .,]\d{3})+", num):
        value = float(re.sub(r"\D", "", num))
    else:
        value = float(num.replace(",", "."))
    if m.group(2):
        value *= 1000
    value = int(value)
    return value if 100 <= value <= 10_000_000 else None


def build_matcher(words):
    """Слово ищется с начала слова: «бот» найдёт «бота», «чат-бот», но не «работа».
    Пробел в фразе = любые пробелы, * = любое окончание слова,
    ! в конце = только целое слово (например «ии!»)."""
    parts = []
    for w in words:
        w = w.strip().lower()
        if not w:
            continue
        whole = w.endswith("!")
        w = w.rstrip("!")
        pat = re.escape(w).replace(r"\*", r"\w*").replace(r"\ ", r"\s+")
        parts.append(r"(?<!\w)" + pat + (r"(?!\w)" if whole else ""))
    return re.compile("|".join(parts), re.I) if parts else None


CATEGORIES = {
    "bot": build_matcher(["бот", "чат-бот", "aiogram", "telebot", "telegram-бот", "тг-бот"]),
    "miniapp": build_matcher(["mini app", "miniapp", "мини-апп", "мини апп", "мини-прилож*",
                              "мини прилож*", "web app", "webapp", "tma!"]),
    "site": build_matcher(["сайт", "лендинг", "landing", "tilda", "тильд", "верстк", "вёрстк",
                           "wordpress", "html", "интернет-магазин", "битрикс", "frontend", "фронтенд"]),
    "parser": build_matcher(["парсер", "парсинг", "спарс*", "парсить", "scrap*", "скрапинг"]),
    "ai": build_matcher(["ии!", "ai!", "нейросет*", "gpt", "chatgpt", "openai", "llm!", "ии-агент*",
                         "ai-агент*", "claude", "gemini!", "deepseek"]),
}


def categorize(text):
    return [name for name, rx in CATEGORIES.items() if rx.search(text)]


def fingerprint(order):
    t = re.sub(r"\W+", "", (order["title"] + order["text"]).lower())[:300]
    return "h:" + hashlib.sha1(t.encode()).hexdigest()[:16]


# ──────────────────────── Telegram API ────────────────────────

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
        if e.code == 429 and retry:
            wait = json.loads(body).get("parameters", {}).get("retry_after", 5)
            time.sleep(wait + 1)
            return tg_api(token, method, payload, retry=False)
        raise RuntimeError(f"Telegram ответил {e.code}: {body}") from None


CAT_LABEL = {"bot": "🤖 Бот", "miniapp": "📱 Mini App", "site": "🌐 Сайт",
             "parser": "🕷 Парсер", "ai": "🧠 ИИ"}


def fmt_money(v):
    return f"{v:,}".replace(",", " ") + " ₽"


def format_order(order, max_len, webapp_url=None):
    e = html.escape
    body = order["text"]
    if body.startswith(order["title"]):
        body = body[len(order["title"]):].strip()
    if len(body) > max_len:
        body = body[:max_len].rstrip() + "…"
    head = []
    if order.get("budget"):
        head.append(f"💰 <b>{fmt_money(order['budget'])}</b>")
    head += [CAT_LABEL[c] for c in order.get("cats", []) if c in CAT_LABEL]
    parts = [f"<b>{e(order['title'])}</b>"]
    if head:
        parts.append("  ".join(head))
    if body:
        parts.append(e(body))
    tail = f"📍 {e(order['source'])}"
    if order.get("offers") is not None:
        tail += f"   👥 откликов: {order['offers']}"
    parts.append(tail)

    rows, row = [], []
    if order.get("url"):
        row.append({"text": "Открыть заказ", "url": order["url"]})
    if order.get("contact"):
        row.append({"text": "Написать", "url": order["contact"]})
    if row:
        rows.append(row)
    if webapp_url:
        rows.append([{"text": "Все заказы", "web_app": {"url": webapp_url}}])
    return "\n\n".join(parts), rows


def send_order(token, chat_id, order, max_len, webapp_url):
    text, rows = format_order(order, max_len, webapp_url)
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
               "disable_web_page_preview": True}
    if rows:
        payload["reply_markup"] = {"inline_keyboard": rows}
    tg_api(token, "sendMessage", payload)


# ───────────────────────── настройки ─────────────────────────

def load_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, data, indent=None):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=indent), encoding="utf-8")


def load_secrets(required=True):
    s = load_json(SECRETS_FILE, {})
    token = (os.environ.get("TELEGRAM_TOKEN") or s.get("telegram_token", "")).strip()
    chat_id = str(os.environ.get("CHAT_ID") or s.get("chat_id", "")).strip()
    if not token or "ВСТАВЬ" in token or not chat_id or "ВСТАВЬ" in chat_id:
        if required:
            sys.exit("❌ Нет токена или chat_id. Заполни secrets.json "
                     "(или переменные TELEGRAM_TOKEN и CHAT_ID). См. README.md")
        return None, None
    return token, chat_id


# ─────────────────────── главная проверка ───────────────────────

def collect(cfg):
    orders, report = [], []

    def run(name, fn):
        try:
            got = fn()
            print(f"  ✓ {name}: {len(got)}")
            report.append({"name": name, "ok": True, "count": len(got)})
            orders.extend(got)
        except Exception as ex:
            print(f"  ✗ {name}: {ex}")
            report.append({"name": name, "ok": False, "count": 0})

    for ch in cfg.get("telegram_channels", []):
        run("@" + ch.lstrip("@"), lambda ch=ch: get_tg_channel(ch))
    if cfg.get("kwork", {}).get("enabled", False):
        run("Kwork", lambda: get_kwork(cfg.get("kwork", {})))
    for feed in cfg.get("rss_feeds", []):
        run(feed.get("name", feed["url"]), lambda feed=feed: get_rss(feed))
    return orders, report


def is_relevant(o, want, stop, title_stop):
    full = (o["title"] + "\n" + o["text"]).lower()
    if want and not want.search(full):
        return False
    if stop and stop.search(full):
        return False
    if title_stop and title_stop.search(o["title"].lower()):
        return False
    return True


def run_once(cfg, dry=False):
    print(time.strftime("%H:%M:%S"), "— проверяю источники…")
    want = build_matcher(cfg.get("keywords", []))
    stop = build_matcher(cfg.get("stop_words", []))
    title_stop = build_matcher(cfg.get("title_stop_words", []))
    ignore_lines = build_matcher(cfg.get("ignore_lines", []))
    max_age = timedelta(days=cfg.get("max_age_days", 10))
    now = now_utc()

    state = load_json(SEEN_FILE, {})
    first_run = "ids" not in state
    seen_list = state.get("ids", [])
    seen = set(seen_list)
    known_sources = set(state.get("known_sources", []))

    collected, report = collect(cfg)

    archive = load_json(ORDERS_FILE, {}).get("orders", [])
    by_key = {o["key"]: o for o in archive if "key" in o}

    fresh = []
    for o in collected:
        o["text"] = strip_service_lines(o["text"], ignore_lines)
        if not o["text"] and not o["title"]:
            continue
        if o.get("budget") is None:
            o["budget"] = parse_money(o["title"] + "\n" + o["text"][:600])
        date = parse_iso(o.get("date"))
        if date and now - date > max_age:
            continue
        if not is_relevant(o, want, stop, title_stop):
            continue
        key = fingerprint(o)
        o["key"] = key
        o["cats"] = categorize(o["title"] + "\n" + o["text"])
        o["text"] = o["text"][:2000]
        old = by_key.get(key)
        o["found"] = old["found"] if old else iso(now)
        by_key[key] = o

        if o["id"] in seen or key in seen:
            continue
        seen.add(o["id"]); seen.add(key)
        seen_list += [o["id"], key]
        src = o["source"]
        if not first_run and src in known_sources:
            fresh.append(o)

    # архив для мини-аппа: только свежие, новые сверху
    def sort_key(o):
        return o.get("date") or o.get("found") or ""
    active = {r["name"] for r in report}
    archive = [o for o in by_key.values()
               if now - (parse_iso(o.get("date") or o.get("found")) or now) <= max_age
               and o.get("source") in active
               and is_relevant(o, want, stop, title_stop)]
    archive.sort(key=sort_key, reverse=True)
    archive = archive[:cfg.get("archive_size", 400)]

    limit = cfg.get("first_run_send", 3) if first_run else cfg.get("max_per_run", 15)
    if first_run:
        fresh = sorted([o for o in archive], key=sort_key)
    to_send = fresh[-limit:] if limit else []
    print(f"  В ленте: {len(archive)}, новых: {len(fresh)}, отправляю: {len(to_send)}")

    webapp_url = cfg.get("webapp_url") or None
    if dry:
        for o in to_send:
            text, rows = format_order(o, cfg.get("max_text_length", 600), webapp_url)
            print("-" * 50); print(text)
            for r in rows:
                print("  ", " | ".join(f"[{b['text']}]" for b in r))
        return archive

    save_json(ORDERS_FILE, {
        "updated": iso(now),
        "sources": report,
        "orders": archive,
    })

    token, chat_id = load_secrets(required=cfg.get("notify", True))
    if token:
        if webapp_url and state.get("menu") != webapp_url:
            try:
                tg_api(token, "setChatMenuButton", {
                    "chat_id": int(chat_id) if chat_id.lstrip("-").isdigit() else chat_id,
                    "menu_button": {"type": "web_app", "text": "Заказы",
                                    "web_app": {"url": webapp_url}},
                })
                state["menu"] = webapp_url
                print("  ✓ кнопка «Заказы» добавлена в чат с ботом")
            except Exception as ex:
                print("  ✗ кнопка меню:", ex)
        if cfg.get("notify", True):
            if first_run:
                tg_api(token, "sendMessage", {
                    "chat_id": chat_id,
                    "text": "🚀 Радар заказов запущен! Дальше буду присылать только новые.",
                })
            for o in to_send:
                try:
                    send_order(token, chat_id, o, cfg.get("max_text_length", 600), webapp_url)
                    time.sleep(1.1)
                except Exception as ex:
                    print("  ✗ не отправилось:", ex)

    state["ids"] = seen_list[-8000:]
    state["known_sources"] = sorted(known_sources | {r["name"] for r in report if r["ok"]}
                                    | {o["source"] for o in collected})
    save_json(SEEN_FILE, state)
    return archive


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
