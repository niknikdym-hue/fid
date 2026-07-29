#!/usr/bin/env python3
"""One-time updater for the Ak Bars Keramik brick feed.

Crawls the current brick catalog, keeps only products whose site characteristic
"Тип" is "Лицевой" or "Кладочный", and explicitly excludes ceramic blocks.
Existing offer wording is preserved where possible, while prices, URLs, images,
manufacturer and characteristics are refreshed from the product pages.
"""

from __future__ import annotations

import copy
import html
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable
from urllib.parse import parse_qs, urljoin, urlparse
import xml.etree.ElementTree as ET

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://akbarskeramic.ru"
CATALOG_URL = f"{BASE_URL}/catalog/kirpich/"
FEED_PATH = Path("akbars_kirpich_yandex_direct_feed.yml")
ALLOWED_TYPES = {"лицевой": ("1", "Кирпич лицевой"), "кладочный": ("2", "Кирпич кладочный")}

KNOWN_LABELS = [
    "Тип",
    "Производитель",
    "Формат, НФ",
    "Марка прочности (российский стандарт)",
    "Марка прочности",
    "Класс морозостойкости, F",
    "Морозостойкость",
    "Пустотность",
    "Оттенки цветов",
    "Цвет производителя",
    "Размеры",
    "Высота, мм",
    "Вес",
    "На поддоне",
    "Норма загрузки",
    "Степень прочности",
    "Водопоглощение",
    "Коэффициент теплопроводности",
    "Место укладки",
]

PARAM_NAME_MAP = {
    "Марка прочности (российский стандарт)": "Прочность",
    "Марка прочности": "Прочность",
    "Класс морозостойкости, F": "Морозостойкость",
}

SESSION = requests.Session()
SESSION.headers.update(
    {
        "User-Agent": "Mozilla/5.0 (compatible; AkBarsFeedUpdater/1.0; +https://github.com/niknikdym-hue/fid)",
        "Accept-Language": "ru-RU,ru;q=0.9",
    }
)


@dataclass
class Product:
    product_id: str
    url: str
    title: str
    price: float
    product_type: str
    vendor: str
    picture: str
    params: dict[str, str]


