#!/usr/bin/env python3
"""Analyze an IOF-XML ResultList and report speed distributions, top-N runners,
status rates, and segment variance per leg.

Usage:
    python utils/analyze_results.py --iof data/results_j2025_ju_iof_fixed.xml
    python utils/analyze_results.py --iof data/results_j2025_ju_iof_fixed.xml --top 20
    python utils/analyze_results.py --iof data/results_j2025_ju_iof_fixed.xml --format json --out stats.json
"""
import argparse
import json
import math
import statistics
import xml.etree.ElementTree as ET
from collections import defaultdict


def infer_leg_from_course_name(name):
    if not name or len(name) < 4:
        return None
    if name[0] in ('J', 'V') and name[1:4].isdigit():
        return int(name[1])
    return None


def parse_resultlist(iof_path):
    tree = ET.parse(iof_path)
    root = tree.getroot()
    ns_uri = root.tag.split('}')[0].strip('{') if '}' in root.tag else None
    ns = {'iof': ns_uri} if ns_uri else None

    event_name_el = root.find('.//iof:Event/iof:Name', ns)
    event_name = event_name_el.text.strip() if event_name_el is not None and event_name_el.text else '?'
    class_name_el = root.find('.//iof:ClassResult/iof:Class/iof:Name', ns)
    class_name = class_name_el.text.strip() if class_name_el is not None and class_name_el.text else '?'

    runners = []  # list of dicts
    for team in root.findall('.//iof:TeamResult', ns):
        bib = team.findtext('iof:BibNumber', namespaces=ns) or ''
        org = team.find('iof:Organisation', ns)
        club = org.findtext('iof:Name', namespaces=ns) or '' if org is not None else ''
        team_name = team.findtext('iof:Name', namespaces=ns) or ''

        for idx, member in enumerate(team.findall('.//iof:TeamMemberResult', ns), start=1):
            result = member.find('iof:Result', ns)
            if result is None:
                continue

            person_el = member.find('iof:Person', ns)
            runner_name = ''
            if person_el is not None:
                name_el = person_el.find('iof:Name', ns)
                if name_el is not None:
                    given = name_el.findtext('iof:Given', namespaces=ns) or ''
                    family = name_el.findtext('iof:Family', namespaces=ns) or ''
                    runner_name = f"{given} {family}".strip()

            leg_txt = result.findtext('iof:Leg', namespaces=ns)
            leg = int(leg_txt) if leg_txt else idx

            status = result.findtext('iof:Status', namespaces=ns) or 'OK'
            time_txt = result.findtext('iof:Time', namespaces=ns)
            leg_time = int(time_txt) if time_txt else None

            course_name_el = result.find('iof:Course/iof:Name', ns)
            course_name = course_name_el.text.strip() if course_name_el is not None and course_name_el.text else ''
            course_len_el = result.find('iof:Course/iof:Length', ns)
            course_length = int(course_len_el.text) if course_len_el is not None and course_len_el.text else 0

            splits = []
            for st in result.findall('iof:SplitTime', ns):
                code = st.findtext('iof:ControlCode', namespaces=ns)
                t = st.findtext('iof:Time', namespaces=ns)
                if code and t:
                    splits.append((code.strip(), int(t)))

            overall_el = result.find('iof:OverallResult', ns)
            overall_time = None
            overall_pos = None
            if overall_el is not None:
                ot = overall_el.findtext('iof:Time', namespaces=ns)
                overall_time = int(ot) if ot else None
                op = overall_el.findtext('iof:Position', namespaces=ns)
                overall_pos = int(op) if op else None

            runners.append({
                'runner_name': runner_name,
                'club': club,
                'team_name': team_name,
                'bib': bib,
                'leg': leg,
                'status': status,
                'leg_time': leg_time,
                'course_name': course_name,
                'course_length': course_length,
                'splits': splits,
                'overall_time': overall_time,
                'overall_pos': overall_pos,
            })

    return event_name, class_name, runners


def compute_km_h(length_m, time_s):
    if not time_s or time_s <= 0 or not length_m:
        return None
    return (length_m / 1000.0) / (time_s / 3600.0)


def fmt_time(seconds):
    if seconds is None:
        return '-'
    seconds = int(round(seconds))
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def percentile(sorted_vals, p):
    if not sorted_vals:
        return None
    k = (len(sorted_vals) - 1) * (p / 100.0)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return sorted_vals[int(k)]
    return sorted_vals[f] * (c - k) + sorted_vals[c] * (k - f)


