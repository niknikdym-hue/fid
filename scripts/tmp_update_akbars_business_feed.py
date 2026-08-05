#!/usr/bin/env python3
from __future__ import annotations

import copy
import html
import json
import re
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse
import xml.etree.ElementTree as ET

import requests
from bs4 import BeautifulSoup, Tag

BASE_URL = "https://akbarskeramic.ru"
FEED_PATH = Path("akbars_yandex_business_feed.yml")
REPORT_PATH = Path("tmp-akbars-business-feed-update-report.json")
CATEGORY_PAGES = {
    "brick": f"{BASE_URL}/catalog/kirpich/",
    "103": f"{BASE_URL}/catalog/keramogranit/",
    "104": f"{BASE_URL}/catalog/bruschatka/",
    "105": f"{BASE_URL}/catalog/nerudnye-materialy/",
}
EXPECTED_CATEGORIES = {
    "101": "Кирпич",
    "102": "Блоки",
    "103": "Керамогранит",
    "104": "Тротуарная плитка",
    "105": "Нерудные материалы",
}
JBI_RE = re.compile(r"(?:железобетон|\bжби\b|сваи|плит[аы] перекрытия|\bфбс\b|кольц[ао] колод)", re.I)
NON_BRICK_RE = re.compile(r"(?:\bбетон\b|\bпесок\b|\bщебень\b|\bгравий\b|\bопгс\b|\bпгс\b|сваи|плит[аы] перекрытия)", re.I)
BLOCK_RE = re.compile(r"(?:\bблок\b|kerakam|керамкам|кетра|крупноформат)", re.I)
PRODUCT_RE = re.compile(r"/catalog/product/(\d+)-", re.I)
PRICE_RE = re.compile(r"(?<!\d)(\d[\d\s\xa0]*(?:[.,]\d{1,2})?)\s*₽")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; AkBarsBusinessFeedUpdater/1.0; +https://github.com/niknikdym-hue/fid)",
    "Accept-Language": "ru-RU,ru;q=0.9",
}
SESSION = requests.Session()
SESSION.headers.update(HEADERS)


@dataclass
class ListingProduct:
    product_id: str
    url: str
    title: str
    vendor: str
    price: float
    picture: str
    source_group: str


