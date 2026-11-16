# Бот: присылает новые объявления квартир с OLX и Otodom в Telegram
import html
import json
import os
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

TOKEN = os.environ["TELEGRAM_TOKEN"]
WORKER_URL = os.environ["WORKER_URL"].rstrip("/")   # адрес «мозга» на Cloudflare
API_KEY = os.environ["API_KEY"]

# ===== НАСТРОЙКИ =====
QUIET_FROM = 23    # тихие часы: с 23:00...
QUIET_TO = 7       # ...до 7:00 — сообщения без звука
# ======================
WARSAW = ZoneInfo("Europe/Warsaw")

ROOMS_OLX = {1: "one", 2: "two", 3: "three", 4: "four"}
ROOMS_OTODOM = {1: "ONE", 2: "TWO", 3: "THREE", 4: "FOUR"}


def olx_url(rooms, max_price):
    return (
        "https://www.olx.pl/nieruchomosci/mieszkania/wynajem/wroclaw/"
        f"?search%5Bfilter_float_price:to%5D={max_price}"
        f"&search%5Bfilter_enum_rooms%5D%5B0%5D={ROOMS_OLX[rooms]}"
        "&search%5Bprivate_business%5D=private"
        "&search%5Border%5D=created_at:desc"
    )


def otodom_url(rooms, max_price):
    return (
        "https://www.otodom.pl/pl/wyniki/wynajem/mieszkanie/dolnoslaskie/wroclaw/wroclaw/wroclaw"
        f"?roomsNumber=%5B{ROOMS_OTODOM[rooms]}%5D&priceMax={max_price}"
        "&ownerTypeSingleSelect=PRIVATE&by=LATEST&direction=DESC&limit=36"
    )


SEEN_FILE = "seen.json"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "pl-PL,pl;q=0.9",
}


API = f"https://api.telegram.org/bot{TOKEN.strip()}"


def is_quiet():
    h = datetime.now(WARSAW).hour
    return h >= QUIET_FROM or h < QUIET_TO


def tg(chat_id, method, payload):
    data = {"chat_id": chat_id, "disable_notification": is_quiet(), **payload}
    r = requests.post(f"{API}/{method}", json=data, timeout=30)
    if not r.ok:
        print(f"Telegram НЕ отправил ({method}) для {chat_id}: {r.status_code} {r.text}")
    return r.ok


def send_offer(chat_id, o, source):
    e = html.escape
    lines = [f"🏠 <b>{e(o['title'])}</b>", "", f"💰 <b>{e(o['price'])}</b>"]
    if o.get("area"):
        lines.append(f"📐 {e(o['area'])}")
    if o.get("district"):
        lines.append(f"📍 {e(o['district'])}")
    if o.get("when"):
        lines.append(f"🆕 {e(o['when'])}")
    lines.append(f"🔎 {source}")
    caption = "\n".join(lines)
    buttons = {"inline_keyboard": [[
        {"text": f"Открыть на {source} ↗", "url": o["url"]},
        {"text": "⭐ В избранное", "callback_data": "fav"},
    ]]}

    if o.get("photo"):
        ok = tg(chat_id, "sendPhoto", {"photo": o["photo"], "caption": caption,
                              "parse_mode": "HTML", "reply_markup": buttons})
        if ok:
            return
    tg(chat_id, "sendMessage", {"text": caption, "parse_mode": "HTML", "reply_markup": buttons,
                       "disable_web_page_preview": True})


# ---------- OLX ----------
def price_number(text):
    digits = re.sub(r"[^\d]", "", text.split(",")[0])
    return int(digits) if digits else None


