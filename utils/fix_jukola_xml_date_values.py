#!/usr/bin/env python3
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from collections import Counter, defaultdict
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

# --- First pass: collect statistics and detect evening-start dates ---
# stats[tag][date] -> Counter of hours
stats: dict[str, dict[str, Counter]] = {t: defaultdict(Counter) for t in ('StartTime', 'FinishTime')}
date_has_hour23 = Counter()
total_scanned = 0

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
        total_scanned += 1
        date_key = dt.strftime("%Y-%m-%d")
        stats[tag_name][date_key][dt.hour] += 1
        if dt.hour == 23:
            date_has_hour23[date_key] += 1

evening_start_dates = set(date_has_hour23.keys())

# --- Print diagnostic summary to stderr ---
print("=" * 60, file=sys.stderr)
print(f"  Input: {input_file}", file=sys.stderr)
print(f"  Total timestamps scanned: {total_scanned}", file=sys.stderr)
st_total = sum((c.total() for c in stats['StartTime'].values()), start=0)
ft_total = sum((c.total() for c in stats['FinishTime'].values()), start=0)
print(f"  StartTime values: {st_total}", file=sys.stderr)
print(f"  FinishTime values: {ft_total}", file=sys.stderr)
print(file=sys.stderr)

# Date distribution before fix
all_dates = sorted(set(list(stats['StartTime'].keys()) + list(stats['FinishTime'].keys())))
print("  Date distribution before fix:", file=sys.stderr)
for date in all_dates:
    st_count = sum(stats['StartTime'][date].values()) if date in stats['StartTime'] else 0
    ft_count = sum(stats['FinishTime'][date].values()) if date in stats['FinishTime'] else 0
    combined = Counter(stats['StartTime'].get(date, Counter()))
    combined += stats['FinishTime'].get(date, Counter())
    hours_present = sorted(combined.keys())
    hour_range = f"{hours_present[0]:02d}-{hours_present[-1]:02d}" if hours_present else "none"
    marker = "  <-- evening start" if date in evening_start_dates else ""
    print(f"    {date}: {st_count:>6} starts, {ft_count:>6} finishes, hours {hour_range}{marker}", file=sys.stderr)

print(file=sys.stderr)

if evening_start_dates:
    print(f"  Detected evening-start date(s): {sorted(evening_start_dates)}", file=sys.stderr)
    print(f"  (dates with timestamps at hour 23 — mass start time)", file=sys.stderr)
    print(f"  Timestamps on these dates with hour < 23 will be shifted +1 day.", file=sys.stderr)
else:
    print("  No evening-start dates detected (no timestamps at hour 23).", file=sys.stderr)
    print("  No timestamps will be modified.", file=sys.stderr)

# --- Second pass: fix dates ---
fixed_count = 0
fixed_by_tag = Counter()
fixed_by_date = Counter()

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

        orig_date = dt.strftime("%Y-%m-%d")
        if orig_date in evening_start_dates and dt.hour < 23:
            dt += timedelta(days=1)
            fixed_count += 1
            fixed_by_tag[tag_name] += 1
            fixed_by_date[orig_date] += 1

        if dt.tzinfo:
            tz = dt.strftime('%z')
            tz = f'{tz[:3]}:{tz[3:]}'
            elem.text = dt.strftime("%Y-%m-%dT%H:%M:%S") + tz
        else:
            elem.text = dt.strftime("%Y-%m-%dT%H:%M:%S")

tree.write(output_file, encoding='utf-8', xml_declaration=True)

print(file=sys.stderr)
if fixed_count > 0:
    print(f"  Fixed {fixed_count} timestamps:", file=sys.stderr)
    print(f"    StartTime: {fixed_by_tag['StartTime']}", file=sys.stderr)
    print(f"    FinishTime: {fixed_by_tag['FinishTime']}", file=sys.stderr)
    for date in sorted(fixed_by_date):
        print(f"    From {date} -> next day: {fixed_by_date[date]} timestamps", file=sys.stderr)
else:
    print("  No timestamps needed fixing.", file=sys.stderr)

print(f"  Output: {output_file}", file=sys.stderr)
print("=" * 60, file=sys.stderr)