def clean_text(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def request(url: str, *, attempts: int = 4) -> requests.Response:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            response = SESSION.get(url, timeout=(15, 45))
            response.raise_for_status()
            return response
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt < attempts:
                time.sleep(attempt * 1.5)
    raise RuntimeError(f"Не удалось загрузить {url}: {last_error}")


def product_id_from_url(url: str) -> str:
    match = re.search(r"/catalog/product/(\d+)-", url)
    return match.group(1) if match else ""


def canonical_product_url(href: str) -> str | None:
    url = urljoin(BASE_URL, href)
    parsed = urlparse(url)
    if parsed.netloc != urlparse(BASE_URL).netloc:
        return None
    if not re.search(r"/catalog/product/\d+-", parsed.path):
        return None
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"


def collect_product_urls() -> list[str]:
    first = request(CATALOG_URL)
    soup = BeautifulSoup(first.text, "html.parser")

    page_numbers = {1}
    for a in soup.find_all("a", href=True):
        query = parse_qs(urlparse(a["href"]).query)
        for key, values in query.items():
            if key.startswith("PAGEN_"):
                for value in values:
                    if value.isdigit():
                        page_numbers.add(int(value))
    max_page = max(page_numbers) if page_numbers else 1
    max_page = min(max(max_page, 1), 100)

    urls: dict[str, None] = {}

    def add_from_html(text: str) -> int:
        local_soup = BeautifulSoup(text, "html.parser")
        before = len(urls)
        for a in local_soup.find_all("a", href=True):
            product_url = canonical_product_url(a["href"])
            if product_url:
                urls[product_url] = None
        return len(urls) - before

    add_from_html(first.text)
    print(f"Каталог: найдено страниц — {max_page}")

    for page in range(2, max_page + 1):
        response = request(f"{CATALOG_URL}?PAGEN_3={page}")
        added = add_from_html(response.text)
        print(f"  страница {page}: новых карточек {added}")

    if not urls:
        raise RuntimeError("В каталоге не найдено ни одной ссылки на товар")

    result = sorted(urls, key=lambda u: int(product_id_from_url(u) or 0))
    print(f"Каталог: уникальных карточек — {len(result)}")
    return result


def parse_json_ld_product(soup: BeautifulSoup) -> dict:
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.string or script.get_text(" ", strip=True)
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        candidates: Iterable[dict]
        if isinstance(data, list):
            candidates = [item for item in data if isinstance(item, dict)]
        elif isinstance(data, dict) and isinstance(data.get("@graph"), list):
            candidates = [item for item in data["@graph"] if isinstance(item, dict)]
        elif isinstance(data, dict):
            candidates = [data]
        else:
            continue
        for item in candidates:
            item_type = item.get("@type")
            if item_type == "Product" or (isinstance(item_type, list) and "Product" in item_type):
                return item
    return {}


def parse_price(soup: BeautifulSoup, json_ld: dict) -> float | None:
    offers = json_ld.get("offers") if isinstance(json_ld, dict) else None
    if isinstance(offers, list):
        offers = next((x for x in offers if isinstance(x, dict)), None)
    if isinstance(offers, dict):
        for key in ("price", "lowPrice"):
            value = offers.get(key)
            if value not in (None, ""):
                try:
                    return float(str(value).replace(" ", "").replace(",", "."))
                except ValueError:
                    pass

    for tag in soup.select('[itemprop="price"], meta[property="product:price:amount"]'):
        value = tag.get("content") or tag.get("value") or tag.get_text(" ", strip=True)
        if value:
            match = re.search(r"\d[\d\s]*(?:[.,]\d{1,2})?", value)
            if match:
                return float(match.group(0).replace(" ", "").replace(",", "."))

    h1 = soup.find("h1")
    root = h1.parent if h1 and h1.parent else soup
    text = clean_text(root.get_text(" ", strip=True))
    match = re.search(r"(?<!\d)(\d[\d\s]*(?:[.,]\d{1,2})?)\s*₽", text)
    if match:
        return float(match.group(1).replace(" ", "").replace(",", "."))
    return None


def parse_characteristics(soup: BeautifulSoup) -> dict[str, str]:
    result: dict[str, str] = {}

    # First use explicit table/list structures where labels and values are separate.
    for row in soup.select("tr, dl, li"):
        parts = [clean_text(x) for x in row.stripped_strings]
        parts = [x for x in parts if x]
        if len(parts) < 2:
            continue
        joined = " ".join(parts)
        for label in KNOWN_LABELS:
            if parts[0] == label:
                result.setdefault(label, clean_text(" ".join(parts[1:])))
                break
            if joined.startswith(label + " "):
                result.setdefault(label, clean_text(joined[len(label) + 1 :]))
                break

    strings = [clean_text(x) for x in soup.stripped_strings]
    strings = [x for x in strings if x]
    try:
        start = next(i for i, value in enumerate(strings) if value == "Характеристики") + 1
    except StopIteration:
        start = 0
    stop_markers = {"Спланируйте свой будущий дом сегодня", "Вы знаете, как возводить мечты"}
    end = next((i for i in range(start, len(strings)) if strings[i] in stop_markers), min(len(strings), start + 120))
    segment = strings[start:end]

    labels_by_length = sorted(KNOWN_LABELS, key=len, reverse=True)
    for i, token in enumerate(segment):
        for label in labels_by_length:
            if token == label and i + 1 < len(segment):
                nxt = segment[i + 1]
                if nxt not in KNOWN_LABELS:
                    result.setdefault(label, nxt)
                break
            if token.startswith(label + " "):
                result.setdefault(label, clean_text(token[len(label) + 1 :]))
                break

    # Normalize accidental captures that swallowed the next known label.
    for label, value in list(result.items()):
        for next_label in labels_by_length:
            marker = f" {next_label} "
            if marker in value:
                result[label] = value.split(marker, 1)[0].strip()
                break
    return {k: v for k, v in result.items() if v}


def parse_product(url: str, old_type_by_id: dict[str, str]) -> Product | None:
    response = request(url)
    soup = BeautifulSoup(response.text, "html.parser")
    product_id = product_id_from_url(url)
    h1 = soup.find("h1")
    title = clean_text(h1.get_text(" ", strip=True) if h1 else "")
    if not title:
        raise RuntimeError(f"У товара {url} не найден H1")

    json_ld = parse_json_ld_product(soup)
    price = parse_price(soup, json_ld)
    if price is None or price <= 0:
        print(f"SKIP без числовой цены: {product_id} {title}")
        return None

    params = parse_characteristics(soup)
    raw_type = clean_text(params.get("Тип", "")).lower()
    if raw_type not in ALLOWED_TYPES:
        raw_type = old_type_by_id.get(product_id, "").lower()

    # Strong block exclusion. A product is included only when its type is known
    # to be facing or masonry brick.
    if raw_type not in ALLOWED_TYPES:
        print(f"SKIP не кирпич ({params.get('Тип', 'тип не распознан')}): {product_id} {title}")
        return None
    if params.get("Тип", "").strip().lower() == "блоки":
        print(f"SKIP блок: {product_id} {title}")
        return None

    vendor = clean_text(params.get("Производитель", ""))
    if not vendor and isinstance(json_ld, dict):
        brand = json_ld.get("brand")
        if isinstance(brand, dict):
            vendor = clean_text(str(brand.get("name", "")))
        elif isinstance(brand, str):
            vendor = clean_text(brand)

    picture = ""
    image = json_ld.get("image") if isinstance(json_ld, dict) else None
    if isinstance(image, list) and image:
        image = image[0]
    if isinstance(image, dict):
        image = image.get("url")
    if isinstance(image, str):
        picture = urljoin(BASE_URL, image)
    if not picture:
        meta = soup.find("meta", property="og:image")
        if meta and meta.get("content"):
            picture = urljoin(BASE_URL, meta["content"])

    return Product(
        product_id=product_id,
        url=url,
        title=title,
        price=price,
        product_type=raw_type,
        vendor=vendor,
        picture=picture,
        params=params,
    )


def child_text(element: ET.Element, tag: str) -> str:
    child = element.find(tag)
    return clean_text(child.text if child is not None else "")


def set_child(element: ET.Element, tag: str, value: str, *, after: str | None = None) -> ET.Element:
    child = element.find(tag)
    if child is None:
        child = ET.Element(tag)
        if after:
            siblings = list(element)
            try:
                index = next(i for i, item in enumerate(siblings) if item.tag == after) + 1
            except StopIteration:
                index = len(siblings)
            element.insert(index, child)
        else:
            element.append(child)
    child.text = value
    return child


def build_offer(product: Product, old_offer: ET.Element | None) -> ET.Element:
    category_id, type_prefix = ALLOWED_TYPES[product.product_type]
    if old_offer is not None:
        offer = copy.deepcopy(old_offer)
    else:
        offer = ET.Element("offer", {"id": product.product_id, "available": "true", "type": "vendor.model"})

    offer.attrib.update({"id": product.product_id, "available": "true", "type": "vendor.model"})
    set_child(offer, "url", product.url)
    set_child(offer, "price", f"{product.price:.2f}")
    set_child(offer, "currencyId", "RUB")
    set_child(offer, "categoryId", category_id)

    if product.picture:
        set_child(offer, "picture", product.picture)

    existing_name = child_text(offer, "name")
    if old_offer is None or not existing_name:
        if re.search(r"\bкирпич\b", product.title, flags=re.IGNORECASE):
            feed_name = product.title
        else:
            feed_name = f"{type_prefix} {product.title}"
        set_child(offer, "name", feed_name)

    set_child(offer, "typePrefix", type_prefix)
    if product.vendor:
        set_child(offer, "vendor", product.vendor)
    elif offer.find("vendor") is None:
        set_child(offer, "vendor", "Ак Барс Керамик")
    set_child(offer, "model", product.title)
    set_child(offer, "pickup", "true")
    set_child(offer, "delivery", "true")
    set_child(offer, "sales_notes", "Самовывоз со склада и доставка доступны по согласованию.")

    # Replace stale characteristics with the current data from the product page.
    for param in list(offer.findall("param")):
        offer.remove(param)

    description_parts = [child_text(offer, "name") or product.title]
    param_order = [
        "Формат, НФ",
        "Размеры",
        "Вес",
        "Марка прочности (российский стандарт)",
        "Марка прочности",
        "Класс морозостойкости, F",
        "Морозостойкость",
        "Пустотность",
        "Оттенки цветов",
        "Цвет производителя",
        "На поддоне",
        "Норма загрузки",
        "Водопоглощение",
        "Коэффициент теплопроводности",
    ]
    seen_param_names: set[str] = set()
    for source_name in param_order:
        value = clean_text(product.params.get(source_name, ""))
        if not value:
            continue
        param_name = PARAM_NAME_MAP.get(source_name, source_name)
        if param_name in seen_param_names:
            continue
        seen_param_names.add(param_name)
        param = ET.SubElement(offer, "param", {"name": param_name})
        if param_name == "Вес" and not re.search(r"\bкг\b", value, flags=re.IGNORECASE):
            param.set("unit", "кг")
        elif param_name == "Размеры" and not re.search(r"\bмм\b", value, flags=re.IGNORECASE):
            param.set("unit", "мм")
        param.text = value
        if param_name in {"Формат, НФ", "Размеры", "Вес", "Прочность", "Морозостойкость", "Оттенки цветов"}:
            description_parts.append(f"{param_name}: {value}")

    description = ". ".join(part.rstrip(".") for part in description_parts if part) + "."
    set_child(offer, "description", description)

    desired_order = [
        "url",
        "price",
        "currencyId",
        "categoryId",
        "picture",
        "name",
        "typePrefix",
        "vendor",
        "model",
        "pickup",
        "delivery",
        "sales_notes",
        "description",
        "param",
    ]
    order_index = {tag: i for i, tag in enumerate(desired_order)}
    children = list(offer)
    children.sort(key=lambda x: order_index.get(x.tag, 999))
    offer[:] = children
    return offer


def indent_xml(element: ET.Element, level: int = 0) -> None:
    pad = "\n" + "  " * level
    child_pad = "\n" + "  " * (level + 1)
    if len(element):
        if not element.text or not element.text.strip():
            element.text = child_pad
        for child in element:
            indent_xml(child, level + 1)
        if not element[-1].tail or not element[-1].tail.strip():
            element[-1].tail = pad
    if level and (not element.tail or not element.tail.strip()):
        element.tail = pad


def main() -> int:
    if not FEED_PATH.exists():
        raise FileNotFoundError(FEED_PATH)

    old_tree = ET.parse(FEED_PATH)
    old_root = old_tree.getroot()
    old_offers_parent = old_root.find("./shop/offers")
    if old_offers_parent is None:
        raise RuntimeError("В исходном фиде отсутствует /shop/offers")

    old_by_id = {offer.attrib.get("id", ""): offer for offer in old_offers_parent.findall("offer")}
    old_type_by_id: dict[str, str] = {}
    for product_id, offer in old_by_id.items():
        category_id = child_text(offer, "categoryId")
        if category_id == "1":
            old_type_by_id[product_id] = "лицевой"
        elif category_id == "2":
            old_type_by_id[product_id] = "кладочный"

    urls = collect_product_urls()
    products: list[Product] = []
    failures: list[str] = []

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(parse_product, url, old_type_by_id): url for url in urls}
        for index, future in enumerate(as_completed(futures), start=1):
            url = futures[future]
            try:
                product = future.result()
                if product is not None:
                    products.append(product)
            except Exception as exc:  # noqa: BLE001
                failures.append(f"{url}: {exc}")
            if index % 25 == 0 or index == len(futures):
                print(f"Обработано карточек: {index}/{len(futures)}; кирпичей с ценой: {len(products)}")

    failure_limit = max(5, int(len(urls) * 0.05))
    if len(failures) > failure_limit:
        print("Слишком много ошибок карточек:", file=sys.stderr)
        for failure in failures:
            print(" -", failure, file=sys.stderr)
        raise RuntimeError(f"Ошибок {len(failures)}, допустимо не более {failure_limit}")
    for failure in failures:
        print("WARN", failure, file=sys.stderr)

    products.sort(key=lambda item: (0 if item.product_type == "кладочный" else 1, item.vendor.lower(), item.title.lower(), int(item.product_id)))
    if len(products) < 50:
        raise RuntimeError(f"Защитная остановка: найдено подозрительно мало кирпичей — {len(products)}")

    root = ET.Element("yml_catalog", {"date": datetime.now().strftime("%Y-%m-%d %H:%M")})
    shop = ET.SubElement(root, "shop")
    ET.SubElement(shop, "name").text = "Ак Барс Керамик"
    ET.SubElement(shop, "company").text = "Ак Барс Керамик"
    ET.SubElement(shop, "url").text = BASE_URL
    currencies = ET.SubElement(shop, "currencies")
    ET.SubElement(currencies, "currency", {"id": "RUB", "rate": "1"})
    categories = ET.SubElement(shop, "categories")
    ET.SubElement(categories, "category", {"id": "1"}).text = "Кирпич лицевой"
    ET.SubElement(categories, "category", {"id": "2"}).text = "Кирпич кладочный"
    offers_parent = ET.SubElement(shop, "offers")

    for product in products:
        offers_parent.append(build_offer(product, old_by_id.get(product.product_id)))

    indent_xml(root)
    xml_bytes = ET.tostring(root, encoding="utf-8", xml_declaration=True, short_empty_elements=True)
    FEED_PATH.write_bytes(xml_bytes + b"\n")

    # Final structural checks.
    check_root = ET.parse(FEED_PATH).getroot()
    check_offers = check_root.findall("./shop/offers/offer")
    ids = [offer.attrib.get("id", "") for offer in check_offers]
    if len(ids) != len(set(ids)):
        raise RuntimeError("В итоговом фиде появились повторяющиеся offer id")
    if any(child_text(offer, "categoryId") not in {"1", "2"} for offer in check_offers):
        raise RuntimeError("В итоговом фиде обнаружена посторонняя категория")
    if any("блок" in (child_text(offer, "typePrefix") + " " + child_text(offer, "name")).lower() for offer in check_offers):
        raise RuntimeError("В итоговом фиде обнаружен блок")

    face_count = sum(child_text(offer, "categoryId") == "1" for offer in check_offers)
    masonry_count = sum(child_text(offer, "categoryId") == "2" for offer in check_offers)
    print(f"ГОТОВО: {len(check_offers)} предложений; лицевой — {face_count}; кладочный — {masonry_count}; блоки — 0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