def clean(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def request(url: str, attempts: int = 5) -> requests.Response:
    error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            response = SESSION.get(url, timeout=(15, 60))
            response.raise_for_status()
            return response
        except Exception as exc:  # noqa: BLE001
            error = exc
            if attempt < attempts:
                time.sleep(attempt * 1.5)
    raise RuntimeError(f"Не удалось загрузить {url}: {error}")


def product_id_from_url(url: str) -> str:
    match = PRODUCT_RE.search(urlparse(url).path)
    return match.group(1) if match else ""


def canonical_product_url(href: str) -> str | None:
    url = urljoin(BASE_URL, href)
    parsed = urlparse(url)
    if parsed.netloc != urlparse(BASE_URL).netloc or not PRODUCT_RE.search(parsed.path):
        return None
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"


def normalize_picture(value: str) -> str:
    if not value or value.startswith("data:"):
        return ""
    url = urljoin(BASE_URL, value)
    match = re.search(r"(/upload/)resize_cache/(iblock/[^/]+/[^/]+)/[^/]+/([^?#]+)", url, re.I)
    if match:
        return urljoin(BASE_URL, f"{match.group(1)}{match.group(2)}/{match.group(3)}")
    return url


def parse_price(value: str) -> float | None:
    match = PRICE_RE.search(value)
    if not match:
        return None
    raw = match.group(1).replace("\xa0", "").replace(" ", "").replace(",", ".")
    try:
        price = float(raw)
    except ValueError:
        return None
    return price if price > 0 else None


def find_card(anchor: Tag) -> Tag | None:
    node: Tag | None = anchor
    for _ in range(9):
        parent = node.parent if node else None
        if not isinstance(parent, Tag):
            break
        node = parent
        text = clean(node.get_text(" ", strip=True))
        if "₽" in text and len(text) < 2500:
            return node
    return None


def extract_vendor(card: Tag, title: str, old_vendor: str) -> str:
    for selector in ('[class*="brand"]', '[class*="vendor"]', '[class*="manufacturer"]'):
        candidate = card.select_one(selector)
        if candidate:
            value = clean(candidate.get_text(" ", strip=True))
            if value and value != title and "₽" not in value:
                return value
    if old_vendor:
        return old_vendor
    strings = [clean(x) for x in card.stripped_strings]
    for value in strings:
        if not value or value == title or "₽" in value or value.lower() in {"шт", "м2", "м²", "т", "кг"}:
            continue
        if len(value) <= 80 and not value.isdigit():
            return value
    return "Ак Барс Керамик"


def extract_listing_products(page_html: str, source_group: str, old_by_id: dict[str, ET.Element]) -> dict[str, ListingProduct]:
    soup = BeautifulSoup(page_html, "html.parser")
    products: dict[str, ListingProduct] = {}
    for anchor in soup.find_all("a", href=True):
        url = canonical_product_url(str(anchor.get("href")))
        if not url:
            continue
        pid = product_id_from_url(url)
        card = find_card(anchor)
        if not card:
            continue
        card_text = clean(card.get_text(" ", strip=True))
        price = parse_price(card_text)
        if price is None:
            continue
        heading = card.find(["h2", "h3", "h4"])
        title = clean(heading.get_text(" ", strip=True) if heading else anchor.get_text(" ", strip=True))
        if not title:
            same_links = card.find_all("a", href=True)
            title = max((clean(a.get_text(" ", strip=True)) for a in same_links if canonical_product_url(str(a.get("href"))) == url), key=len, default="")
        if not title:
            continue
        old = old_by_id.get(pid)
        old_vendor = child_text(old, "vendor") if old is not None else ""
        vendor = extract_vendor(card, title, old_vendor)
        picture = ""
        image = card.find("img")
        if image:
            picture = normalize_picture(str(image.get("data-src") or image.get("data-lazy") or image.get("src") or ""))
        existing = products.get(pid)
        item = ListingProduct(pid, url, title, vendor, price, picture, source_group)
        if existing is None or len(item.title) > len(existing.title):
            products[pid] = item
    return products


def pagination(page_html: str) -> tuple[str | None, int]:
    soup = BeautifulSoup(page_html, "html.parser")
    values: dict[str, set[int]] = {}
    for anchor in soup.find_all("a", href=True):
        query = parse_qs(urlparse(str(anchor.get("href"))).query)
        for key, raw_values in query.items():
            if not key.startswith("PAGEN_"):
                continue
            for raw in raw_values:
                if raw.isdigit():
                    values.setdefault(key, set()).add(int(raw))
    if not values:
        return None, 1
    key = max(values, key=lambda item: max(values[item]))
    return key, min(max(values[key]), 200)


def collect_category(url: str, source_group: str, old_by_id: dict[str, ET.Element]) -> dict[str, ListingProduct]:
    first = request(url)
    key, max_page = pagination(first.text)
    result = extract_listing_products(first.text, source_group, old_by_id)
    print(f"{source_group}: страниц {max_page}; страница 1 — {len(result)} товаров")
    for page in range(2, max_page + 1):
        separator = "&" if "?" in url else "?"
        page_url = f"{url}{separator}{key}={page}" if key else url
        response = request(page_url)
        found = extract_listing_products(response.text, source_group, old_by_id)
        before = len(result)
        result.update(found)
        print(f"{source_group}: страница {page}/{max_page} — новых {len(result) - before}")
        time.sleep(0.05)
    return result


def child_text(element: ET.Element | None, tag: str) -> str:
    if element is None:
        return ""
    child = element.find(tag)
    return clean(child.text if child is not None else "")


def set_child(element: ET.Element, tag: str, value: str) -> None:
    child = element.find(tag)
    if child is None:
        child = ET.SubElement(element, tag)
    child.text = value


def json_ld_product(soup: BeautifulSoup) -> dict:
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.string or script.get_text(" ", strip=True)
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        candidates: list[dict] = []
        if isinstance(data, list):
            candidates = [item for item in data if isinstance(item, dict)]
        elif isinstance(data, dict) and isinstance(data.get("@graph"), list):
            candidates = [item for item in data["@graph"] if isinstance(item, dict)]
        elif isinstance(data, dict):
            candidates = [data]
        for item in candidates:
            item_type = item.get("@type")
            if item_type == "Product" or (isinstance(item_type, list) and "Product" in item_type):
                return item
    return {}


def product_details(url: str) -> tuple[str, str, str, str]:
    soup = BeautifulSoup(request(url).text, "html.parser")
    page_text = clean(soup.get_text(" ", strip=True))
    product_type = ""
    match = re.search(r"(?:^|\s)Тип\s+(Лицевой|Кладочный|Блоки)(?:\s|$)", page_text, re.I)
    if match:
        product_type = match.group(1).lower()
    data = json_ld_product(soup)
    vendor = ""
    brand = data.get("brand") if isinstance(data, dict) else None
    if isinstance(brand, dict):
        vendor = clean(str(brand.get("name", "")))
    elif isinstance(brand, str):
        vendor = clean(brand)
    image = data.get("image") if isinstance(data, dict) else None
    if isinstance(image, list) and image:
        image = image[0]
    if isinstance(image, dict):
        image = image.get("url")
    picture = normalize_picture(str(image or ""))
    if not picture:
        meta = soup.find("meta", property="og:image")
        picture = normalize_picture(str(meta.get("content", ""))) if meta else ""
    description = clean(str(data.get("description", ""))) if isinstance(data, dict) else ""
    return product_type, vendor, picture, description


def category_for_product(item: ListingProduct, old_category: str) -> str | None:
    if item.source_group != "brick":
        return item.source_group
    if old_category in {"101", "102"}:
        return old_category
    if JBI_RE.search(item.title) or NON_BRICK_RE.search(item.title):
        return None
    product_type, vendor, picture, _ = product_details(item.url)
    if vendor and item.vendor == "Ак Барс Керамик":
        item.vendor = vendor
    if picture and not item.picture:
        item.picture = picture
    if product_type == "блоки" or BLOCK_RE.search(item.title):
        return "102"
    if product_type in {"лицевой", "кладочный"}:
        return "101"
    return None


def create_offer(item: ListingProduct, category_id: str, old: ET.Element | None) -> ET.Element:
    if old is not None:
        offer = copy.deepcopy(old)
    else:
        offer = ET.Element("offer", {"id": item.product_id})
    offer.set("id", item.product_id)
    set_child(offer, "name", item.title)
    set_child(offer, "vendor", item.vendor or child_text(old, "vendor") or "Ак Барс Керамик")
    set_child(offer, "price", f"{item.price:.2f}")
    set_child(offer, "currencyId", "RUB")
    set_child(offer, "categoryId", category_id)
    picture = item.picture or child_text(old, "picture")
    set_child(offer, "picture", picture)
    old_description = child_text(old, "description")
    old_short = child_text(old, "shortDescription")
    description = old_description or f"{item.title}. Производитель: {item.vendor or 'Ак Барс Керамик'}."
    short_description = old_short or item.title
    set_child(offer, "description", description)
    set_child(offer, "shortDescription", short_description)
    set_child(offer, "url", item.url)

    order = ["name", "vendor", "price", "currencyId", "categoryId", "picture", "description", "shortDescription", "url"]
    children = {child.tag: child for child in list(offer)}
    for child in list(offer):
        offer.remove(child)
    for tag in order:
        child = children.get(tag)
        if child is None:
            child = ET.Element(tag)
        offer.append(child)
    return offer


def indent(element: ET.Element, level: int = 0) -> None:
    space = "\n" + "  " * level
    if len(element):
        if not element.text or not element.text.strip():
            element.text = space + "  "
        for child in element:
            indent(child, level + 1)
        if not element[-1].tail or not element[-1].tail.strip():
            element[-1].tail = space
    if level and (not element.tail or not element.tail.strip()):
        element.tail = space


def main() -> int:
    tree = ET.parse(FEED_PATH)
    root = tree.getroot()
    categories = {c.get("id", ""): clean(c.text) for c in root.findall("./shop/categories/category")}
    if categories != EXPECTED_CATEGORIES:
        raise RuntimeError(f"Неожиданный набор категорий: {categories}")
    offers_parent = root.find("./shop/offers")
    if offers_parent is None:
        raise RuntimeError("В фиде не найден элемент offers")
    old_offers = list(offers_parent.findall("offer"))
    old_by_id = {offer.get("id", ""): offer for offer in old_offers if offer.get("id")}
    old_category = {pid: child_text(offer, "categoryId") for pid, offer in old_by_id.items()}

    all_products: dict[str, ListingProduct] = {}
    for source_group, url in CATEGORY_PAGES.items():
        found = collect_category(url, source_group, old_by_id)
        for pid, item in found.items():
            if pid in all_products:
                print(f"DUPLICATE LISTING {pid}: {all_products[pid].url} / {item.url}")
            all_products[pid] = item

    final: list[tuple[str, ListingProduct]] = []
    skipped_jbi: list[str] = []
    skipped_unclassified: list[str] = []
    for pid, item in all_products.items():
        haystack = f"{item.title} {item.url}"
        if JBI_RE.search(haystack) or "/catalog/zhbi/" in item.url:
            skipped_jbi.append(pid)
            continue
        cid = category_for_product(item, old_category.get(pid, ""))
        if cid not in EXPECTED_CATEGORIES:
            skipped_unclassified.append(pid)
            continue
        final.append((cid, item))

    final.sort(key=lambda pair: (int(pair[0]), int(pair[1].product_id)))
    offers_parent.clear()
    counts: Counter[str] = Counter()
    new_ids: list[str] = []
    for cid, item in final:
        old = old_by_id.get(item.product_id)
        if old is None:
            new_ids.append(item.product_id)
        offers_parent.append(create_offer(item, cid, old))
        counts[cid] += 1

    removed_ids = sorted(set(old_by_id) - {item.product_id for _, item in final}, key=lambda x: int(x) if x.isdigit() else x)
    if len(final) < 500:
        raise RuntimeError(f"Защитная остановка: слишком мало товаров — {len(final)}")
    if any(counts[cid] == 0 for cid in EXPECTED_CATEGORIES):
        raise RuntimeError(f"Защитная остановка: пустая категория — {dict(counts)}")

    root.attrib.pop("date", None)
    indent(root)
    tree.write(FEED_PATH, encoding="utf-8", xml_declaration=True)
    ET.parse(FEED_PATH)

    report = {
        "old_offer_count": len(old_offers),
        "collected_listing_count": len(all_products),
        "new_offer_count": len(final),
        "counts": {cid: counts[cid] for cid in EXPECTED_CATEGORIES},
        "new_ids_count": len(new_ids),
        "new_ids_first_100": new_ids[:100],
        "removed_ids_count": len(removed_ids),
        "removed_ids_first_100": removed_ids[:100],
        "skipped_jbi_count": len(skipped_jbi),
        "skipped_jbi_ids": skipped_jbi,
        "skipped_unclassified_count": len(skipped_unclassified),
        "skipped_unclassified_ids_first_100": skipped_unclassified[:100],
        "source_pages": CATEGORY_PAGES,
    }
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
