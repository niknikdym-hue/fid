#!/usr/bin/env python3
from __future__ import annotations

import html
import json
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse
import xml.etree.ElementTree as ET

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://akbarskeramic.ru"
FILTER_URL = f"{BASE_URL}/catalog/kirpich/filter/format-is-rigel/"
OUTPUT = Path("akbars_rigel_kirpich_yandex_direct_feed.yml")
EXPECTED_COUNT = 7

LABELS = [
    "Тип",
    "Производитель",
    "Формат, НФ",
    "Марка прочности (российский стандарт)",
    "Класс морозостойкости, F",
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
    "Морозостойкость",
    "Марка прочности",
]

PARAM_NAMES = {
    "Марка прочности (российский стандарт)": "Прочность",
    "Марка прочности": "Прочность",
    "Класс морозостойкости, F": "Морозостойкость",
}

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (compatible; AkBarsRigelFeed/1.0; +https://github.com/niknikdym-hue/fid)",
    "Accept-Language": "ru-RU,ru;q=0.9",
})


def clean(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def get(url: str, attempts: int = 4) -> requests.Response:
    error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            response = SESSION.get(url, timeout=(15, 45))
            response.raise_for_status()
            return response
        except Exception as exc:  # noqa: BLE001
            error = exc
            if attempt < attempts:
                time.sleep(attempt * 1.5)
    raise RuntimeError(f"Не удалось загрузить {url}: {error}")


def product_id(url: str) -> str:
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


def collect_products() -> list[str]:
    soup = BeautifulSoup(get(FILTER_URL).text, "html.parser")
    urls: dict[str, None] = {}
    for tag in soup.find_all("a", href=True):
        url = canonical_product_url(tag["href"])
        if url:
            urls[url] = None
    result = list(urls)
    if len(result) != EXPECTED_COUNT:
        raise RuntimeError(f"Ожидалось {EXPECTED_COUNT} ригельных товаров, найдено {len(result)}: {result}")
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
        candidates: list[dict] = []
        if isinstance(data, list):
            candidates = [x for x in data if isinstance(x, dict)]
        elif isinstance(data, dict) and isinstance(data.get("@graph"), list):
            candidates = [x for x in data["@graph"] if isinstance(x, dict)]
        elif isinstance(data, dict):
            candidates = [data]
        for item in candidates:
            item_type = item.get("@type")
            if item_type == "Product" or (isinstance(item_type, list) and "Product" in item_type):
                return item
    return {}


def parse_price(soup: BeautifulSoup, product_json: dict) -> float:
    offers = product_json.get("offers") if isinstance(product_json, dict) else None
    if isinstance(offers, list):
        offers = next((x for x in offers if isinstance(x, dict)), None)
    if isinstance(offers, dict):
        for key in ("price", "lowPrice"):
            raw = offers.get(key)
            if raw not in (None, ""):
                try:
                    return float(str(raw).replace("\xa0", "").replace(" ", "").replace(",", "."))
                except ValueError:
                    pass

    for tag in soup.select('[itemprop="price"], meta[property="product:price:amount"]'):
        raw = tag.get("content") or tag.get("value") or tag.get_text(" ", strip=True)
        if raw:
            match = re.search(r"\d[\d\s\xa0]*(?:[.,]\d{1,2})?", raw)
            if match:
                return float(match.group(0).replace("\xa0", "").replace(" ", "").replace(",", "."))

    h1 = soup.find("h1")
    scope = h1.parent if h1 and h1.parent else soup
    text = clean(scope.get_text(" ", strip=True))
    match = re.search(r"(?<!\d)(\d[\d\s\xa0]*(?:[.,]\d{1,2})?)\s*₽", text)
    if not match:
        raise RuntimeError("Не найдена числовая цена")
    return float(match.group(1).replace("\xa0", "").replace(" ", "").replace(",", "."))


def parse_params(soup: BeautifulSoup) -> dict[str, str]:
    tokens = [clean(x) for x in soup.stripped_strings]
    tokens = [x for x in tokens if x]
    try:
        start = next(i for i, value in enumerate(tokens) if value == "Характеристики") + 1
    except StopIteration as exc:
        raise RuntimeError("Не найден блок характеристик") from exc

    stop = next(
        (i for i in range(start, len(tokens)) if tokens[i] in {"Спланируйте свой будущий дом сегодня", "Вы знаете, как возводить мечты"}),
        min(start + 120, len(tokens)),
    )
    segment = tokens[start:stop]
    labels = sorted(LABELS, key=len, reverse=True)
    result: dict[str, str] = {}
    for index, token in enumerate(segment):
        for label in labels:
            if token == label and index + 1 < len(segment):
                value = segment[index + 1]
                if value not in LABELS:
                    result.setdefault(label, value)
                break
            if token.startswith(label + " "):
                result.setdefault(label, clean(token[len(label) + 1 :]))
                break
    return result


def normalize_picture(url: str) -> str:
    url = urljoin(BASE_URL, url)
    match = re.search(r"(/upload/)resize_cache/(iblock/[^/]+/[^/]+)/[^/]+/([^?#]+)", url, re.I)
    if match:
        return urljoin(BASE_URL, f"{match.group(1)}{match.group(2)}/{match.group(3)}")
    return url


def picture_url(soup: BeautifulSoup, product_json: dict) -> str:
    image = product_json.get("image") if isinstance(product_json, dict) else None
    if isinstance(image, list) and image:
        image = image[0]
    if isinstance(image, dict):
        image = image.get("url")
    if isinstance(image, str) and image:
        return normalize_picture(image)

    meta = soup.find("meta", property="og:image")
    if meta and meta.get("content"):
        return normalize_picture(str(meta["content"]))

    h1 = soup.find("h1")
    for image_tag in soup.find_all("img"):
        alt = clean(image_tag.get("alt", ""))
        if h1 and alt == clean(h1.get_text(" ", strip=True)):
            source = image_tag.get("data-src") or image_tag.get("src")
            if source:
                return normalize_picture(str(source))
    return ""


def add_text(parent: ET.Element, tag: str, value: str, **attrs: str) -> ET.Element:
    element = ET.SubElement(parent, tag, attrs)
    element.text = value
    return element


def create_offer(parent: ET.Element, url: str) -> None:
    soup = BeautifulSoup(get(url).text, "html.parser")
    h1 = soup.find("h1")
    title = clean(h1.get_text(" ", strip=True) if h1 else "")
    if not title:
        raise RuntimeError(f"У {url} не найден H1")

    params = parse_params(soup)
    if clean(params.get("Формат, НФ", "")).lower() != "ригель":
        raise RuntimeError(f"Товар не подтвержден как ригельный: {url} — {params.get('Формат, НФ')}")
    if clean(params.get("Тип", "")).lower() == "блоки":
        raise RuntimeError(f"В ригельный фид попал блок: {url}")

    product_json = json_ld_product(soup)
    price = parse_price(soup, product_json)
    vendor = clean(params.get("Производитель", ""))
    picture = picture_url(soup, product_json)

    offer = ET.SubElement(parent, "offer", {"id": product_id(url), "available": "true", "type": "vendor.model"})
    add_text(offer, "url", url)
    add_text(offer, "price", f"{price:.2f}")
    add_text(offer, "currencyId", "RUB")
    add_text(offer, "categoryId", "1")
    if picture:
        add_text(offer, "picture", picture)
    add_text(offer, "name", f"Кирпич лицевой ригельный {title}")
    add_text(offer, "typePrefix", "Кирпич лицевой ригельный")
    if vendor:
        add_text(offer, "vendor", vendor)
    add_text(offer, "model", title)
    add_text(offer, "pickup", "true")
    add_text(offer, "delivery", "true")
    add_text(offer, "sales_notes", "Самовывоз со склада и доставка доступны по согласованию.")

    description_parts = [f"Кирпич лицевой ригельный {title}."]
    for source_name in LABELS:
        if source_name in {"Тип", "Производитель"}:
            continue
        value = clean(params.get(source_name, ""))
        if value:
            target_name = PARAM_NAMES.get(source_name, source_name)
            fragment = f"{target_name}: {value}."
            if fragment not in description_parts:
                description_parts.append(fragment)
    add_text(offer, "description", " ".join(description_parts))

    added: set[str] = set()
    for source_name in LABELS:
        if source_name in {"Тип", "Производитель"}:
            continue
        value = clean(params.get(source_name, ""))
        if not value:
            continue
        target_name = PARAM_NAMES.get(source_name, source_name)
        if target_name in added:
            continue
        attrs: dict[str, str] = {"name": target_name}
        if target_name == "Размеры":
            attrs["unit"] = "мм"
        elif target_name == "Вес":
            attrs["unit"] = "кг"
        add_text(offer, "param", value, **attrs)
        added.add(target_name)


def main() -> int:
    urls = collect_products()

    catalog = ET.Element("yml_catalog", {"date": datetime.now().strftime("%Y-%m-%d %H:%M")})
    shop = ET.SubElement(catalog, "shop")
    add_text(shop, "name", "Ак Барс Керамик")
    add_text(shop, "company", "Ак Барс Керамик")
    add_text(shop, "url", BASE_URL)
    currencies = ET.SubElement(shop, "currencies")
    ET.SubElement(currencies, "currency", {"id": "RUB", "rate": "1"})
    categories = ET.SubElement(shop, "categories")
    add_text(categories, "category", "Кирпич лицевой ригельный", id="1")
    offers = ET.SubElement(shop, "offers")

    for url in urls:
        print(f"Обрабатывается: {url}")
        create_offer(offers, url)

    offer_nodes = offers.findall("offer")
    if len(offer_nodes) != EXPECTED_COUNT:
        raise RuntimeError(f"В итоговом фиде {len(offer_nodes)} предложений вместо {EXPECTED_COUNT}")
    if any(node.findtext("categoryId") != "1" for node in offer_nodes):
        raise RuntimeError("Обнаружена посторонняя категория")
    if any("блок" in clean(node.findtext("name")).lower() for node in offer_nodes):
        raise RuntimeError("Обнаружен блок")

    ET.indent(catalog, space="  ")
    ET.ElementTree(catalog).write(OUTPUT, encoding="utf-8", xml_declaration=True)
    print(f"ГОТОВО: {OUTPUT}; предложений — {len(offer_nodes)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
