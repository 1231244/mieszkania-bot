# Бот: присылает новые объявления квартир с OLX и Otodom в Telegram
import html
import json
import os
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import time

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
SNAPSHOT_FILE = "latest.json"   # все актуальные объявления — бот берёт их для «Показать сейчас»
SNAPSHOT_PRICE = 6000           # для снимка берём квартиры до этой цены
PETS_FILE = "pets.json"         # что бот уже узнал про животных: {ссылка: yes/no/unknown}
PETS_PER_RUN = 40               # сколько старых объявлений проверять за раз (новые — всегда)
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


PETS_TEXT = {"yes": "🐾 Можно с животными", "no": "🚫 Без животных", "unknown": "❔ Животные: не указано"}
ROOM_WORDS = {1: "1 комната", 2: "2 комнаты", 3: "3 комнаты", 4: "4+ комнаты"}


def minutes_ago(posted_at):
    if not posted_at:
        return None
    try:
        dt = datetime.fromisoformat(posted_at)
    except ValueError:
        return None
    return int((datetime.now(WARSAW) - dt).total_seconds() // 60)


def ago_text(mins):
    if mins is None or mins < 0:
        return ""
    if mins < 60:
        return f"{max(mins, 1)} мин назад"
    if mins < 24 * 60:
        return f"{mins // 60} ч назад"
    return f"{mins // (24 * 60)} дн назад"


def card(o, is_new):
    """Текст карточки квартиры (одинаковый для уведомлений и «Показать сейчас»)."""
    e = html.escape
    lines = []
    ago = ago_text(minutes_ago(o.get("posted_at")))
    if is_new:
        lines += [f"🔥 <b>Новое</b>" + (f" · {ago}" if ago else ""), ""]
    head = [ROOM_WORDS.get(o.get("rooms"), "")]
    if o.get("area_num"):
        head.append(f"{o['area_num']:g} м²")
    lines.append("🏠 <b>" + " · ".join(x for x in head if x) + "</b>")
    price = f"💰 <b>{e(o['price'])}</b>"
    if o.get("ppm"):
        price += f" · {o['ppm']} zł/м²"
    lines.append(price)
    deal = o.get("deal")
    if deal is not None:
        if deal <= -5:
            lines.append(f"💚 Дешевле среднего на {abs(deal)}%")
        elif deal >= 10:
            lines.append(f"🔴 Дороже среднего на {deal}%")
        else:
            lines.append("⚪ Средняя цена по рынку")
    if o.get("district_short"):
        lines.append(f"📍 {e(o['district_short'])}")
    lines.append(PETS_TEXT.get(o.get("pets"), PETS_TEXT["unknown"]))
    lines += ["", f"<i>{e(o['title'][:90])}</i>", ""]
    tail = f"👤 Частник · {o['source']}"
    if not is_new and ago:
        tail += f" · {ago}"
    lines.append(tail)
    return "\n".join(lines)


def card_buttons(o):
    second = [{"text": "⭐ В избранное", "callback_data": "fav"}]
    if o.get("district_short"):
        q = requests.utils.quote(f"{o['district_short']}, Wrocław")
        second.append({"text": "🗺 Карта", "url": f"https://www.google.com/maps/search/?api=1&query={q}"})
    return {"inline_keyboard": [
        [{"text": "🔗 Открыть объявление", "url": o["url"], "style": "primary"}],
        second,
    ]}


def send_offer(chat_id, o):
    caption = card(o, is_new=True)
    buttons = card_buttons(o)
    if o.get("photo"):
        if tg(chat_id, "sendPhoto", {"photo": o["photo"], "caption": caption,
                                     "parse_mode": "HTML", "reply_markup": buttons}):
            return
    tg(chat_id, "sendMessage", {"text": caption, "parse_mode": "HTML", "reply_markup": buttons,
                                "disable_web_page_preview": True})


def send_more(chat_id, count):
    tg(chat_id, "sendMessage", {
        "text": f"📬 И ещё <b>{count}</b> новых квартир по твоим фильтрам",
        "parse_mode": "HTML",
        "reply_markup": {"inline_keyboard": [[{"text": "👀 Листать все", "callback_data": "show:0", "style": "success"}]]},
    })


def short_district(text):
    parts = [p.strip() for p in re.split(r"[,]", text or "") if p.strip()]
    parts = [p for p in parts if p.lower() not in ("wrocław", "wroclaw", "dolnośląskie", "dolnoslaskie")]
    return ", ".join(parts[:2])


def enrich(o, rooms, source):
    o = dict(o, rooms=rooms, source=source)
    m = re.search(r"(\d+(?:[.,]\d+)?)", o.get("area") or "")
    o["area_num"] = float(m.group(1).replace(",", ".")) if m else None
    if o["area_num"] and o.get("price_num"):
        o["ppm"] = round(o["price_num"] / o["area_num"])
    o["district_short"] = short_district(o.get("district"))
    if not o.get("street"):
        m = re.search(r"\bul\.?\s+([A-ZŁŚŻŹĆÓ][\wąćęłńóśźż\-]+(?:\s+[A-ZŁŚŻŹĆÓ][\wąćęłńóśźż\-]+)?)(?:\s+(\d+[a-zA-Z]?))?",
                      o.get("title") or "")
        o["street"] = " ".join(x for x in m.groups() if x) if m else ""
    if o.get("price_num") and o["area_num"]:
        o["sig"] = f"{rooms}|{o['price_num']}|{round(o['area_num'])}|{o['district_short'].lower()}"
    else:
        o["sig"] = o["url"]
    return o


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
        posted_at = None
        t = re.search(r"Dzisiaj o (\d{1,2}):(\d{2})", when)
        if t and "Odświeżono" not in when:
            posted_at = datetime.now(WARSAW).replace(
                hour=int(t.group(1)), minute=int(t.group(2)), second=0, microsecond=0).isoformat()
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
                       "photo": photo, "url": url, "fresh": fresh, "featured": featured,
                       "posted_at": posted_at})
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


