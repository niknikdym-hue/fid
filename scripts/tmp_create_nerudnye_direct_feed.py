#!/usr/bin/env python3
from __future__ import annotations

import html
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse, parse_qs
import xml.etree.ElementTree as ET

import requests
from bs4 import BeautifulSoup, Tag

BASE = "https://akbarskeramic.ru"
CATEGORY_URL = f"{BASE}/catalog/nerudnye-materialy/"
OUT = Path("akbars_nerudnye_materialy_yandex_direct_feed.yml")
PRODUCT_RE = re.compile(r"/catalog/product/(\d+)-", re.I)
PRICE_RE = re.compile(r"(?<!\d)(\d{1,7}(?:[\s\u00a0\u202f]\d{3})*(?:[.,]\d{1,2})?)\s*₽")
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; AkBarsDirectFeed/1.0; +https://github.com/niknikdym-hue/fid)",
    "Accept-Language": "ru-RU,ru;q=0.9",
}


def clean(s: str | None) -> str:
    if not s:
        return ""
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def get(url: str) -> requests.Response:
    last = None
    for attempt in range(1, 5):
        try:
            r = requests.get(url, headers=HEADERS, timeout=(15, 60))
            r.raise_for_status()
            return r
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt < 4:
                time.sleep(attempt)
    raise RuntimeError(f"Не удалось загрузить {url}: {last}")


def canonical_product_url(href: str) -> str | None:
    u = urljoin(BASE, href)
    p = urlparse(u)
    if p.netloc != urlparse(BASE).netloc or not PRODUCT_RE.search(p.path):
        return None
    return f"{p.scheme}://{p.netloc}{p.path}"


def product_id(url: str) -> str:
    m = PRODUCT_RE.search(urlparse(url).path)
    return m.group(1) if m else ""


def page_count(soup: BeautifulSoup) -> tuple[str | None, int]:
    candidates: dict[str, set[int]] = {}
    for a in soup.find_all("a", href=True):
        qs = parse_qs(urlparse(str(a.get("href"))).query)
        for key, vals in qs.items():
            if key.startswith("PAGEN_"):
                for v in vals:
                    if v.isdigit():
                        candidates.setdefault(key, set()).add(int(v))
    if not candidates:
        return None, 1
    key = max(candidates, key=lambda k: max(candidates[k]))
    return key, max(candidates[key])


def find_card(anchor: Tag) -> Tag | None:
    node: Tag | None = anchor
    best = None
    for _ in range(10):
        parent = node.parent if node else None
        if not isinstance(parent, Tag):
            break
        node = parent
        txt = clean(node.get_text(" ", strip=True))
        if "₽" in txt and len(txt) < 2500:
            best = node
            classes = " ".join(node.get("class", []))
            if re.search(r"(?:product|catalog|item|card)", classes, re.I):
                return node
    return best


def extract_price(card: Tag | None) -> float | None:
    if card is None:
        return None
    txt = clean(card.get_text(" ", strip=True))
    m = PRICE_RE.search(txt)
    if not m:
        return None
    raw = re.sub(r"[\s\u00a0\u202f]", "", m.group(1)).replace(",", ".")
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if value > 0 else None


def img_from(node: Tag | BeautifulSoup | None) -> str:
    if node is None:
        return ""
    img = node.find("img")
    if not img:
        return ""
    raw = str(img.get("data-src") or img.get("data-lazy") or img.get("src") or "")
    if not raw or raw.startswith("data:"):
        return ""
    return urljoin(BASE, raw)


def listing_products(page_html: str) -> dict[str, dict[str, object]]:
    soup = BeautifulSoup(page_html, "html.parser")
    out: dict[str, dict[str, object]] = {}
    for a in soup.find_all("a", href=True):
        url = canonical_product_url(str(a.get("href")))
        if not url:
            continue
        pid = product_id(url)
        card = find_card(a)
        price = extract_price(card)
        if price is None:
            continue
        title = clean(a.get_text(" ", strip=True))
        if card:
            h = card.find(["h2", "h3", "h4"])
            if h:
                title = clean(h.get_text(" ", strip=True)) or title
        if not title:
            continue
        text = clean(card.get_text(" ", strip=True)) if card else ""
        vendor = ""
        if "СК Кама" in text:
            vendor = "СК Кама"
        elif "Качканар" in text:
            vendor = "Качканар"
        current = out.get(pid)
        item = {"id": pid, "url": url, "title": title, "price": price, "vendor": vendor, "picture": img_from(card)}
        if current is None or len(str(item["title"])) > len(str(current["title"])):
            out[pid] = item
    return out


