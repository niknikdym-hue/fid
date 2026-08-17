#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urljoin
import xml.etree.ElementTree as ET

import requests
from bs4 import BeautifulSoup

BASE = "https://akbarskeramic.ru"
FEED = Path("akbars_bruschatka_yandex_direct_feed.yml")
REPORT = Path("tmp-akbars-bruschatka-feed-report.json")
TARGET_IDS = {"8616", "8617", "8618", "8619"}
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; AkBarsDirectFeed/1.0)", "Accept-Language": "ru-RU,ru;q=0.9"}


def add_text(parent: ET.Element, tag: str, text: object) -> ET.Element:
    el = ET.SubElement(parent, tag)
    el.text = str(text)
    return el


def main() -> None:
    report = json.loads(REPORT.read_text(encoding="utf-8"))
    source_items = {str(x["id"]): x for x in report.get("suspicious_excluded", []) if str(x["id"]) in TARGET_IDS}
    missing = TARGET_IDS - set(source_items)
    if missing:
        raise SystemExit(f"Missing clinker items in report: {sorted(missing)}")

    tree = ET.parse(FEED)
    root = tree.getroot()
    offers = root.find("./shop/offers")
    if offers is None:
        raise SystemExit("No offers node")
    existing = {o.get("id") for o in offers.findall("offer")}

    added = 0
    for pid in sorted(TARGET_IDS, key=int):
        if pid in existing:
            continue
        item = source_items[pid]
        url = item["url"]
        r = requests.get(url, headers=HEADERS, timeout=(15, 60))
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        h1 = soup.find("h1")
        title = h1.get_text(" ", strip=True) if h1 else item["title"]
        og = soup.find("meta", attrs={"property": "og:image"})
        picture = urljoin(BASE, og.get("content")) if og and og.get("content") else ""
        price = float(item["price"])

        offer = ET.SubElement(offers, "offer", {"id": pid, "available": "true"})
        add_text(offer, "url", url)
        add_text(offer, "price", f"{price:.2f}")
        add_text(offer, "currencyId", "RUB")
        add_text(offer, "categoryId", "1")
        if picture:
            add_text(offer, "picture", picture)
        add_text(offer, "name", title)
        add_text(offer, "typePrefix", "Тротуарная плитка")
        add_text(offer, "vendor", "5 элемент")
        add_text(offer, "model", title)
        add_text(offer, "pickup", "true")
        add_text(offer, "delivery", "true")
        add_text(offer, "sales_notes", "Цена за шт. Самовывоз и доставка доступны по согласованию.")
        add_text(offer, "description", f"{title}. Цена — {price:g} руб./шт.")
        add_text(offer, "param", "шт.").set("name", "Единица измерения")
        added += 1

    ordered = sorted(offers.findall("offer"), key=lambda o: int(o.get("id", "0")))
    for o in offers.findall("offer"):
        offers.remove(o)
    for o in ordered:
        offers.append(o)

    ET.indent(root, space="  ")
    tree.write(FEED, encoding="utf-8", xml_declaration=True)

    report["suspicious_excluded"] = [x for x in report.get("suspicious_excluded", []) if str(x["id"]) not in TARGET_IDS]
    report["suspicious_excluded_count"] = len(report["suspicious_excluded"])
    report["included_offer_count"] = len(offers.findall("offer"))
    report["clinker_unit_corrected_to_piece"] = sorted(TARGET_IDS, key=int)
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    if len(offers.findall("offer")) != 64:
        raise SystemExit(f"Unexpected final offer count: {len(offers.findall('offer'))}")
    print(f"OK: added {added} clinker offers as per-piece; final offers={len(offers.findall('offer'))}")


if __name__ == "__main__":
    main()