def print_event_summary(event_name, class_name, runners):
    teams = len(set(r['bib'] for r in runners))
    total = len(runners)
    n_legs = len(set(r['leg'] for r in runners))
    print()
    print(f"{'=' * 60}")
    print(f"  Event: {event_name}")
    print(f"  Class: {class_name}")
    print(f"  Teams: {teams}   Runners: {total}   Legs: {n_legs}")
    print(f"{'=' * 60}")


def print_status_distribution(runners):
    counts = defaultdict(int)
    for r in runners:
        counts[r['status']] += 1
    total = len(runners)
    print()
    print("  Overall status distribution:")
    for status in ['OK', 'DidNotStart', 'DidNotFinish', 'Disqualified', 'Finished']:
        c = counts.get(status, 0)
        if c > 0:
            pct = c * 100.0 / total
            print(f"    {status:<20s} {c:>6d}  ({pct:5.1f}%)")


def print_leg_stats(runners, top_n):
    by_leg = defaultdict(list)
    for r in runners:
        if r['leg_time'] and r['status'] == 'OK':
            by_leg[r['leg']].append(r)

    for leg in sorted(by_leg.keys()):
        leg_runners = by_leg[leg]
        speeds = [compute_km_h(r['course_length'], r['leg_time']) for r in leg_runners]
        speeds = [s for s in speeds if s is not None]
        times = [r['leg_time'] for r in leg_runners]
        lengths = set(r['course_length'] for r in leg_runners if r['course_length'])

        if not speeds:
            continue

        speeds_sorted = sorted(speeds)
        times_sorted = sorted(times)

        # Status counts for this leg (all runners, not just OK)
        leg_all = [r for r in runners if r['leg'] == leg]
        status_counts = defaultdict(int)
        for r in leg_all:
            status_counts[r['status']] += 1

        # Course variants
        course_variants = defaultdict(list)
        for r in leg_runners:
            if r['course_name']:
                course_variants[r['course_name']].append(r)

        course_lengths_str = ', '.join(f"{l}m" for l in sorted(lengths)) if lengths else '?'

        print()
        print(f"  {'─' * 56}")
        print(f"  Leg {leg}  ({len(leg_runners)} OK runners, course lengths: {course_lengths_str})")
        print(f"  {'─' * 56}")

        # Status rates
        total_leg = len(leg_all)
        status_parts = []
        for s in ['OK', 'DidNotStart', 'DidNotFinish', 'Disqualified']:
            c = status_counts.get(s, 0)
            if c > 0:
                status_parts.append(f"{s}={c * 100.0 / total_leg:.1f}%")
        print(f"  Status: {', '.join(status_parts)}")

        # Speed distribution
        print(f"  Speed (km/h):")
        print(f"    Min:  {min(speeds):5.2f}    P10: {percentile(speeds_sorted, 10):5.2f}"
              f"    P25: {percentile(speeds_sorted, 25):5.2f}"
              f"    Median: {percentile(speeds_sorted, 50):5.2f}")
        print(f"    Mean: {statistics.mean(speeds):5.2f}    P75: {percentile(speeds_sorted, 75):5.2f}"
              f"    P90: {percentile(speeds_sorted, 90):5.2f}"
              f"    Max: {max(speeds):5.2f}")
        if len(speeds) > 1:
            print(f"    Std dev: {statistics.stdev(speeds):.2f}")

        # Time distribution
        print(f"  Leg time:")
        print(f"    Fastest: {fmt_time(min(times_sorted))}   Median: {fmt_time(percentile(times_sorted, 50))}"
              f"   Slowest: {fmt_time(max(times_sorted))}")

        # Top-N
        leg_sorted = sorted(leg_runners, key=lambda r: r['leg_time'])[:top_n]
        print()
        print(f"  Top {min(top_n, len(leg_sorted))} fastest:")
        print(f"  {'#':>3s}  {'Runner':<28s} {'Club':<24s} {'Course':<8s} {'Dist':>6s} {'Time':>9s} {'km/h':>6s}")
        print(f"  {'─' * 3}  {'─' * 28} {'─' * 24} {'─' * 8} {'─' * 6} {'─' * 9} {'─' * 6}")
        for i, r in enumerate(leg_sorted, 1):
            kmh = compute_km_h(r['course_length'], r['leg_time'])
            dist_km = r['course_length'] / 1000.0 if r['course_length'] else 0
            print(f"  {i:>3d}  {r['runner_name']:<28s} {r['club']:<24s} {r['course_name']:<8s}"
                  f" {dist_km:5.1f}k {fmt_time(r['leg_time']):>9s} {kmh:5.2f}")


