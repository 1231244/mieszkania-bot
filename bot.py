# Бот: присылает новые объявления квартир с OLX и Otodom в Telegram
import json
import os

import requests
from bs4 import BeautifulSoup

TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]

# ===== ФИЛЬТРЫ (меняй здесь) =====
MAX_PRICE = 3200   # максимальная цена, zł
ROOMS = 2          # количество комнат: 1, 2, 3 или 4
# =================================

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


def send(text):
    r = requests.post(
        f"https://api.telegram.org/bot{TOKEN.strip()}/sendMessage",
        data={"chat_id": CHAT_ID.strip(), "text": text},
        timeout=20,
    )
    if not r.ok:
        print(f"Telegram НЕ отправил сообщение: {r.status_code} {r.text}")


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
        offers.append({"id": ad_id, "title": title, "price": price, "loc": loc, "url": url})
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
        })
    return offers


SOURCES = {"OLX": get_olx, "Otodom": get_otodom}


# ---------- ГЛАВНОЕ ----------
def main():
    # При ручном запуске (Run workflow) — тестовое сообщение
    if os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch":
        send("🔔 Тест: бот работает и проверяет OLX + Otodom")

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

        first_run = name not in seen
        old = seen.get(name, [])
        new = [o for o in offers if o["id"] not in set(old)]

        if first_run:
            send(f"✅ {name} подключён: сейчас {len(offers)} объявлений. "
                 "Буду присылать только новые.")
        else:
            for o in reversed(new):
                send(f"🏠 [{name}] {o['title']}\n💰 {o['price']}\n📍 {o['loc']}\n{o['url']}")

        seen[name] = (old + [o["id"] for o in new])[-500:]
        print(f"{name}: найдено {len(offers)}, новых {len(new)}")

    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(seen, f)


if __name__ == "__main__":
    main()
