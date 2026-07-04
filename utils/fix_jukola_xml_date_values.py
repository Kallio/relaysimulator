#!/usr/bin/env python3
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from collections import Counter
import sys

if len(sys.argv) != 3:
    print(f"Usage: {sys.argv[0]} input.xml output.xml")
    sys.exit(1)

input_file = sys.argv[1]
output_file = sys.argv[2]

tree = ET.parse(input_file)
root = tree.getroot()

# Detect namespace from root element (IOF namespace like http://www.orienteering.org/datastandard/3.0)
ns_uri = root.tag.split('}')[0].strip('{') if '}' in root.tag else ''
ns = f'{{{ns_uri}}}' if ns_uri else ''

# Register namespace so output preserves the original xmlns declaration
if ns_uri:
    ET.register_namespace('', ns_uri)

# --- First pass: collect all datetime values to detect evening-start dates ---
date_has_hour23 = Counter()
for tag_name in ('StartTime', 'FinishTime'):
    qualified = f'{ns}{tag_name}' if ns else tag_name
    for elem in root.iter(qualified):
        if elem.text is None:
            continue
        text = elem.text.strip()
        if not text:
            continue
        try:
            dt = datetime.strptime(text, "%Y-%m-%dT%H:%M:%S%z")
        except ValueError:
            try:
                dt = datetime.strptime(text, "%Y-%m-%dT%H:%M:%S")
            except ValueError:
                continue
        if dt.hour == 23:
            date_has_hour23[dt.date()] += 1

# A date with hour-23 values is an evening-start date (e.g. Jukola mass start at 23:00).
# Any time on that date with hour < 23 is likely a post-midnight data error
# and should be shifted to the next day.
evening_start_dates = set(date_has_hour23.keys())

if evening_start_dates:
    print(f"Detected evening-start date(s): {[str(d) for d in sorted(evening_start_dates)]}", file=sys.stderr)

# --- Second pass: fix dates ---
fixed_count = 0
for tag_name in ('StartTime', 'FinishTime'):
    qualified = f'{ns}{tag_name}' if ns else tag_name
    for elem in root.iter(qualified):
        if elem.text is None:
            continue
        text = elem.text.strip()
        if not text:
            continue

        try:
            dt = datetime.strptime(text, "%Y-%m-%dT%H:%M:%S%z")
        except ValueError:
            try:
                dt = datetime.strptime(text, "%Y-%m-%dT%H:%M:%S")
            except ValueError:
                continue

        if dt.date() in evening_start_dates and dt.hour < 23:
            dt += timedelta(days=1)
            fixed_count += 1

        if dt.tzinfo:
            tz = dt.strftime('%z')
            tz = f'{tz[:3]}:{tz[3:]}'
            elem.text = dt.strftime("%Y-%m-%dT%H:%M:%S") + tz
        else:
            elem.text = dt.strftime("%Y-%m-%dT%H:%M:%S")

tree.write(output_file, encoding='utf-8', xml_declaration=True)
print(f"Fixed {fixed_count} timestamps", file=sys.stderr)