def get_olx(rooms, max_price):
    r = requests.get(olx_url(rooms, max_price), headers=HEADERS, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    offers = []
    for card in soup.select('div[data-cy="l-card"]'):
        ad_id = card.get("id")
        link = card.find("a", href=True)
        if not ad_id or not link:
            continue
        url = link["href"]
        if url.startswith("/"):
            url = "https://www.olx.pl" + url
        url = url.split("?")[0].split("#")[0]
        title_el = card.find(["h4", "h6", "h3"])
        title = title_el.get_text(strip=True) if title_el else link.get_text(strip=True)
        price_el = card.find(attrs={"data-testid": "ad-price"})
        price = price_el.get_text(" ", strip=True) if price_el else "?"
        loc_el = card.find(attrs={"data-testid": "location-date"})
        loc = loc_el.get_text(" ", strip=True) if loc_el else ""
        featured = card.find(attrs={"data-testid": "adCard-featured"}) is not None
        # район и время: «Wrocław, Krzyki - Dzisiaj o 12:44»
        district, _, when = loc.partition(" - ")
        m = re.search(r"(\d+(?:[.,]\d+)?)\s*m²", card.get_text(" "))
        area = f"{m.group(1)} m²" if m else ""
        photo = ""
        img = card.find("img")
        if img:
            for attr in ("src", "data-src"):
                v = img.get(attr) or ""
                if v.startswith("http"):
                    photo = v
                    break
            if not photo and img.get("srcset"):
                photo = img["srcset"].split(",")[-1].strip().split(" ")[0]
            photo = re.sub(r";s=\d+x\d+", ";s=1000x750", photo)  # фото покрупнее
        # свежее = опубликовано сегодня и не «поднято» (Odświeżono)
        fresh = ("Dzisiaj" in loc) and ("Odświeżono" not in loc) and not featured
        offers.append({"id": ad_id, "title": title, "price": price, "loc": loc,
                       "price_num": price_number(price),
                       "district": district, "when": when, "area": area,
                       "photo": photo, "url": url, "fresh": fresh})
    return offers


# ---------- OTODOM ----------
def find_ads(node):
    """Ищет в данных страницы список объявлений (у каждого есть id и slug)."""
    if isinstance(node, list):
        if node and all(isinstance(x, dict) and "slug" in x and "id" in x for x in node):
            return node
        for x in node:
            found = find_ads(x)
            if found:
                return found
    elif isinstance(node, dict):
        for v in node.values():
            found = find_ads(v)
            if found:
                return found
    return None


def otodom_price(ad):
    for key in ("totalPrice", "rentPrice", "price"):
        p = ad.get(key)
        if isinstance(p, dict) and p.get("value"):
            return f"{p['value']} {p.get('currency', 'zł')}"
    return "?"


def otodom_location(ad):
    loc = ad.get("location") or {}
    places = (loc.get("reverseGeocoding") or {}).get("locations") or []
    if places:
        return places[-1].get("fullName", "")
    street = ((loc.get("address") or {}).get("street") or {}).get("name", "")
    return street


def otodom_fresh(ad):
    """Свежее = создано за последние 2 дня (если сайт отдаёт дату)."""
    created = ad.get("createdAtFirst") or ad.get("dateCreatedFirst") or ad.get("dateCreated")
    if not created:
        return True
    try:
        dt = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - dt < timedelta(days=2)
    except ValueError:
        return True


def otodom_photo(ad):
    imgs = ad.get("images") or []
    if imgs and isinstance(imgs[0], dict):
        for key in ("large", "medium", "small"):
            if imgs[0].get(key):
                return imgs[0][key]
    return ""


def otodom_when(ad):
    created = ad.get("createdAtFirst") or ad.get("dateCreatedFirst") or ad.get("dateCreated")
    if not created:
        return ""
    try:
        dt = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return "Опубликовано " + dt.astimezone(WARSAW).strftime("%d.%m в %H:%M")
    except ValueError:
        return ""


def get_otodom(rooms, max_price):
    r = requests.get(otodom_url(rooms, max_price), headers=HEADERS, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    script = soup.find("script", id="__NEXT_DATA__")
    if not script:
        return []
    ads = find_ads(json.loads(script.string)) or []
    offers = []
    for ad in ads:
        offers.append({
            "id": str(ad["id"]),
            "title": ad.get("title", "Объявление"),
            "price": otodom_price(ad),
            "price_num": price_number(otodom_price(ad)),
            "loc": otodom_location(ad),
            "url": f"https://www.otodom.pl/pl/oferta/{ad['slug']}",
            "district": otodom_location(ad),
            "area": f"{ad['areaInSquareMeters']} m²" if ad.get("areaInSquareMeters") else "",
            "when": otodom_when(ad),
            "photo": otodom_photo(ad),
            "fresh": otodom_fresh(ad),
        })
    return offers


SOURCES = {"OLX": get_olx, "Otodom": get_otodom}


# ---------- ГЛАВНОЕ ----------
def get_users():
    r = requests.get(f"{WORKER_URL}/users", params={"key": API_KEY}, timeout=30)
    r.raise_for_status()
    return r.json()


def main():
    users = get_users()
    print(f"Подписчиков: {len(users)}")
    if not users:
        return

    seen = {}
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, encoding="utf-8") as f:
            seen = json.load(f)
        if not isinstance(seen, dict):
            seen = {}

    already_sent = set()   # чтобы одно объявление не пришло человеку дважды

    # Для каждого количества комнат — одна проверка с максимальной ценой среди подписчиков
    all_rooms = sorted({r for u in users for r in u["rooms"]})
    for rooms in all_rooms:
        wanting = [u for u in users if rooms in u["rooms"]]
        max_price = max(u["maxPrice"] for u in wanting)

        for name, getter in SOURCES.items():
            key = f"{name}:{rooms}"
            try:
                offers = getter(rooms, max_price)
            except Exception as e:
                print(f"{key}: ошибка — {e}")
                continue
            if not offers:
                print(f"{key}: объявлений не найдено")
                continue

            first_run = key not in seen
            old = set(seen.get(key, []))
            new = [o for o in offers if o["id"] not in old]

            if not first_run:   # при первой проверке просто запоминаем, без рассылки
                for o in reversed([o for o in new if o.get("fresh", True)]):
                    for u in wanting:
                        if (u["id"], o["url"]) in already_sent:
                            continue
                        if o.get("price_num") is None or o["price_num"] <= u["maxPrice"]:
                            send_offer(u["id"], o, name)
                            already_sent.add((u["id"], o["url"]))

            seen[key] = (seen.get(key, []) + [o["id"] for o in new])[-500:]
            print(f"{key}: найдено {len(offers)}, новых {len(new)}")

    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(seen, f)


if __name__ == "__main__":
    main()
