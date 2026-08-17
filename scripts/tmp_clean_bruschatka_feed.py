#!/usr/bin/env python3
import re
import xml.etree.ElementTree as ET
from pathlib import Path

PATH = Path('akbars_bruschatka_yandex_direct_feed.yml')

tree = ET.parse(PATH)
root = tree.getroot()
removed = 0
for offer in root.findall('./shop/offers/offer'):
    for p in list(offer.findall('param')):
        if (p.text or '').strip().lower() == 'array':
            offer.remove(p)
            removed += 1
    desc = offer.find('description')
    if desc is not None and desc.text:
        text = desc.text
        text = re.sub(r'\s*Оттенки цветов:\s*Array\.?', '', text, flags=re.I)
        text = re.sub(r'\s*Цвет производителя:\s*Array\.?', '', text, flags=re.I)
        text = re.sub(r'\s{2,}', ' ', text).strip()
        desc.text = text

ET.indent(root, space='  ')
tree.write(PATH, encoding='utf-8', xml_declaration=True)
check = PATH.read_text(encoding='utf-8')
if '>Array<' in check or ': Array' in check:
    raise SystemExit('ERROR: Array garbage remains')
print(f'OK: removed {removed} Array params')
