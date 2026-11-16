# Бот: присылает новые объявления квартир с OLX в Telegram
import json
import os

import requests
from bs4 import BeautifulSoup

TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]

# ===== ФИЛЬТРЫ (меняй здесь) =====
MAX_PRICE = 3200          # максимальная цена, zł
ROOMS = "two"             # one / two / three / four
# =================================

URL = (
    "https://www.olx.pl/nieruchomosci/mieszkania/wynajem/wroclaw/"
    f"?search%5Bfilter_float_price:to%5D={MAX_PRICE}"
    f"&search%5Bfilter_enum_rooms%5D%5B0%5D={ROOMS}"
    "&search%5Bprivate_business%5D=private"
    "&search%5Border%5D=created_at:desc"
)
SEEN_FILE = "seen.json"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "pl-PL,pl;q=0.9",
}


def send(text):
    requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        data={"chat_id": CHAT_ID, "text": text},
        timeout=20,
    )


def get_offers():
    r = requests.get(URL, headers=HEADERS, timeout=30)
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


def main():
    first_run = not os.path.exists(SEEN_FILE)
    seen = []
    if not first_run:
        with open(SEEN_FILE, encoding="utf-8") as f:
            seen = json.load(f)
    seen_set = set(seen)

    offers = get_offers()
    if not offers:
        print("Объявлений не найдено: возможно, OLX изменил страницу или заблокировал запрос")
        return

    new = [o for o in offers if o["id"] not in seen_set]

    if first_run:
        send(f"✅ Бот запущен! Сейчас на OLX {len(offers)} подходящих объявлений. "
             "Дальше буду присылать только новые.")
    else:
        for o in reversed(new):
            send(f"🏠 {o['title']}\n💰 {o['price']}\n📍 {o['loc']}\n{o['url']}")

    seen.extend(o["id"] for o in new)
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(seen[-500:], f)
    print(f"Найдено: {len(offers)}, новых: {len(new)}")


if __name__ == "__main__":
    main()
