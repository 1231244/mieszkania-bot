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
CHAT_ID = os.environ["CHAT_ID"]

# ===== ФИЛЬТРЫ (меняй здесь) =====
MAX_PRICE = 3200   # максимальная цена, zł
ROOMS = 2          # количество комнат: 1, 2, 3 или 4
QUIET_FROM = 23    # тихие часы: с 23:00...
QUIET_TO = 7       # ...до 7:00 — сообщения без звука
# =================================
WARSAW = ZoneInfo("Europe/Warsaw")

ROOMS_OLX = {1: "one", 2: "two", 3: "three", 4: "four"}[ROOMS]
ROOMS_OTODOM = {1: "ONE", 2: "TWO", 3: "THREE", 4: "FOUR"}[ROOMS]

OLX_URL = (
    "https://www.olx.pl/nieruchomosci/mieszkania/wynajem/wroclaw/"
    f"?search%5Bfilter_float_price:to%5D={MAX_PRICE}"
    f"&search%5Bfilter_enum_rooms%5D%5B0%5D={ROOMS_OLX}"
    "&search%5Bprivate_business%5D=private"
    "&search%5Border%5D=created_at:desc"
)
OTODOM_URL = (
    "https://www.otodom.pl/pl/wyniki/wynajem/mieszkanie/dolnoslaskie/wroclaw/wroclaw/wroclaw"
    f"?roomsNumber=%5B{ROOMS_OTODOM}%5D&priceMax={MAX_PRICE}"
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


def tg(method, payload):
    payload = {"chat_id": CHAT_ID.strip(), "disable_notification": is_quiet(), **payload}
    r = requests.post(f"{API}/{method}", json=payload, timeout=30)
    if not r.ok:
        print(f"Telegram НЕ отправил ({method}): {r.status_code} {r.text}")
    return r.ok


def send(text):
    tg("sendMessage", {"text": text, "parse_mode": "HTML"})


def send_offer(o, source):
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
    buttons = {"inline_keyboard": [[{"text": f"Открыть на {source} ↗", "url": o["url"]}]]}

    if o.get("photo"):
        ok = tg("sendPhoto", {"photo": o["photo"], "caption": caption,
                              "parse_mode": "HTML", "reply_markup": buttons})
        if ok:
            return
    tg("sendMessage", {"text": caption, "parse_mode": "HTML", "reply_markup": buttons,
                       "disable_web_page_preview": True})


# ---------- OLX ----------
def get_olx():
    r = requests.get(OLX_URL, headers=HEADERS, timeout=30)
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


def get_otodom():
    r = requests.get(OTODOM_URL, headers=HEADERS, timeout=30)
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
def main():
    # При ручном запуске (Run workflow) — тест + пример оформления
    manual = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
    if manual:
        send("🔔 <b>Тест:</b> бот работает и проверяет OLX + Otodom")
    example_sent = False

    seen = {}
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, encoding="utf-8") as f:
            seen = json.load(f)
        if isinstance(seen, list):          # старый формат
            seen = {"OLX": seen}

    for name, getter in SOURCES.items():
        try:
            offers = getter()
        except Exception as e:
            print(f"{name}: ошибка — {e}")
            continue
        if not offers:
            print(f"{name}: объявлений не найдено (сайт изменился или заблокировал запрос)")
            continue

        if manual and not example_sent:
            send("👀 <b>Пример</b> того, как выглядят объявления:")
            send_offer(offers[0], name)
            example_sent = True

        first_run = name not in seen
        old = seen.get(name, [])
        new = [o for o in offers if o["id"] not in set(old)]

        if first_run:
            send(f"✅ {name} подключён: сейчас {len(offers)} объявлений. "
                 "Буду присылать только новые.")
        else:
            for o in reversed([o for o in new if o.get("fresh", True)]):
                send_offer(o, name)

        seen[name] = (old + [o["id"] for o in new])[-500:]
        print(f"{name}: найдено {len(offers)}, новых {len(new)}")

    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(seen, f)


if __name__ == "__main__":
    main()
