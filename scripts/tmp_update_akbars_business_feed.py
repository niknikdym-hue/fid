#!/usr/bin/env python3
from __future__ import annotations

import copy
import html
import json
import re
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
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
GROUP_PATHS = {
    "brick": "/catalog/kirpich/",
    "103": "/catalog/keramogranit/",
    "104": "/catalog/bruschatka/",
    "105": "/catalog/nerudnye-materialy/",
}
JBI_RE = re.compile(r"(?:железобетон|\bжби\b|сваи|плит[аы] перекрытия|\bфбс\b|кольц[ао] колод)", re.I)
NON_BRICK_RE = re.compile(r"(?:\bбетон\b|\bпесок\b|\bщебень\b|\bгравий\b|\bопгс\b|\bпгс\b|сваи|плит[аы] перекрытия)", re.I)
BLOCK_RE = re.compile(r"(?:\bблок\b|kerakam|керакам|кетра|крупноформат)", re.I)
PRODUCT_RE = re.compile(r"/catalog/product/(\d+)-", re.I)
PRICE_RE = re.compile(r"(?<!\d)(\d{1,6}(?:[ \u00a0]\d{3})*(?:[.,]\d{1,2})?)\s*₽")
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; AkBarsBusinessFeedUpdater/2.0; +https://github.com/niknikdym-hue/fid)",
    "Accept-Language": "ru-RU,ru;q=0.9",
}


@dataclass
class Seed:
    product_id: str
    url: str
    title: str
    vendor: str
    picture: str
    source_group: str


@dataclass
class Product:
    product_id: str
    url: str
    title: str
    vendor: str
    price: float
    picture: str
    description: str
    source_group: str
    detail_group: str
    product_type: str