def otodom_street(ad):
    street = (((ad.get("location") or {}).get("address") or {}).get("street") or {})
    name = street.get("name") or ""
    if not name:
        return ""
    return f"{name} {street.get('number') or ''}".strip()


def otodom_photo(ad):
    imgs = ad.get("images") or []
    if imgs and isinstance(imgs[0], dict):
        for key in ("large", "medium", "small"):
            if imgs[0].get(key):
                return imgs[0][key]
    return ""


def otodom_posted(ad):
    created = ad.get("createdAtFirst") or ad.get("dateCreatedFirst") or ad.get("dateCreated")
    if not created:
        return None
    try:
        dt = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(WARSAW).isoformat()
    except ValueError:
        return None


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
            "street": otodom_street(ad),
            "area": f"{ad['areaInSquareMeters']} m²" if ad.get("areaInSquareMeters") else "",
            "when": otodom_when(ad),
            "posted_at": otodom_posted(ad),
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


# ---------- ЖИВОТНЫЕ ----------
PETS_NO = [
    r"bez zwierz", r"bez (psów|psa|kotów|kota)", r"zakaz\w* (trzymania )?zwierz",
    r"nie akceptuj\w*[^.\n]{0,25}zwierz", r"zwierz\w*[^.\n]{0,25}nie (są |jest )?(akceptowan|dozwolon|mile|przyjmuj)",
    r"zwierz\w*\s*[:\-]?\s*nie\b", r"nie dla [^.\n]{0,20}zwierz", r"no pets", r"pets not allowed",
]
PETS_YES = [
    r"zwierz\w*[^.\n]{0,30}(akceptowan|dozwolon|mile widzian|mogą|możliw|do uzgodnienia|nie ma problemu)",
    r"akceptuj\w*[^.\n]{0,25}zwierz", r"przyjazn\w*[^.\n]{0,10}zwierz", r"pet[\s-]?friendly",
    r"zwierz\w*\s*[:\-]?\s*tak\b", r"(pies|psa|piesek|kot|kota|kotek)[^.\n]{0,25}(mile|akceptowan|dozwolon|możliw|ok\b)",
    r"pets (are )?(allowed|welcome)", r"z (małym )?zwierz\w*[^.\n]{0,15}(ok|możliw|tak)",
]


def ad_text(url):
    """Текст объявления: описание + параметры."""
    r = requests.get(url, headers=HEADERS, timeout=20)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    parts = []
    for sel in ['[data-cy="ad_description"]', '[data-testid="ad-parameters-container"]',
                '[data-cy="adPageAdDescription"]', '[data-testid="ad.top-information.table"]',
                '[data-testid="ad.additional-information.table"]']:
        for el in soup.select(sel):
            parts.append(el.get_text(" ", strip=True))
    script = soup.find("script", id="__NEXT_DATA__")   # Otodom хранит описание здесь
    if script and script.string:
        try:
            ad = json.loads(script.string).get("props", {}).get("pageProps", {}).get("ad") or {}
            parts.append(BeautifulSoup(ad.get("description") or "", "html.parser").get_text(" "))
            parts.append(json.dumps(ad.get("characteristics") or [], ensure_ascii=False))
            parts.append(json.dumps(ad.get("features") or [], ensure_ascii=False))
        except (ValueError, AttributeError):
            pass
    if not any(p.strip() for p in parts):
        parts.append(soup.get_text(" ", strip=True))
    return " ".join(parts).lower()


def detect_pets(url):
    try:
        text = ad_text(url)
    except Exception as e:
        print(f"Животные: не открылось {url} — {e}")
        return None
    if any(re.search(p, text) for p in PETS_NO):
        return "no"
    if any(re.search(p, text) for p in PETS_YES):
        return "yes"
    return "unknown"


def check_pets(urls, pets, limit=None):
    checked = 0
    for url in urls:
        if url in pets:
            continue
        if limit is not None and checked >= limit:
            break
        result = detect_pets(url)
        if result:
            pets[url] = result
        checked += 1
        time.sleep(0.7)   # не торопимся, чтобы сайты не блокировали
    return checked