def print_segment_variance(runners):
    """For the most common course per leg, report per-segment time statistics."""
    # Group by course name
    by_course = defaultdict(list)
    for r in runners:
        if r['status'] == 'OK' and r['splits'] and r['course_name']:
            by_course[r['course_name']].append(r)

    if not by_course:
        return

    # Pick the most common course per leg
    leg_best = {}
    for r in runners:
        if r['status'] == 'OK' and r['course_name']:
            leg = r['leg']
            if leg not in leg_best or len(by_course[r['course_name']]) > len(by_course.get(leg_best[leg], [])):
                leg_best[leg] = r['course_name']

    print()
    print(f"  {'=' * 56}")
    print(f"  Segment variance (most common course per leg)")
    print(f"  {'=' * 56}")

    for leg in sorted(leg_best.keys()):
        course = leg_best[leg]
        course_runners = by_course[course]
        if len(course_runners) < 3:
            continue

        # Get canonical control order from the runner with most splits
        max_splits = max(course_runners, key=lambda r: len(r['splits']))
        canonical_codes = [code for code, _ in max_splits['splits']]

        # Compute segment times (delta between consecutive cumulative splits)
        segments = defaultdict(list)  # (code_from, code_to) -> list of segment times
        for r in course_runners:
            split_times = dict(r['splits'])
            prev_time = 0
            for code in canonical_codes:
                t = split_times.get(code)
                if t is not None:
                    seg = t - prev_time
                    if seg > 0:
                        segments[('start' if prev_time == 0 else canonical_codes[canonical_codes.index(code) - 1], code)].append(seg)
                    prev_time = t

        if not segments:
            continue

        print()
        print(f"  Leg {leg} — Course {course} ({len(course_runners)} runners)")
        print(f"  {'From':<10s} {'To':<10s} {'Mean(s)':>8s} {'StdDev':>8s} {'CV%':>7s} {'Min':>7s} {'Max':>7s}")
        print(f"  {'─' * 10} {'─' * 10} {'─' * 8} {'─' * 8} {'─' * 7} {'─' * 7} {'─' * 7}")

        for code_from, code_to in segments:
            vals = segments[(code_from, code_to)]
            if len(vals) < 3:
                continue
            mean = statistics.mean(vals)
            sd = statistics.stdev(vals) if len(vals) > 1 else 0
            cv = (sd / mean * 100) if mean > 0 else 0
            marker = ' <<<' if cv > 30 else ''
            print(f"  {code_from:<10s} {code_to:<10s} {mean:8.1f} {sd:8.1f} {cv:6.1f}% {min(vals):7d} {max(vals):7d}{marker}")


def print_calibration_summary(runners):
    """Print compact speed calibration summary for create_artificial_competitors.py."""
    ok_runners = [r for r in runners if r['status'] == 'OK' and r['leg_time']]
    all_speeds = []
    by_leg = defaultdict(list)
    for r in ok_runners:
        kmh = compute_km_h(r['course_length'], r['leg_time'])
        if kmh:
            all_speeds.append(kmh)
            by_leg[r['leg']].append(kmh)

    if not all_speeds:
        return

    all_sorted = sorted(all_speeds)
    total = len(runners)
    ok_count = sum(1 for r in runners if r['status'] == 'OK')
    dns = sum(1 for r in runners if r['status'] == 'DidNotStart')
    dnf = sum(1 for r in runners if r['status'] == 'DidNotFinish')
    dsq = sum(1 for r in runners if r['status'] == 'Disqualified')
    mp_count = 0  # MP is detected from course mismatch, not status in IOF-XML

    print()
    print(f"  {'=' * 56}")
    print(f"  Speed calibration summary")
    print(f"  {'=' * 56}")
    print()
    print(f"  Overall OK runners: {ok_count}")
    print(f"  Overall median speed: {percentile(all_sorted, 50):.1f} km/h")
    print(f"  Overall top-10 avg:   {statistics.mean(all_sorted[-10:]):.1f} km/h" if len(all_sorted) >= 10 else "")
    print(f"  Overall P10 speed:    {percentile(all_sorted, 10):.1f} km/h")

    print()
    print("  Per-leg speeds (km/h):")
    print(f"  {'Leg':>4s}  {'N':>5s}  {'P10':>6s}  {'P25':>6s}  {'Median':>6s}  {'P75':>6s}  {'P90':>6s}  {'Top10':>6s}")
    print(f"  {'─' * 4}  {'─' * 5}  {'─' * 6}  {'─' * 6}  {'─' * 6}  {'─' * 6}  {'─' * 6}  {'─' * 6}")
    for leg in sorted(by_leg.keys()):
        s = sorted(by_leg[leg])
        n = len(s)
        top10_avg = statistics.mean(s[-10:]) if n >= 10 else statistics.mean(s)
        print(f"  {leg:>4d}  {n:>5d}"
              f"  {percentile(s, 10):6.1f}"
              f"  {percentile(s, 25):6.1f}"
              f"  {percentile(s, 50):6.1f}"
              f"  {percentile(s, 75):6.1f}"
              f"  {percentile(s, 90):6.1f}"
              f"  {top10_avg:6.1f}")

    print()
    print("  Status rates (for create_artificial_competitors.py):")
    print(f"    OK={ok_count * 100.0 / total:.1f}%, DNS={dns * 100.0 / total:.1f}%,"
          f" DNF={dnf * 100.0 / total:.1f}%, DSQ={dsq * 100.0 / total:.1f}%")