def clean(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def get(url: str, attempts: int = 5) -> requests.Response:
    error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            response = requests.get(url, headers=HEADERS, timeout=(15, 60))
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


def child_text(element: ET.Element | None, tag: str) -> str:
    if element is None:
        return ""
    child = element.find(tag)
    return clean(child.text if child is not None else "")


def find_card(anchor: Tag) -> Tag | None:
    node: Tag | None = anchor
    for _ in range(9):
        parent = node.parent if node else None
        if not isinstance(parent, Tag):
            break
        node = parent
        classes = " ".join(node.get("class", []))
        if re.search(r"(?:product|catalog|item|card)", classes, re.I):
            return node
        if node.find(["h2", "h3", "h4"]) and len(clean(node.get_text(" ", strip=True))) < 2500:
            return node
    return None


def listing_seed(anchor: Tag, url: str, source_group: str, old_by_id: dict[str, ET.Element]) -> Seed:
    pid = product_id_from_url(url)
    card = find_card(anchor)
    title = clean(anchor.get_text(" ", strip=True))
    vendor = child_text(old_by_id.get(pid), "vendor")
    picture = child_text(old_by_id.get(pid), "picture")
    if card is not None:
        heading = card.find(["h2", "h3", "h4"])
        if heading:
            title = clean(heading.get_text(" ", strip=True)) or title
        if not title:
            title = max(
                (clean(a.get_text(" ", strip=True)) for a in card.find_all("a", href=True) if canonical_product_url(str(a.get("href"))) == url),
                key=len,
                default="",
            )
        if not picture:
            image = card.find("img")
            if image:
                picture = normalize_picture(str(image.get("data-src") or image.get("data-lazy") or image.get("src") or ""))
        if not vendor:
            for selector in ('[class*="brand"]', '[class*="vendor"]', '[class*="manufacturer"]'):
                candidate = card.select_one(selector)
                if candidate:
                    value = clean(candidate.get_text(" ", strip=True))
                    if value and value != title:
                        vendor = value
                        break
    return Seed(pid, url, title, vendor, picture, source_group)


def pagination(page_html: str) -> tuple[str | None, int]:
    soup = BeautifulSoup(page_html, "html.parser")
    values: dict[str, set[int]] = {}
    for anchor in soup.find_all("a", href=True):
        query = parse_qs(urlparse(str(anchor.get("href"))).query)
        for key, raw_values in query.items():
            if key.startswith("PAGEN_"):
                for raw in raw_values:
                    if raw.isdigit():
                        values.setdefault(key, set()).add(int(raw))
    if not values:
        return None, 1
    key = max(values, key=lambda item: max(values[item]))
    return key, min(max(values[key]), 200)


def extract_page_seeds(page_html: str, source_group: str, old_by_id: dict[str, ET.Element]) -> dict[str, Seed]:
    soup = BeautifulSoup(page_html, "html.parser")
    result: dict[str, Seed] = {}
    for anchor in soup.find_all("a", href=True):
        url = canonical_product_url(str(anchor.get("href")))
        if not url:
            continue
        seed = listing_seed(anchor, url, source_group, old_by_id)
        old = result.get(seed.product_id)
        if old is None or len(seed.title) > len(old.title):
            result[seed.product_id] = seed
    return result


def collect_category(url: str, source_group: str, old_by_id: dict[str, ET.Element]) -> dict[str, Seed]:
    first = get(url)
    key, max_page = pagination(first.text)
    result = extract_page_seeds(first.text, source_group, old_by_id)
    print(f"{source_group}: страниц {max_page}; страница 1 — {len(result)} ссылок")
    for page in range(2, max_page + 1):
        separator = "&" if "?" in url else "?"
        page_url = f"{url}{separator}{key}={page}" if key else url
        found = extract_page_seeds(get(page_url).text, source_group, old_by_id)
        before = len(result)
        result.update(found)
        print(f"{source_group}: страница {page}/{max_page} — новых {len(result) - before}")
        time.sleep(0.03)
    return result


def json_ld_product(soup: BeautifulSoup) -> dict:
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.string or script.get_text(" ", strip=True)
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(data, list):
            candidates = [item for item in data if isinstance(item, dict)]
        elif isinstance(data, dict) and isinstance(data.get("@graph"), list):
            candidates = [item for item in data["@graph"] if isinstance(item, dict)]
        elif isinstance(data, dict):
            candidates = [data]
        else:
            candidates = []
        for item in candidates:
            item_type = item.get("@type")
            if item_type == "Product" or (isinstance(item_type, list) and "Product" in item_type):
                return item
    return {}


def numeric_price(raw: object) -> float | None:
    if raw in (None, ""):
        return None
    match = re.search(r"\d{1,6}(?:[ \u00a0]\d{3})*(?:[.,]\d{1,2})?", str(raw))
    if not match:
        return None
    try:
        value = float(match.group(0).replace(" ", "").replace("\u00a0", "").replace(",", "."))
    except ValueError:
        return None
    return value if value > 0 else None


def parse_price(soup: BeautifulSoup, data: dict) -> float | None:
    offers = data.get("offers") if isinstance(data, dict) else None
    if isinstance(offers, list):
        offers = next((item for item in offers if isinstance(item, dict)), None)
    if isinstance(offers, dict):
        for key in ("price", "lowPrice"):
            price = numeric_price(offers.get(key))
            if price:
                return price
    for tag in soup.select('[itemprop="price"], meta[property="product:price:amount"]'):
        price = numeric_price(tag.get("content") or tag.get("value") or tag.get_text(" ", strip=True))
        if price:
            return price
    h1 = soup.find("h1")
    if h1:
        nearby: list[str] = []
        for element in h1.next_elements:
            if isinstance(element, str):
                token = clean(element)
                if not token:
                    continue
                if token == "Характеристики":
                    break
                nearby.append(token)
                if len(nearby) >= 100:
                    break
        for raw in PRICE_RE.findall(clean(" ".join(nearby))):
            price = numeric_price(raw)
            if price:
                return price
    page_text = clean(soup.get_text(" ", strip=True))
    if h1:
        title = clean(h1.get_text(" ", strip=True))
        if title and title in page_text:
            page_text = page_text.split(title, 1)[1]
    for raw in PRICE_RE.findall(page_text):
        price = numeric_price(raw)
        if price:
            return price
    return None


def detect_detail_group(soup: BeautifulSoup) -> str:
    paths = {urlparse(urljoin(BASE_URL, str(a.get("href", "")))).path for a in soup.find_all("a", href=True)}
    for group, expected_path in GROUP_PATHS.items():
        if expected_path in paths:
            return group
    return ""


def detect_type(soup: BeautifulSoup) -> str:
    tokens = [clean(value) for value in soup.stripped_strings]
    for index, value in enumerate(tokens):
        if value == "Тип" and index + 1 < len(tokens):
            candidate = tokens[index + 1].lower()
            if candidate in {"лицевой", "кладочный", "блоки"}:
                return candidate
        match = re.match(r"^Тип\s+(Лицевой|Кладочный|Блоки)$", value, re.I)
        if match:
            return match.group(1).lower()
    return ""


def detail_product(seed: Seed, old: ET.Element | None) -> Product | None:
    soup = BeautifulSoup(get(seed.url).text, "html.parser")
    h1 = soup.find("h1")
    title = clean(h1.get_text(" ", strip=True) if h1 else seed.title)
    if not title:
        return None
    data = json_ld_product(soup)
    price = parse_price(soup, data)
    if price is None:
        return None
    vendor = seed.vendor or child_text(old, "vendor")
    brand = data.get("brand") if isinstance(data, dict) else None
    if isinstance(brand, dict):
        vendor = clean(str(brand.get("name", ""))) or vendor
    elif isinstance(brand, str):
        vendor = clean(brand) or vendor
    picture = seed.picture or child_text(old, "picture")
    image = data.get("image") if isinstance(data, dict) else None
    if isinstance(image, list) and image:
        image = image[0]
    if isinstance(image, dict):
        image = image.get("url")
    if image:
        picture = normalize_picture(str(image)) or picture
    if not picture:
        meta = soup.find("meta", property="og:image")
        if meta:
            picture = normalize_picture(str(meta.get("content", "")))
    description = clean(str(data.get("description", ""))) if isinstance(data, dict) else ""
    return Product(
        seed.product_id,
        seed.url,
        title,
        vendor or "Ак Барс Керамик",
        price,
        picture,
        description,
        seed.source_group,
        detect_detail_group(soup),
        detect_type(soup),
    )


def category_for_product(product: Product, old_category: str) -> str | None:
    if JBI_RE.search(f"{product.title} {product.url}") or "/catalog/zhbi/" in product.url:
        return None
    if product.source_group != "brick":
        expected = product.source_group
        if product.detail_group and product.detail_group != expected:
            return None
        return expected
    if product.detail_group and product.detail_group != "brick":
        return None
    if NON_BRICK_RE.search(product.title):
        return None
    if product.product_type == "блоки" or BLOCK_RE.search(product.title):
        return "102"
    if product.product_type in {"лицевой", "кладочный"}:
        return "101"
    if old_category in {"101", "102"}:
        return old_category
    return "101"


def set_child(element: ET.Element, tag: str, value: str) -> None:
    child = element.find(tag)
    if child is None:
        child = ET.SubElement(element, tag)
    child.text = value


def create_offer(product: Product, category_id: str, old: ET.Element | None) -> ET.Element:
    offer = copy.deepcopy(old) if old is not None else ET.Element("offer", {"id": product.product_id})
    offer.set("id", product.product_id)
    set_child(offer, "name", product.title)
    set_child(offer, "vendor", product.vendor)
    set_child(offer, "price", f"{product.price:.2f}")
    set_child(offer, "currencyId", "RUB")
    set_child(offer, "categoryId", category_id)
    set_child(offer, "picture", product.picture or child_text(old, "picture"))
    description = product.description or child_text(old, "description") or f"{product.title}. Производитель: {product.vendor}."
    short_description = child_text(old, "shortDescription") or product.title
    set_child(offer, "description", description)
    set_child(offer, "shortDescription", short_description)
    set_child(offer, "url", product.url)
    order = ["name", "vendor", "price", "currencyId", "categoryId", "picture", "description", "shortDescription", "url"]
    children = {child.tag: child for child in list(offer)}
    for child in list(offer):
        offer.remove(child)
    for tag in order:
        offer.append(children.get(tag, ET.Element(tag)))
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

    seeds: dict[str, Seed] = {}
    for source_group, url in CATEGORY_PAGES.items():
        found = collect_category(url, source_group, old_by_id)
        for pid, seed in found.items():
            if pid not in seeds or len(seed.title) > len(seeds[pid].title):
                seeds[pid] = seed
    print(f"Уникальных ссылок на карточки: {len(seeds)}")

    products: dict[str, Product] = {}
    no_price: list[str] = []
    errors: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=14) as executor:
        futures = {executor.submit(detail_product, seed, old_by_id.get(pid)): pid for pid, seed in seeds.items()}
        for index, future in enumerate(as_completed(futures), start=1):
            pid = futures[future]
            try:
                product = future.result()
                if product is None:
                    no_price.append(pid)
                else:
                    products[pid] = product
            except Exception as exc:  # noqa: BLE001
                errors[pid] = str(exc)
            if index % 100 == 0 or index == len(futures):
                print(f"Карточки: {index}/{len(futures)}; с ценой {len(products)}; без цены {len(no_price)}; ошибок {len(errors)}")

    final: list[tuple[str, Product]] = []
    skipped_jbi: list[str] = []
    skipped_mismatch: list[str] = []
    for pid, product in products.items():
        if JBI_RE.search(f"{product.title} {product.url}"):
            skipped_jbi.append(pid)
            continue
        cid = category_for_product(product, old_category.get(pid, ""))
        if cid not in EXPECTED_CATEGORIES:
            skipped_mismatch.append(pid)
            continue
        final.append((cid, product))

    final.sort(key=lambda pair: (int(pair[0]), int(pair[1].product_id)))
    offers_parent.clear()
    counts: Counter[str] = Counter()
    new_ids: list[str] = []
    for cid, product in final:
        old = old_by_id.get(product.product_id)
        if old is None:
            new_ids.append(product.product_id)
        offers_parent.append(create_offer(product, cid, old))
        counts[cid] += 1

    final_ids = {product.product_id for _, product in final}
    removed_ids = sorted(set(old_by_id) - final_ids, key=lambda value: int(value) if value.isdigit() else value)
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
        "collected_seed_count": len(seeds),
        "detail_products_with_price": len(products),
        "new_offer_count": len(final),
        "counts": {cid: counts[cid] for cid in EXPECTED_CATEGORIES},
        "new_ids_count": len(new_ids),
        "new_ids_first_100": new_ids[:100],
        "removed_ids_count": len(removed_ids),
        "removed_ids_first_100": removed_ids[:100],
        "no_price_count": len(no_price),
        "no_price_ids_first_100": sorted(no_price, key=lambda value: int(value))[:100],
        "request_error_count": len(errors),
        "request_errors_first_30": dict(list(errors.items())[:30]),
        "skipped_jbi_count": len(skipped_jbi),
        "skipped_jbi_ids": skipped_jbi,
        "skipped_category_mismatch_count": len(skipped_mismatch),
        "skipped_category_mismatch_ids_first_100": skipped_mismatch[:100],
        "source_pages": CATEGORY_PAGES,
    }
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
