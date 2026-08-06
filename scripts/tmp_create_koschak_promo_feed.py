#!/usr/bin/env python3
from __future__ import annotations

import copy
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

SOURCE = Path("akbars_kirpich_yandex_direct_feed.yml")
TARGET = Path("akbars_koschakovskiy_aktsiya_yandex_direct_feed.yml")

TARGETS = {
    "flesh_cosmo": {
        "price": "25.00",
        "display_name": "Кирпич лицевой ФЛЕШ КОСМО РИФ 1 НФ — акция",
    },
    "soloma_kanyon": {
        "price": "26.00",
        "display_name": "Кирпич лицевой Солома Каньон 1 НФ — акция",
    },
    "soloma_dikiy_kamen": {
        "price": "26.00",
        "display_name": "Кирпич лицевой Солома Дикий камень 1 НФ — акция",
    },
    "sahara_galich": {
        "price": "27.00",
        "display_name": "Кирпич лицевой Сахара Галич с песком 1 НФ — акция",
    },
}


def norm(value: str | None) -> str:
    text = (value or "").upper().replace("Ё", "Е")
    text = text.replace("1,4", "1.4")
    return re.sub(r"[^А-ЯA-Z0-9.]+", " ", text).strip()


def child_text(node: ET.Element, tag: str) -> str:
    child = node.find(tag)
    return (child.text or "").strip() if child is not None else ""


def is_one_nf(name: str) -> bool:
    return "1.4" not in name and bool(re.search(r"(?:^| )1 НФ(?: |$)", name))


def classify(name: str) -> str | None:
    n = norm(name)
    if not is_one_nf(n):
        return None
    if "ФЛЕШ" in n and "КОСМО" in n:
        return "flesh_cosmo"
    if "СОЛОМА" in n and "КАНЬОН" in n:
        return "soloma_kanyon"
    if "СОЛОМА" in n and "ДИКИЙ" in n and "КАМЕНЬ" in n:
        return "soloma_dikiy_kamen"
    if "САХАРА" in n and "ГАЛИЧ" in n and "КРАСНЫЙ" not in n:
        return "sahara_galich"
    return None


def score(key: str, offer: ET.Element) -> tuple[int, int]:
    name = norm(child_text(offer, "name"))
    points = 0
    if "КОЩАКОВ" in norm(child_text(offer, "vendor")):
        points += 20
    if key == "flesh_cosmo" and "РИФ" in name:
        points += 10
    if key == "sahara_galich" and "С ПЕСКОМ" in name:
        points += 10
    if "КИРПИЧ" in name:
        points += 2
    return points, -len(name)


def set_text(node: ET.Element, tag: str, value: str, after: str | None = None) -> None:
    child = node.find(tag)
    if child is None:
        child = ET.Element(tag)
        if after:
            siblings = list(node)
            try:
                index = next(i for i, item in enumerate(siblings) if item.tag == after) + 1
            except StopIteration:
                index = len(siblings)
            node.insert(index, child)
        else:
            node.append(child)
    child.text = value


def indent(elem: ET.Element, level: int = 0) -> None:
    pad = "\n" + "  " * level
    if len(elem):
        if not elem.text or not elem.text.strip():
            elem.text = pad + "  "
        for child in elem:
            indent(child, level + 1)
        if not elem[-1].tail or not elem[-1].tail.strip():
            elem[-1].tail = pad
    if level and (not elem.tail or not elem.tail.strip()):
        elem.tail = pad


def main() -> int:
    source_root = ET.parse(SOURCE).getroot()
    source_offers = source_root.findall("./shop/offers/offer")

    candidates: dict[str, list[ET.Element]] = {key: [] for key in TARGETS}
    for offer in source_offers:
        key = classify(child_text(offer, "name"))
        if key:
            candidates[key].append(offer)

    selected: dict[str, ET.Element] = {}
    for key, offers in candidates.items():
        if not offers:
            raise RuntimeError(f"Не найдена карточка товара: {key}")
        selected[key] = max(offers, key=lambda offer: score(key, offer))

    date_value = datetime.now(ZoneInfo("Europe/Moscow")).strftime("%Y-%m-%d %H:%M")
    root = ET.Element("yml_catalog", {"date": date_value})
    shop = ET.SubElement(root, "shop")
    ET.SubElement(shop, "name").text = "Ак Барс Керамик — акционный кирпич"
    ET.SubElement(shop, "company").text = "Ак Барс Керамик"
    ET.SubElement(shop, "url").text = "https://akbarskeramic.ru"
    currencies = ET.SubElement(shop, "currencies")
    ET.SubElement(currencies, "currency", {"id": "RUB", "rate": "1"})
    categories = ET.SubElement(shop, "categories")
    ET.SubElement(categories, "category", {"id": "1"}).text = "Акционный кирпич Кощаковского завода"
    offers_node = ET.SubElement(shop, "offers")

    order = ["flesh_cosmo", "soloma_kanyon", "soloma_dikiy_kamen", "sahara_galich"]
    for key in order:
        source_offer = selected[key]
        offer = copy.deepcopy(source_offer)
        offer.set("available", "true")
        offer.set("type", "vendor.model")
        set_text(offer, "price", TARGETS[key]["price"])
        oldprice = offer.find("oldprice")
        if oldprice is not None:
            offer.remove(oldprice)
        set_text(offer, "currencyId", "RUB", after="price")
        set_text(offer, "categoryId", "1", after="currencyId")
        set_text(offer, "name", TARGETS[key]["display_name"])
        set_text(offer, "typePrefix", "Кирпич лицевой акционный")
        set_text(offer, "vendor", "Кощаковский завод")
        set_text(offer, "model", TARGETS[key]["display_name"].replace(" — акция", ""))
        set_text(offer, "pickup", "true")
        set_text(offer, "delivery", "true")
        set_text(
            offer,
            "sales_notes",
            "Акционная цена за 1 штуку. Наличие, объём партии, срок действия акции и условия доставки уточняйте у менеджера.",
        )
        description = child_text(offer, "description")
        promo = f"Акционная цена — {TARGETS[key]['price'].replace('.00', '')} руб./шт."
        set_text(offer, "description", f"{promo} {description}".strip())
        offers_node.append(offer)

    ids = [offer.get("id", "") for offer in offers_node.findall("offer")]
    prices = [child_text(offer, "price") for offer in offers_node.findall("offer")]
    if len(ids) != 4 or len(set(ids)) != 4:
        raise RuntimeError(f"Некорректное число или дубли ID: {ids}")
    if prices != ["25.00", "26.00", "26.00", "27.00"]:
        raise RuntimeError(f"Некорректные цены: {prices}")
    for offer in offers_node.findall("offer"):
        if not child_text(offer, "url"):
            raise RuntimeError(f"Нет URL у offer {offer.get('id')}")
        if "КОЩАКОВ" not in norm(child_text(offer, "vendor")):
            raise RuntimeError(f"Неверный производитель у offer {offer.get('id')}")

    indent(root)
    ET.ElementTree(root).write(TARGET, encoding="utf-8", xml_declaration=True)

    print(f"Создан {TARGET}: {len(ids)} предложения")
    for key, offer in zip(order, offers_node.findall("offer")):
        print(
            f"{key}: id={offer.get('id')} | {child_text(offer, 'name')} | "
            f"{child_text(offer, 'price')} RUB | {child_text(offer, 'url')}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
