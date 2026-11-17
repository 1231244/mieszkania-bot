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
INFO_FILE = "adinfo.json"       # что бот узнал, открыв объявление: {ссылка: {pets, photo}}
OLD_PETS_FILE = "pets.json"
PETS_PER_RUN = 400              # сколько старых объявлений открывать за раз (новые — всегда)
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "pl-PL,pl;q=0.9",
}


API = f"https://api.telegram.org/bot{TOKEN.strip()}"


def is_quiet():
    h = datetime.now(WARSAW).hour
    return h >= QUIET_FROM or h < QUIET_TO


ADMIN_ID = os.environ.get("ADMIN_ID", "").strip()   # твой Telegram ID: сюда приходят сигналы о поломках


def tg(chat_id, method, payload, quiet=None):
    """Отправка с очередью: не больше ~25 сообщений в секунду; если Telegram просит подождать — ждём."""
    data = {"chat_id": chat_id, "disable_notification": is_quiet() if quiet is None else quiet, **payload}
    for attempt in range(3):
        r = requests.post(f"{API}/{method}", json=data, timeout=30)
        time.sleep(0.04)
        if r.status_code == 429:   # «слишком много сообщений» — Telegram говорит, сколько ждать
            wait = (r.json().get("parameters") or {}).get("retry_after", 3)
            print(f"Telegram просит подождать {wait} с")
            time.sleep(wait + 1)
            continue
        if not r.ok:
            print(f"Telegram НЕ отправил ({method}) для {chat_id}: {r.status_code} {r.text[:200]}")
        return r.ok
    return False


def alert(text):
    """Сигнал владельцу бота."""
    print(f"СИГНАЛ: {text}")
    if ADMIN_ID:
        tg(ADMIN_ID, "sendMessage", {"text": text, "parse_mode": "HTML"}, quiet=False)


PETS_TEXT = {"yes": "🐾 Можно с животными", "no": "🚫 Без животных", "unknown": "❔ Животные: не указано"}
def money(n):
    return f"{n:,}".replace(",", " ") + " zł" if n else "?"


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
    """Компактная карточка квартиры (одинаковая для уведомлений и «Показать сейчас»)."""
    e = html.escape
    lines = []
    ago = ago_text(minutes_ago(o.get("posted_at")))
    if is_new:
        head = "🔥 <b>Новое</b>" + (f" · {ago}" if ago else "")
        if o.get("search_name"):
            head += f" · 🔎 {e(o['search_name'])}"
        lines += [head, ""]

    # 1. Что и где
    head = [ROOM_WORDS.get(o.get("rooms"), "")]
    if o.get("area_num"):
        head.append(f"{o['area_num']:g} м²")
    lines.append("🏠 <b>" + " · ".join(x for x in head if x) + "</b>")
    if o.get("district_short"):
        lines.append(f"📍 {e(o['district_short'])}")

    # 2. Деньги: сколько платить в месяц и кауция
    rent = o.get("price_num")
    if rent:
        if o.get("czynsz"):
            lines.append(f"💳 <b>{money(rent + o['czynsz'])}/мес</b>")
            lines.append(f"     {money(rent)} аренда + {money(o['czynsz'])} оплаты")
        else:
            lines.append(f"💳 <b>{money(rent)}/мес</b> · оплаты не указаны")
    else:
        lines.append(f"💳 {e(o['price'])}")
    lines.append(f"🔐 Кауция: {money(o['kaucja'])}" if o.get("kaucja") else "🔐 Кауция: не указана")

    # 3. Плюсы и минусы — только если есть что сказать
    deal = o.get("deal")
    if deal is not None and deal <= -5:
        lines.append(f"💚 На {abs(deal)}% дешевле рынка")
    elif deal is not None and deal >= 10:
        lines.append(f"🔺 На {deal}% дороже рынка")
    if o.get("pets") in ("yes", "no"):
        lines.append(PETS_TEXT[o["pets"]])

    # 4. Заголовок хозяина и источник
    lines += ["", f"<i>{e(o['title'][:80])}</i>"]
    tail = f"{o['source']} · частник"
    if not is_new and ago:
        tail += f" · {ago}"
    lines.append(f"<i>{tail}</i>")
    return "\n".join(lines)