def output_json(runners, event_name, class_name, top_n, out_path):
    ok_runners = [r for r in runners if r['status'] == 'OK' and r['leg_time']]
    all_speeds = []
    by_leg = defaultdict(list)
    for r in ok_runners:
        kmh = compute_km_h(r['course_length'], r['leg_time'])
        if kmh:
            all_speeds.append(kmh)
            by_leg[r['leg']].append(kmh)

    all_sorted = sorted(all_speeds) if all_speeds else []
    total = len(runners)

    data = {
        'event_name': event_name,
        'class_name': class_name,
        'total_teams': len(set(r['bib'] for r in runners)),
        'total_runners': total,
        'status_counts': {},
        'overall_speed': {},
        'legs': {},
    }

    for status in ['OK', 'DidNotStart', 'DidNotFinish', 'Disqualified', 'Finished']:
        c = sum(1 for r in runners if r['status'] == status)
        if c > 0:
            data['status_counts'][status] = {'count': c, 'percent': round(c * 100.0 / total, 2)}

    if all_sorted:
        data['overall_speed'] = {
            'median': round(percentile(all_sorted, 50), 2),
            'top10_avg': round(statistics.mean(all_sorted[-10:]), 2) if len(all_sorted) >= 10 else None,
            'p10': round(percentile(all_sorted, 10), 2),
            'p25': round(percentile(all_sorted, 25), 2),
            'p75': round(percentile(all_sorted, 75), 2),
            'p90': round(percentile(all_sorted, 90), 2),
        }

    for leg in sorted(by_leg.keys()):
        s = sorted(by_leg[leg])
        leg_all = [r for r in runners if r['leg'] == leg]
        leg_ok = [r for r in leg_all if r['status'] == 'OK']
        leg_status = {}
        for status in ['OK', 'DidNotStart', 'DidNotFinish', 'Disqualified']:
            c = sum(1 for r in leg_all if r['status'] == status)
            if c > 0:
                leg_status[status] = c

        top_runners = sorted(
            [r for r in runners if r['leg'] == leg and r['status'] == 'OK' and r['leg_time']],
            key=lambda r: r['leg_time']
        )[:top_n]

        data['legs'][leg] = {
            'ok_count': len(leg_ok),
            'status': leg_status,
            'speed': {
                'median': round(percentile(s, 50), 2),
                'p10': round(percentile(s, 10), 2),
                'p25': round(percentile(s, 25), 2),
                'p75': round(percentile(s, 75), 2),
                'p90': round(percentile(s, 90), 2),
            },
            'top_n': [{
                'rank': i + 1,
                'runner': r['runner_name'],
                'club': r['club'],
                'course': r['course_name'],
                'km_h': round(compute_km_h(r['course_length'], r['leg_time']) or 0, 2),
                'time': r['leg_time'],
            } for i, r in enumerate(top_runners)],
        }

    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"  JSON written to {out_path}")


def main():
    p = argparse.ArgumentParser(description="Analyze IOF-XML ResultList for speed distributions and statistics")
    p.add_argument('--iof', required=True, help="Path to IOF-XML ResultList file")
    p.add_argument('--top', type=int, default=10, help="Number of top runners to show per leg (default: 10)")
    p.add_argument('--format', choices=['table', 'json'], default='table', help="Output format (default: table)")
    p.add_argument('--out', help="Output file path (required for --format json)")
    p.add_argument('--segments', action='store_true', help="Show per-segment variance analysis")
    args = p.parse_args()

    print(f"  Parsing {args.iof}...")
    event_name, class_name, runners = parse_resultlist(args.iof)

    if args.format == 'json':
        if not args.out:
            p.error("--out is required when using --format json")
        output_json(runners, event_name, class_name, args.top, args.out)
        return

    print_event_summary(event_name, class_name, runners)
    print_status_distribution(runners)
    print_leg_stats(runners, args.top)

    if args.segments:
        print_segment_variance(runners)

    print_calibration_summary(runners)
    print()


if __name__ == '__main__':
    main()
