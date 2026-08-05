#!/usr/bin/env python3
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from pathlib import Path
import xml.etree.ElementTree as ET

FEED = Path("akbars_yandex_business_feed.yml")
REPORT = Path("tmp-akbars-business-feed-report.json")
JBI_RE = re.compile(r"(?:железобетон|\bжби\b|сваи|плит[аы] перекрытия|фбс|кольц[ао] колод)", re.I)


def text(node: ET.Element, tag: str) -> str:
    child = node.find(tag)
    return (child.text or "").strip() if child is not None else ""


def main() -> int:
    root = ET.parse(FEED).getroot()
    categories = {
        c.get("id", ""): (c.text or "").strip()
        for c in root.findall("./shop/categories/category")
    }
    offers = root.findall("./shop/offers/offer")
    counts = Counter(text(o, "categoryId") for o in offers)
    examples: dict[str, list[dict[str, str]]] = defaultdict(list)
    jbi_offer_ids: list[str] = []
    tags = Counter()

    for offer in offers:
        cid = text(offer, "categoryId")
        tags.update(child.tag for child in offer)
        sample = {
            "id": offer.get("id", ""),
            "name": text(offer, "name"),
            "url": text(offer, "url"),
            "price": text(offer, "price"),
        }
        if len(examples[cid]) < 3:
            examples[cid].append(sample)
        haystack = " ".join([
            categories.get(cid, ""),
            text(offer, "name"),
            text(offer, "typePrefix"),
            text(offer, "model"),
            text(offer, "description"),
            text(offer, "url"),
        ])
        if JBI_RE.search(haystack):
            jbi_offer_ids.append(offer.get("id", ""))

    report = {
        "root_tag": root.tag,
        "catalog_date": root.get("date"),
        "offer_count": len(offers),
        "categories": [
            {
                "id": cid,
                "name": name,
                "offer_count": counts.get(cid, 0),
                "looks_like_jbi": bool(JBI_RE.search(name)),
                "examples": examples.get(cid, []),
            }
            for cid, name in categories.items()
        ],
        "offer_child_tags": dict(tags),
        "jbi_like_offer_count": len(jbi_offer_ids),
        "jbi_like_offer_ids_first_30": jbi_offer_ids[:30],
    }
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