def enrich(item: dict[str, object]) -> dict[str, object]:
    r = get(str(item["url"]))
    soup = BeautifulSoup(r.text, "html.parser")
    h1 = soup.find("h1")
    if h1:
        item["title"] = clean(h1.get_text(" ", strip=True)) or item["title"]
    og = soup.find("meta", attrs={"property": "og:image"})
    if og and og.get("content"):
        item["picture"] = urljoin(BASE, str(og.get("content")))
    if not item.get("picture"):
        item["picture"] = img_from(soup)

    # Collect compact characteristics without trusting any price from the detail page.
    params: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for li in soup.find_all("li"):
        txt = clean(li.get_text(" ", strip=True))
        if not txt or len(txt) > 180:
            continue
        m = re.match(r"^([^:]{2,60})[:\s]+(.{1,100})$", txt)
        if not m:
            continue
        key, val = clean(m.group(1)), clean(m.group(2))
        if key.lower() in {"характеристики", "каталог", "клиентам", "партнёрам"}:
            continue
        pair = (key, val)
        if pair not in seen and any(k in key.lower() for k in ("фракц", "марка", "мороз", "лещад", "плотн", "процент", "содержание")):
            seen.add(pair)
            params.append(pair)
        if len(params) >= 8:
            break
    item["params"] = params
    return item


def add_text(parent: ET.Element, tag: str, text: object) -> ET.Element:
    el = ET.SubElement(parent, tag)
    el.text = str(text)
    return el


def main() -> None:
    first = get(CATEGORY_URL)
    first_soup = BeautifulSoup(first.text, "html.parser")
    page_key, pages = page_count(first_soup)
    all_items = listing_products(first.text)
    print(f"page 1/{pages}: {len(all_items)} priced products")
    for n in range(2, pages + 1):
        sep = "&" if "?" in CATEGORY_URL else "?"
        url = f"{CATEGORY_URL}{sep}{page_key}={n}" if page_key else CATEGORY_URL
        found = listing_products(get(url).text)
        before = len(all_items)
        all_items.update(found)
        print(f"page {n}/{pages}: +{len(all_items)-before}")

    if not all_items:
        raise SystemExit("ERROR: no priced products found")

    enriched = []
    for idx, item in enumerate(sorted(all_items.values(), key=lambda x: int(str(x["id"]))), 1):
        enriched.append(enrich(item))
        print(f"detail {idx}/{len(all_items)}: {item['id']} {item['title']} — {item['price']}")
        time.sleep(0.05)

    root = ET.Element("yml_catalog", {"date": datetime.now().strftime("%Y-%m-%d %H:%M")})
    shop = ET.SubElement(root, "shop")
    add_text(shop, "name", "Ак Барс Керамик")
    add_text(shop, "company", "Ак Барс Керамик")
    add_text(shop, "url", BASE)
    currencies = ET.SubElement(shop, "currencies")
    ET.SubElement(currencies, "currency", {"id": "RUB", "rate": "1"})
    categories = ET.SubElement(shop, "categories")
    add_text(categories, "category", "Нерудные материалы").set("id", "1")
    offers = ET.SubElement(shop, "offers")

    for item in enriched:
        offer = ET.SubElement(offers, "offer", {"id": str(item["id"]), "available": "true"})
        add_text(offer, "url", item["url"])
        price = float(item["price"])
        add_text(offer, "price", f"{price:.2f}")
        add_text(offer, "currencyId", "RUB")
        add_text(offer, "categoryId", "1")
        if item.get("picture"):
            add_text(offer, "picture", item["picture"])
        add_text(offer, "name", item["title"])
        add_text(offer, "typePrefix", "Нерудные материалы")
        if item.get("vendor"):
            add_text(offer, "vendor", item["vendor"])
        add_text(offer, "model", item["title"])
        add_text(offer, "pickup", "true")
        add_text(offer, "delivery", "true")
        add_text(offer, "sales_notes", "Цена за 1 тонну. Самовывоз и доставка доступны по согласованию.")
        desc = f"{item['title']}. Цена — {price:g} руб./т."
        params = item.get("params") or []
        if params:
            desc += " " + ". ".join(f"{k}: {v}" for k, v in params) + "."
        add_text(offer, "description", desc)
        add_text(offer, "param", "т").set("name", "Единица измерения")
        for key, val in params:
            add_text(offer, "param", val).set("name", key)

    ET.indent(root, space="  ")
    ET.ElementTree(root).write(OUT, encoding="utf-8", xml_declaration=True)

    # Final validation.
    check = ET.parse(OUT).getroot()
    offers_check = check.findall("./shop/offers/offer")
    ids = [o.get("id", "") for o in offers_check]
    if len(ids) != len(set(ids)):
        raise SystemExit("ERROR: duplicate offer IDs")
    for o in offers_check:
        p = o.findtext("price", "0")
        if float(p) <= 0:
            raise SystemExit(f"ERROR: invalid price for {o.get('id')}")
        if o.findtext("categoryId") != "1":
            raise SystemExit(f"ERROR: invalid category for {o.get('id')}")
        if "/catalog/product/" not in o.findtext("url", ""):
            raise SystemExit(f"ERROR: invalid URL for {o.get('id')}")
    print(f"OK: wrote {OUT} with {len(offers_check)} offers")


if __name__ == "__main__":
    main()
