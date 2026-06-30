#!/usr/bin/env python3
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
import sys

if len(sys.argv) != 3:
    print(f"Usage: {sys.argv[0]} input.xml output.xml")
    sys.exit(1)

input_file = sys.argv[1]
output_file = sys.argv[2]

# Lue XML
tree = ET.parse(input_file)
root = tree.getroot()

# Poista kaikki namespace-prefiksit
for elem in root.iter():
    if '}' in elem.tag:
        elem.tag = elem.tag.split('}', 1)[1]

# Käy läpi kaikki StartTime ja FinishTime
for tag_name in ['StartTime', 'FinishTime']:
    for elem in root.iter(tag_name):
        if elem.text is None:
            continue
        text = elem.text.strip()
        if not text:
            continue

        # Erotetaan aikaleima ja aikavyöhyke
        if '+' in text:
            dt_str, tz = text.split('+')
            tz = '+' + tz
        elif '-' in text[10:]:
            dt_str, tz = text.split('-')
            tz = '-' + tz
        else:
            dt_str = text
            tz = ''

        dt = datetime.strptime(dt_str, "%Y-%m-%dT%H:%M:%S")
        # Lisää päivä vain jos tunti < 23 ja päivä on 14
        if dt.hour < 23 and dt.day == 14:
            dt += timedelta(days=1)

        elem.text = dt.strftime("%Y-%m-%dT%H:%M:%S") + tz

# Tallenna XML ilman prefiksiä
tree.write(output_file, encoding='utf-8', xml_declaration=True)