def card_buttons(o):
    second = [{"text": "⭐ В избранное", "callback_data": "fav"}]
    if o.get("district_short"):
        q = requests.utils.quote(f"{o['district_short']}, Wrocław")
        second.append({"text": "🗺 Карта", "url": f"https://www.google.com/maps/search/?api=1&query={q}"})
    return {"inline_keyboard": [
        [{"text": "🔗 Открыть объявление", "url": o["url"]}],
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
    out = []
    for p in parts:
        if p.lower() in ("wrocław", "wroclaw", "dolnośląskie", "dolnoslaskie"):
            continue
        if p.lower() not in [x.lower() for x in out]:   # «Krzyki, Krzyki» → «Krzyki»
            out.append(p)
    return ", ".join(out[:2])


def is_single_room(o):
    """«Pokój do wynajęcia» — комната в общей квартире, а не квартира."""
    t = (o.get("title") or "").lower().strip()
    if re.match(r"(do wynajęcia\s+|wynajmę\s+)?pok[oó]j\b", t):   # заголовок начинается с «Pokój»
        return True
    if re.search(r"\bpok[oó]j\b(?!\w)", t) and not re.search(r"\d\s*-?\s*pok|pokoje|pokojow|dwupok|trzypok|kawalerk", t):
        return True
    return bool(o.get("area_num") and o.get("rooms", 1) >= 2 and o["area_num"] < 18)


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


def olx_page_photos(page_html):
    """OLX прячет фото всех объявлений в данных страницы — достаём их оттуда сразу."""
    photos = {}
    m = re.search(r"window\.__PRERENDERED_STATE__\s*=\s*(\".*?\");\s*(?:window|</script>)", page_html, re.S)
    if not m:
        return photos
    try:
        state = json.loads(json.loads(m.group(1)))
        ads = (((state.get("listing") or {}).get("listing") or {}).get("ads")) or []
        for ad in ads:
            pics = ad.get("photos") or []
            if ad.get("url") and pics:
                first = pics[0] if isinstance(pics[0], str) else (pics[0].get("link") or "")
                first = first.replace("{width}", "1000").replace("{height}", "750")
                if first:
                    photos[ad["url"].split("?")[0].split("#")[0]] = first
    except (ValueError, AttributeError, TypeError) as e:
        print(f"OLX: не прочитались фото из данных страницы — {e}")
    return photos


def get_olx(rooms, max_price):
    r = requests.get(olx_url(rooms, max_price), headers=HEADERS, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    page_photos = olx_page_photos(r.text)
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
            if "no_thumbnail" in photo or "placeholder" in photo:
                photo = ""
        if not photo:
            photo = page_photos.get(url, "")
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
        if ad.get("isPrivateOwner") is False or (isinstance(ad.get("agency"), dict) and ad["agency"].get("name")):
            continue   # агентство — пропускаем
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


def ad_page(url):
    """Открывает объявление: возвращает (текст, главное фото, {czynsz, kaucja} из данных Otodom)."""
    r = requests.get(url, headers=HEADERS, timeout=20)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    og = soup.find("meta", property="og:image")
    photo = og.get("content", "") if og else ""
    parts = []
    extras = {}
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
            if str(ad.get("advertiserType", "")).lower() in ("agency", "developer"):
                extras["agency"] = True
            target = ad.get("target") or {}
            for k, v in (target.items() if isinstance(target, dict) else []):
                if k.lower() in ("rent", "czynsz") and to_int(v):
                    extras.setdefault("czynsz", to_int(v))
                if k.lower() in ("deposit", "kaucja") and to_int(v):
                    extras.setdefault("kaucja", to_int(v))
            chars = ad.get("characteristics") or []
            parts.append(json.dumps(chars, ensure_ascii=False))
            for c in chars:
                if isinstance(c, dict) and c.get("key") in ("rent", "deposit"):
                    n = to_int(c.get("value"))
                    if n:
                        extras["czynsz" if c["key"] == "rent" else "kaucja"] = n
            parts.append(json.dumps(ad.get("features") or [], ensure_ascii=False))
        except (ValueError, AttributeError):
            pass
    full = soup.get_text(" ", strip=True).lower()
    if not any(p.strip() for p in parts):
        parts.append(full)
    if re.search(r"(typ ogłoszeniodawcy|ogłoszeniodawca)\s*:?\s*(biuro nieruchomości|agencja|deweloper)", full):
        extras["agency"] = True
    extras["full"] = full
    return " ".join(parts).lower(), photo, extras


def to_int(value):
    digits = re.sub(r"[^\d]", "", str(value or "").split(",")[0].split(".")[0])
    return int(digits) if digits else None


NUM = r"(\d{1,2}[\s.]?\d{3}|\d{2,4})(?:[,.]\d{2}|[,.]\s?[-–])?\s*(?:zł|zl|pln)"
CZYNSZ_PATTERNS = [
    r"czynsz\s*\(?dodatkowo\)?\s*[:\-–]?\s*" + NUM,
    r"czynsz\s*[:\-–]\s*" + NUM,
    r"czynsz\s*(?:administracyjny|do spółdzielni|do wspólnoty|adm\.?)\s*[:\-–]?\s*(?:ok\.?\s*)?" + NUM,
    r"opłat\w*\s*(?:administracyjn\w*|eksploatacyjn\w*)\s*[:\-–]?\s*(?:ok\.?\s*)?" + NUM,
    r"\+\s*czynsz\s*[:\-–]?\s*(?:ok\.?\s*)?" + NUM,
    r"\+\s*" + NUM + r"\s*(?:czynsz|opłat)",
]
KAUCJA_PATTERNS = [
    r"kaucj\w*[^0-9\n]{0,35}?" + NUM,
    r"depozyt\w*[^0-9\n]{0,25}?" + NUM,
]


def costs_from_text(text, rent, extras):
    """Доп. оплаты (czynsz) и кауция: из данных сайта, иначе ищем в тексте объявления."""
    text = text + " " + extras.get("full", "")
    czynsz = extras.get("czynsz")
    if not czynsz:
        for p in CZYNSZ_PATTERNS:
            m = re.search(p, text)
            if m:
                n = to_int(m.group(1))
                if n and 50 <= n <= 2500 and n != rent:
                    czynsz = n
                    break
    kaucja = extras.get("kaucja")
    if not kaucja:
        for p in KAUCJA_PATTERNS:
            m = re.search(p, text)
            if m:
                n = to_int(m.group(1))
                if n and 300 <= n <= 30000:
                    kaucja = n
                    break
    if not kaucja and rent:   # «kaucja w wysokości jednomiesięcznego czynszu» и т.п.
        m = re.search(r"kaucj\w*[^.\n]{0,50}?(jedno|1[\s-]?|dwu|2[\s-]?|trzy|3[\s-]?)\s*-?\s*miesi", text)
        if m:
            k = {"jedno": 1, "dwu": 2, "trzy": 3}.get(m.group(1).strip(" -"), None) or int(m.group(1).strip(" -"))
            kaucja = rent * k
    return czynsz, kaucja


def pets_from_text(text):
    if any(re.search(p, text) for p in PETS_NO):
        return "no"
    if any(re.search(p, text) for p in PETS_YES):
        return "yes"
    return "unknown"


INFO_VERSION = 4   # поднимаем, когда бот начинает узнавать из объявления что-то новое


def check_ads(urls, info, need_photo, rents, limit=None):
    """Открывает объявления, которых ещё нет в кэше: животные, фото, оплаты, кауция."""
    checked = 0
    for url in urls:
        known = info.get(url)
        if known and known.get("v") == 3 and known.get("kaucja"):
            known["v"] = INFO_VERSION   # кауция уже найдена — открывать заново не нужно
        if known and known.get("v") == INFO_VERSION and (known.get("photo") or url not in need_photo):
            continue
        if limit is not None and checked >= limit:
            break
        try:
            text, photo, extras = ad_page(url)
            czynsz, kaucja = costs_from_text(text, rents.get(url), extras)
            info[url] = {"v": INFO_VERSION, "pets": pets_from_text(text), "photo": photo or "none",
                         "czynsz": czynsz, "kaucja": kaucja, "agency": bool(extras.get("agency"))}
        except Exception as e:
            print(f"Не открылось {url} — {e}")
        checked += 1
        time.sleep(0.7)   # не торопимся, чтобы сайты не блокировали
    return checked


import unicodedata

BIG_DISTRICTS = ["Stare Miasto", "Śródmieście", "Krzyki", "Fabryczna", "Psie Pole"]


def norm(text):
    t = unicodedata.normalize("NFD", (text or "").lower())
    return "".join(c for c in t if unicodedata.category(c) != "Mn").replace("ł", "l")


def user_matches(u, o, rooms):
    """Подходит ли квартира под фильтры человека (та же логика, что в worker.js)."""
    if rooms not in u["rooms"]:
        return False
    pay = o.get("total") or o.get("price_num")
    if not pay:
        return False
    if u.get("minPrice") and pay < u["minPrice"]:
        return False
    if u.get("maxPrice") and pay > u["maxPrice"]:
        return False
    if (u.get("minArea") or u.get("maxArea")) and not o.get("area_num"):
        return False
    if u.get("minArea") and o["area_num"] < u["minArea"]:
        return False
    if u.get("maxArea") and o["area_num"] > u["maxArea"]:
        return False
    if u.get("hideNoPets") and o.get("pets") == "no":
        return False
    if u.get("districts"):
        hay = norm(f"{o.get('district_short', '')} {o.get('title', '')}")
        if not any(norm(d) in hay for d in u["districts"]):
            return False
    return True


MAX_PER_RUN = 3   # если новых больше — присылаем 3 лучших и кнопку «Посмотреть ещё»


def districts_map(offers):
    """Микрорайоны, которые сейчас встречаются в объявлениях: {«Krzyki»: [«Huby», «Gaj», …]}."""
    out = {b: set() for b in BIG_DISTRICTS}
    for o in offers:
        parts = [p.strip() for p in (o.get("district_short") or "").split(",")]
        if len(parts) == 2 and parts[1] in out and parts[0] not in BIG_DISTRICTS:
            out[parts[1]].add(parts[0])
    return {b: sorted(v) for b, v in out.items()}


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
        max_price = max([SNAPSHOT_PRICE] + [u.get("maxPrice") or 20000 for u in wanting])
        for name, getter in SOURCES.items():
            key = f"{name}:{rooms}"
            try:
                offers = getter(rooms, max_price)
            except Exception as e:
                print(f"{key}: ошибка — {e}")
                continue
            offers = [enrich(o, rooms, name) for o in offers if not o.get("featured")]
            offers = [o for o in offers if not is_single_room(o)]   # только целые квартиры
            print(f"{key}: найдено {len(offers)}")
            fetched.append((key, rooms, offers))

    # Проверка здоровья: если сайт 3 раза подряд ничего не отдал — пишем владельцу
    health = seen.get("_health", {})
    for name in SOURCES:
        got = sum(len(offs) for k, r, offs in fetched if k.startswith(name + ":"))
        fails = 0 if got else health.get(name, 0) + 1
        if fails == 3:
            alert(f"⚠️ <b>{name}</b>: 3 проверки подряд (~45 мин) — 0 объявлений.\n"
                  f"Возможно, сайт изменился или заблокировал бота. Открой лог в GitHub Actions.")
        if fails == 0 and health.get(name, 0) >= 3:
            alert(f"✅ <b>{name}</b> снова работает.")
        health[name] = fails
    seen["_health"] = health

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

    # 3) Открываем объявления: животные + фото. Сначала все новые, потом понемногу старые
    info = {}
    if os.path.exists(INFO_FILE):
        with open(INFO_FILE, encoding="utf-8") as f:
            info = json.load(f)
    elif os.path.exists(OLD_PETS_FILE):   # переносим то, что узнали раньше (без фото)
        with open(OLD_PETS_FILE, encoding="utf-8") as f:
            info = {u: {"pets": v, "photo": ""} for u, v in json.load(f).items()}
    new_urls, other_urls = [], []
    for key, rooms, offers in fetched:
        old = set(seen.get(key, []))
        for o in offers:
            (new_urls if (key in seen and o["id"] not in old) else other_urls).append(o["url"])
    # объявления без фото проверяем в первую очередь
    need_photo = {o["url"] for _, _, offs in fetched for o in offs if not o.get("photo")}
    other_urls.sort(key=lambda u: 0 if u in need_photo else 1)
    rents = {o["url"]: o.get("price_num") for _, _, offs in fetched for o in offs}
    n1 = check_ads(dict.fromkeys(new_urls), info, need_photo, rents)
    # сначала объявления без фото, потом остальные
    n2 = check_ads(dict.fromkeys(u for u in other_urls if u in need_photo), info, need_photo, rents, limit=200)
    n2 += check_ads(dict.fromkeys(other_urls), info, need_photo, rents, limit=PETS_PER_RUN)
    print(f"Объявления: открыто новых {n1}, старых {n2}, всего известно {len(info)}")
    for _, _, offers in fetched:
        for o in offers:
            known = info.get(o["url"]) or {}
            o["pets"] = known.get("pets", "unknown")
            o["agency"] = known.get("agency", False)
            o["czynsz"] = known.get("czynsz")
            o["kaucja"] = known.get("kaucja")
            if o.get("price_num"):
                o["total"] = o["price_num"] + (o["czynsz"] or 0)
            if not o.get("photo") and known.get("photo") not in (None, "", "none"):
                o["photo"] = known["photo"]
    with open(INFO_FILE, "w", encoding="utf-8") as f:
        json.dump(dict(list(info.items())[-3000:]), f, ensure_ascii=False)

    # 4) Новые объявления → подписчикам по их фильтрам
    to_send = {u["id"]: [] for u in users}
    for key, rooms, offers in fetched:
        first_run = key not in seen
        old = set(seen.get(key, []))
        new = [o for o in offers if o["id"] not in old]
        if not first_run:
            for o in new:
                if not o.get("fresh", True) or o["sig"] in sigs or o.get("agency"):
                    continue
                sigs.add(o["sig"])
                for u in users:   # u = один поиск человека (у человека может быть до 5)
                    if user_matches(u, o, rooms) and all(x["url"] != o["url"] for x in to_send[u["id"]]):
                        to_send[u["id"]].append(dict(o, search_name=u.get("search", "")))
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
                        and not lst[i].get("agency") \
                        and (minutes_ago(lst[i].get("posted_at")) or 0) < 60 * 24 * 60:
                    snap_sigs.add(lst[i]["sig"])
                    snapshot.append(lst[i])
    with open(SNAPSHOT_FILE, "w", encoding="utf-8") as f:
        json.dump({"updated": datetime.now(WARSAW).isoformat(), "avg": avg,
                   "districts": districts_map(snapshot), "offers": snapshot}, f, ensure_ascii=False)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        alert(f"❌ <b>Бот упал</b> при проверке квартир:\n<code>{html.escape(str(e))[:300]}</code>\n"
              f"Подробности — в логе GitHub Actions.")
        raise