MAX_PER_RUN = 3   # если новых больше — присылаем 3 лучших и кнопку «Посмотреть ещё»


def main():
    users = get_users()
    print(f"Подписчиков: {len(users)}")

    seen = {}
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, encoding="utf-8") as f:
            seen = json.load(f)
        if not isinstance(seen, dict):
            seen = {}
    sigs = set(seen.get("_sigs", []))   # «отпечатки» квартир, чтобы не слать одну и ту же дважды

    # 1) Собираем объявления со всех сайтов для 1–4 комнат
    fetched = []   # (key, rooms, [offers])
    for rooms in [1, 2, 3, 4]:
        wanting = [u for u in users if rooms in u["rooms"]]
        max_price = max([SNAPSHOT_PRICE] + [u["maxPrice"] for u in wanting])
        for name, getter in SOURCES.items():
            key = f"{name}:{rooms}"
            try:
                offers = getter(rooms, max_price)
            except Exception as e:
                print(f"{key}: ошибка — {e}")
                continue
            offers = [enrich(o, rooms, name) for o in offers if not o.get("featured")]
            print(f"{key}: найдено {len(offers)}")
            fetched.append((key, rooms, offers))

    # 2) Средняя цена за м² по каждому количеству комнат
    avg = {}
    for rooms in [1, 2, 3, 4]:
        vals = sorted(o["ppm"] for k, r, offs in fetched if r == rooms for o in offs if o.get("ppm"))
        if len(vals) >= 5:
            avg[rooms] = vals[len(vals) // 2]   # медиана — честнее среднего
    for _, rooms, offers in fetched:
        for o in offers:
            if o.get("ppm") and rooms in avg:
                o["deal"] = round((o["ppm"] / avg[rooms] - 1) * 100)

    # 3) Животные: сначала все новые объявления, потом понемногу старые
    pets = {}
    if os.path.exists(PETS_FILE):
        with open(PETS_FILE, encoding="utf-8") as f:
            pets = json.load(f)
    new_urls, other_urls = [], []
    for key, rooms, offers in fetched:
        old = set(seen.get(key, []))
        for o in offers:
            (new_urls if (key in seen and o["id"] not in old) else other_urls).append(o["url"])
    n1 = check_pets(dict.fromkeys(new_urls), pets)
    n2 = check_pets(dict.fromkeys(other_urls), pets, limit=PETS_PER_RUN)
    print(f"Животные: проверено новых {n1}, старых {n2}, всего известно {len(pets)}")
    for _, _, offers in fetched:
        for o in offers:
            o["pets"] = pets.get(o["url"], "unknown")
    with open(PETS_FILE, "w", encoding="utf-8") as f:
        json.dump(dict(list(pets.items())[-3000:]), f, ensure_ascii=False)

    # 4) Новые объявления → подписчикам по их фильтрам
    to_send = {u["id"]: [] for u in users}
    for key, rooms, offers in fetched:
        first_run = key not in seen
        old = set(seen.get(key, []))
        new = [o for o in offers if o["id"] not in old]
        if not first_run:
            for o in new:
                if not o.get("fresh", True) or o["sig"] in sigs:
                    continue
                sigs.add(o["sig"])
                for u in users:
                    if u.get("hideNoPets") and o.get("pets") == "no":
                        continue
                    if rooms in u["rooms"] and o.get("price_num") and o["price_num"] <= u["maxPrice"]:
                        to_send[u["id"]].append(o)
        seen[key] = (seen.get(key, []) + [o["id"] for o in new])[-500:]
        print(f"{key}: новых {len(new)}")

    for uid, offers in to_send.items():
        if not offers:
            continue
        # лучшие сверху: самые выгодные по цене за м²
        offers.sort(key=lambda o: o.get("deal") if o.get("deal") is not None else 0)
        for o in offers[:MAX_PER_RUN]:
            send_offer(uid, o)
        if len(offers) > MAX_PER_RUN:
            send_more(uid, len(offers) - MAX_PER_RUN)
        print(f"Пользователю {uid}: отправлено {min(len(offers), MAX_PER_RUN)} из {len(offers)}")

    seen["_sigs"] = list(sigs)[-1000:]
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(seen, f)

    # 5) Снимок для кнопки «Показать сейчас»: OLX и Otodom вперемешку, без дублей
    snapshot, snap_sigs = [], set()
    for rooms in [1, 2, 3, 4]:
        lists = [offs for k, r, offs in fetched if r == rooms]
        for i in range(max((len(x) for x in lists), default=0)):
            for lst in lists:
                if i < len(lst) and lst[i]["sig"] not in snap_sigs and lst[i].get("price_num") \
                        and (minutes_ago(lst[i].get("posted_at")) or 0) < 60 * 24 * 60:
                    snap_sigs.add(lst[i]["sig"])
                    snapshot.append(lst[i])
    with open(SNAPSHOT_FILE, "w", encoding="utf-8") as f:
        json.dump({"updated": datetime.now(WARSAW).isoformat(), "avg": avg, "offers": snapshot},
                  f, ensure_ascii=False)


if __name__ == "__main__":
    main()
