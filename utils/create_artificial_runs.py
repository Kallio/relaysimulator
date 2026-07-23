#!/usr/bin/env python3
"""Generate a synthetic IOF-XML ResultList for real teams from a CSV registration file.

Reads a CourseData XML file for courses/controls, a CSV file with actual team
registrations (team names, runner names, bib numbers), optionally reads an
existing ResultList for speed calibration, and outputs a fully synthetic
ResultList compatible with simulator.py.

Usage:
    # Non-interactive (all args via CLI)
    python utils/create_artificial_runs.py \
      --courses data/runners.j2025_ju_iof_fixed_courses.xml \
      --teams-csv data/teams.csv \
      --ref-results data/results_j2025_ju_iof_fixed.xml \
      --out data/artificial_runs.xml --seed 42

    # Interactive (missing args are prompted)
    python utils/create_artificial_runs.py \
      --courses data/runners.j2025_ju_iof_fixed_courses.xml \
      --teams-csv data/teams.csv
"""
import argparse
import csv
import math
import random
import re
import statistics
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import datetime, timedelta, timezone

IOF_NS = "http://www.orienteering.org/datastandard/3.0"

FI_GIVEN = [
    "Mikko", "Jari", "Pekka", "Juha", "Matti", "Timo", "Antti", "Kimmo",
    "Ville", "Janne", "Jussi", "Marko", "Sami", "Tero", "Kari", "Risto",
    "Heikki", "Petri", "Seppo", "Reijo", "Harri", "Jukka", "Saku", "Leo",
    "Onni", "Eero", "Aapo", "Olavi", "Veli", "Kalle",
]

FI_FAMILY = [
    "Virtanen", "Korhonen", "Mäkinen", "Nieminen", "Hämäläinen",
    "Laine", "Heikkinen", "Koskinen", "Järvinen", "Lehtonen",
    "Saarinen", "Tuominen", "Salonen", "Kivinen", "Johansson",
    "Niemi", "Hietala", "Ahonen", "Salo", "Lehtinen",
    "Rantanen", "Kelkka", "Anttila", "Kallio", "Väisänen",
    "Miettinen", "Pitkänen", "Laakso", "Mustonen", "Hirsjärvi",
]

SE_GIVEN = [
    "Olle", "Jesper", "Viktor", "Erik", "Anders", "Gustav", "Lars",
    "Per", "Carl", "Nils", "Henrik", "Martin", "Fredrik", "Johan",
    "David", "Emil", "Filip", "Anton", "Axel", "Oscar",
]

SE_FAMILY = [
    "Svensk", "Andersson", "Johansson", "Karlsson", "Nilsson",
    "Eriksson", "Larsson", "Olsson", "Persson", "Berg",
    "Lindberg", "Holm", "Arvidsson", "Björk", "Lund",
    "Ström", "Engström", "Bergman", "Hedlund", "Sandberg",
]

TBD_PATTERNS = re.compile(r'^\s*(TBD|TBA|tbd|tba|-+)\s*$')


def infer_leg_from_course_name(name):
    if not name or len(name) < 4:
        return None
    if name[0] in ('J', 'V') and name[1:4].isdigit():
        return int(name[1])
    return None


def parse_coursedata(courses_path):
    """Parse CourseData XML, return dict: leg -> {course_name -> {controls: [...], lengths: [...]}}"""
    tree = ET.parse(courses_path)
    root = tree.getroot()
    ns_uri = root.tag.split('}')[0].strip('{') if '}' in root.tag else IOF_NS
    ns = {'iof': ns_uri}

    controls = {}
    for ctrl in root.findall('.//iof:RaceCourseData/iof:Control', ns):
        cid_el = ctrl.find('iof:Id', ns)
        if cid_el is not None and cid_el.text:
            controls[cid_el.text.strip()] = ctrl

    courses_by_leg = defaultdict(dict)
    for course in root.findall('.//iof:RaceCourseData/iof:Course', ns):
        name_el = course.find('iof:Name', ns)
        if name_el is None or not name_el.text:
            continue
        course_name = name_el.text.strip()
        leg = infer_leg_from_course_name(course_name)
        if leg is None:
            continue

        length_el = course.find('iof:Length', ns)
        course_length = int(length_el.text) if length_el is not None and length_el.text else 0
        if course_length == 0:
            course_length = DEFAULT_COURSE_LENGTHS.get(leg, 10000)

        ordered_controls = []
        leg_lengths = []
        for cc in course.findall('iof:CourseControl', ns):
            cc_type = cc.get('type', '')
            ctrl_el = cc.find('iof:Control', ns)
            if ctrl_el is None or not ctrl_el.text:
                continue
            ctrl_code = ctrl_el.text.strip()
            ll_el = cc.find('iof:LegLength', ns)
            ll = int(ll_el.text) if ll_el is not None and ll_el.text else 0
            ordered_controls.append((ctrl_code, cc_type))
            if ll > 0:
                leg_lengths.append(ll)

        courses_by_leg[leg][course_name] = {
            'controls': ordered_controls,
            'leg_lengths': leg_lengths,
            'length': course_length,
        }

    return dict(courses_by_leg)


def parse_teams_csv(csv_path):
    """Parse team registration CSV.

    Expected columns (auto-detected):
      "Kilpailunumero" - bib number
      "Sarja"          - class name
      "Joukkueen nimi" - team name
      "Nimi-N"         - runner name for leg N (auto-detect N)

    Returns list of dicts:
      [{'bib': int, 'class': str, 'team_name': str,
        'runners': [{'given': str, 'family': str} | None, ...]}, ...]
    """
    with open(csv_path, 'r', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f, delimiter=',', quotechar='"')
        fieldnames = reader.fieldnames

        if not fieldnames:
            print("  ERROR: CSV file has no header row.")
            sys.exit(1)

        # Auto-detect leg count from Nimi-N columns
        leg_columns = {}
        for col in fieldnames:
            m = re.match(r'^Nimi-(\d+)$', col.strip())
            if m:
                leg_num = int(m.group(1))
                leg_columns[leg_num] = col.strip()

        if not leg_columns:
            print("  ERROR: No 'Nimi-N' columns found in CSV. Cannot detect legs.")
            sys.exit(1)

        n_legs = max(leg_columns.keys())
        print(f"  Detected {n_legs} legs from CSV columns (Nimi-1 .. Nimi-{n_legs})")

        # Find required columns (handle potential whitespace/BOM in headers)
        def find_col(name):
            for col in fieldnames:
                if col.strip().replace('\ufeff', '') == name:
                    return col
            return None

        col_bib = find_col('Kilpailunumero')
        col_class = find_col('Sarja')
        col_team = find_col('Joukkueen nimi')

        if not col_bib or not col_team:
            print("  ERROR: Required columns 'Kilpailunumero' and/or 'Joukkueen nimi' not found.")
            print(f"  Found columns: {fieldnames}")
            sys.exit(1)

        teams = []
        for row in reader:
            bib_str = row.get(col_bib, '').strip().strip('"')
            team_name = row.get(col_team, '').strip().strip('"')
            class_name = row.get(col_class, '').strip().strip('"') if col_class else ''

            if not bib_str or not team_name:
                continue

            try:
                bib = int(bib_str)
            except ValueError:
                print(f"  WARNING: Skipping row with invalid bib '{bib_str}' (team: {team_name})")
                continue

            runners = []
            for leg in range(1, n_legs + 1):
                col_name = leg_columns.get(leg)
                if not col_name:
                    runners.append(None)
                    continue

                raw_name = row.get(col_name, '').strip().strip('"')

                # Handle "Name1 & Name2" format — take the name for this leg
                if ' & ' in raw_name:
                    parts = [p.strip() for p in raw_name.split(' & ')]
                    # Use the part matching this leg index (1-based), or last part if out of range
                    idx = min(leg - 1, len(parts) - 1)
                    raw_name = parts[idx] if idx < len(parts) else ''

                if not raw_name or TBD_PATTERNS.match(raw_name):
                    runners.append(None)  # placeholder, will generate synthetic name
                else:
                    # Split "FirstName LastName" into given/family
                    name_parts = raw_name.rsplit(' ', 1)
                    if len(name_parts) == 2:
                        runners.append({'given': name_parts[0], 'family': name_parts[1]})
                    elif len(name_parts) == 1:
                        runners.append({'given': name_parts[0], 'family': ''})
                    else:
                        runners.append(None)

            teams.append({
                'bib': bib,
                'class': class_name,
                'team_name': team_name,
                'runners': runners,
            })

    if not teams:
        print("  ERROR: No valid teams found in CSV.")
        sys.exit(1)

    # Sort by bib
    teams.sort(key=lambda t: t['bib'])
    return teams, n_legs


def analyze_ref_results(ref_path):
    """Parse a reference ResultList and return per-leg speed distributions."""
    tree = ET.parse(ref_path)
    root = tree.getroot()
    ns_uri = root.tag.split('}')[0].strip('{') if '}' in root.tag else IOF_NS
    ns = {'iof': ns_uri}

    stats = {}

    for team in root.findall('.//iof:TeamResult', ns):
        for member in team.findall('.//iof:TeamMemberResult', ns):
            result = member.find('iof:Result', ns)
            if result is None:
                continue

            leg_txt = result.findtext('iof:Leg', namespaces=ns)
            leg = int(leg_txt) if leg_txt else None
            if leg is None:
                continue

            status = result.findtext('iof:Status', namespaces=ns) or 'OK'
            time_txt = result.findtext('iof:Time', namespaces=ns)
            course_len_el = result.find('iof:Course/iof:Length', ns)
            course_length = int(course_len_el.text) if course_len_el is not None and course_len_el.text else 0

            if leg not in stats:
                stats[leg] = {'speeds': [], 'status_counts': defaultdict(int)}

            stats[leg]['status_counts'][status] += 1

            if status == 'OK' and time_txt and course_length:
                try:
                    leg_time = int(time_txt)
                    kmh = (course_length / 1000.0) / (leg_time / 3600.0)
                    if 1.0 < kmh < 15.0:
                        stats[leg]['speeds'].append(kmh)
                except (ValueError, ZeroDivisionError):
                    pass

    return stats


def percentile(sorted_vals, p):
    if not sorted_vals:
        return 0
    k = (len(sorted_vals) - 1) * (p / 100.0)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return sorted_vals[int(k)]
    return sorted_vals[f] * (c - k) + sorted_vals[c] * (k - f)


def build_speed_sampler(ref_stats, legs, speed_mode, avg_speed=None, speed_variance=None):
    """Return a function: (runner_id, leg) -> km_h that samples realistic speeds."""
    if speed_mode == 'manual':
        base = avg_speed or 6.5
        var = (speed_variance or 15) / 100.0

        def sampler(runner_id, leg, rng):
            s = base * (1 + max(-0.4, min(0.4, rng.gauss(0, var))))
            return max(1.5, s)
        return sampler

    if not ref_stats:
        defaults = {1: 6.4, 2: 7.2, 3: 5.8, 4: 6.9, 5: 7.5, 6: 6.1, 7: 5.5}
        base = avg_speed or 6.5
        var = (speed_variance or 15) / 100.0

        def sampler(runner_id, leg, rng):
            ref = defaults.get(leg, base)
            return max(1.5, ref * (1 + max(-0.4, min(0.4, rng.gauss(0, var)))))
        return sampler

    if speed_mode == 'top10':
        leg_medians = {}
        leg_top10 = {}
        for leg in sorted(ref_stats.keys()):
            s = sorted(ref_stats[leg]['speeds'])
            if s:
                leg_medians[leg] = percentile(s, 50)
                leg_top10[leg] = statistics.mean(s[-10:]) if len(s) >= 10 else statistics.mean(s)

        overall_top10 = statistics.mean([v for v in leg_top10.values()]) if leg_top10 else 6.5
        leg_ratios = {}
        for leg in leg_medians:
            leg_ratios[leg] = leg_medians[leg] / overall_top10 if overall_top10 > 0 else 0.8

        def sampler(runner_id, leg, rng):
            ratio = leg_ratios.get(leg, 0.8)
            base = overall_top10 * ratio
            s = base * (1 + max(-0.4, min(0.4, rng.gauss(0, 0.12))))
            return max(1.5, s)
        return sampler

    # percentile mode (default)
    leg_speeds = {}
    for leg in sorted(ref_stats.keys()):
        s = sorted(ref_stats[leg]['speeds'])
        if s:
            leg_speeds[leg] = s

    def sampler(runner_id, leg, rng):
        speeds = leg_speeds.get(leg)
        if not speeds:
            speeds = sorted([s for ls in leg_speeds.values() for s in ls])
        if not speeds:
            return 6.5
        s_min = speeds[0]
        s_max = speeds[-1]
        runner_rng = random.Random(hash(runner_id))
        sample = runner_rng.betavariate(2.5, 3.0)
        return max(s_min * 0.9, s_min + sample * (s_max - s_min * 0.9))

    return sampler


DEFAULT_COURSE_LENGTHS = {
    1: 12000, 2: 10000, 3: 13000, 4: 6200,
    5: 6100, 6: 11000, 7: 15000,
}


def generate_splits(controls, leg_lengths, total_time):
    """Distribute total_time across controls proportionally by leg_lengths."""
    if not controls or len(controls) < 2:
        return []

    valid_lengths = [l for l in leg_lengths if l > 0] if leg_lengths else []
    n_segs = len(valid_lengths) if valid_lengths else len(controls) - 1

    if n_segs == 0:
        return [(controls[-1][0], int(total_time))]

    avg_seg = total_time / n_segs
    seg_times = []
    for i in range(n_segs):
        noise = random.uniform(0.92, 1.08)
        seg_times.append(avg_seg * noise)

    current_total = sum(seg_times)
    if current_total > 0:
        scale = total_time / current_total
        seg_times = [t * scale for t in seg_times]

    cumulative = 0
    result = []
    seg_idx = 0
    for ctrl_code, ctrl_type in controls:
        if ctrl_type == 'Start':
            continue
        if seg_idx < len(seg_times):
            cumulative += seg_times[seg_idx]
            seg_idx += 1
        else:
            cumulative = total_time
        result.append((ctrl_code, int(round(cumulative))))

    if result:
        result[-1] = (result[-1][0], int(round(total_time)))

    return result


def generate_runner_name(rng):
    use_swedish = rng.random() < 0.15
    if use_swedish:
        given = rng.choice(SE_GIVEN)
        family = rng.choice(SE_FAMILY)
    else:
        given = rng.choice(FI_GIVEN)
        family = rng.choice(FI_FAMILY)
    return given, family


def format_iso(dt):
    return dt.strftime('%Y-%m-%dT%H:%M:%S+03:00')


def prompt(msg, default=None, type_fn=str):
    if default is not None:
        display = f" [{default}]"
    else:
        display = ""
    while True:
        raw = input(f"? {msg}{display}: ").strip()
        if not raw and default is not None:
            return type_fn(default) if type_fn != str else default
        if not raw and default is None:
            print("  Please enter a value.")
            continue
        try:
            return type_fn(raw)
        except (ValueError, TypeError):
            print(f"  Invalid value. Expected {type_fn.__name__}.")


def interactive_setup(courses_by_leg, ref_stats, args, n_legs):
    """Fill in missing args via interactive prompts."""
    print()
    event_default = "Synthetic Relay Race" if n_legs > 1 else "Synthetic Individual Race"

    if not args.event_name:
        args.event_name = prompt("Event name", default=event_default)

    if not args.mass_start:
        args.mass_start = prompt("Mass start time", default="2025-06-14T23:00:00+03:00")

    if ref_stats:
        print()
        print("  Per-leg speed (km/h) from reference data:")
        for leg in sorted(ref_stats.keys()):
            s = sorted(ref_stats[leg]['speeds'])
            if s:
                top10_avg = statistics.mean(s[-10:]) if len(s) >= 10 else statistics.mean(s)
                print(f"    Leg {leg}: median={percentile(s, 50):.1f}, "
                      f"top10={top10_avg:.1f}, P10={percentile(s, 10):.1f}")
        total = sum(sum(v['status_counts'].values()) for v in ref_stats.values())
        status_strs = []
        for status in ['OK', 'DidNotStart', 'DidNotFinish', 'Disqualified']:
            c = sum(v['status_counts'].get(status, 0) for v in ref_stats.values())
            if c > 0:
                status_strs.append(f"{status}={c * 100.0 / total:.1f}%")
        print(f"    Status rates: {', '.join(status_strs)}")

    if not args.speed_mode:
        if ref_stats:
            print()
            print("  Speed modes:")
            print("    percentile — realistic distribution matching real data (recommended)")
            print("    top10      — elite-calibrated, base speed from top-10 average")
            print("    manual     — single avg speed + variance percentage")
            args.speed_mode = prompt("Speed mode", default="percentile")
        else:
            args.speed_mode = "manual"

    if args.speed_mode == 'manual':
        if args.avg_speed is None:
            args.avg_speed = prompt("Average runner speed in km/h (typical orienteering relay: 5-8)", default=6.5, type_fn=float)
        if args.speed_variance is None:
            args.speed_variance = prompt("Speed variance +/- % (15 = runners vary +/- 15%)", default=15, type_fn=float)
    else:
        if args.avg_speed is None:
            args.avg_speed = None
        if args.speed_variance is None:
            args.speed_variance = None

    dns_default = 0.01
    dnf_default = 0.02
    mp_default = 0.01
    dsq_default = 0.005

    if ref_stats:
        total = sum(sum(v['status_counts'].values()) for v in ref_stats.values())
        if total > 0:
            dns_default = round(sum(v['status_counts'].get('DidNotStart', 0) for v in ref_stats.values()) / total, 3)
            dnf_default = round(sum(v['status_counts'].get('DidNotFinish', 0) for v in ref_stats.values()) / total, 3)
            dsq_default = round(sum(v['status_counts'].get('Disqualified', 0) for v in ref_stats.values()) / total, 3)

    if args.dnf_rate is None:
        args.dnf_rate = prompt(f"DNF rate", default=dnf_default, type_fn=float)
    if args.mp_rate is None:
        args.mp_rate = prompt("Missing punch rate", default=mp_default, type_fn=float)
    if args.dsq_rate is None:
        args.dsq_rate = prompt("DSQ rate", default=dsq_default, type_fn=float)
    if args.dns_rate is None:
        args.dns_rate = prompt("DNS rate", default=dns_default, type_fn=float)

    if args.seed is None:
        seed_str = prompt("Random seed (empty for random)", default=None)
        args.seed = int(seed_str) if seed_str else None


def build_resultlist_xml(event_name, mass_start_dt, teams_data, class_name="Relay"):
    """Build IOF-XML ResultList ElementTree from generated team data."""
    root = ET.Element("ResultList")
    root.set("xmlns", IOF_NS)
    root.set("xmlns:xsi", "http://www.w3.org/2001/XMLSchema-instance")
    root.set("iofVersion", "3.0")
    root.set("createTime", datetime.now(timezone.utc).isoformat())
    root.set("creator", "create_artificial_runs.py")

    event_el = ET.SubElement(root, "Event")
    ET.SubElement(event_el, "Id")
    ET.SubElement(event_el, "Name").text = event_name
    start_el = ET.SubElement(event_el, "StartTime")
    ET.SubElement(start_el, "Date").text = mass_start_dt.strftime('%Y-%m-%d')

    class_result = ET.SubElement(root, "ClassResult")
    class_el = ET.SubElement(class_result, "Class")
    ET.SubElement(class_el, "Id").text = "0"
    ET.SubElement(class_el, "Name").text = class_name

    for team in teams_data:
        tr = ET.SubElement(class_result, "TeamResult")
        ET.SubElement(tr, "Name").text = team['team_name']
        org = ET.SubElement(tr, "Organisation")
        ET.SubElement(org, "Name").text = team['team_name']
        ET.SubElement(tr, "BibNumber").text = str(team['bib'])

        for leg_data in team['legs']:
            tmr = ET.SubElement(tr, "TeamMemberResult")
            person = ET.SubElement(tmr, "Person")
            name_el = ET.SubElement(person, "Name")
            ET.SubElement(name_el, "Family").text = leg_data['family']
            ET.SubElement(name_el, "Given").text = leg_data['given']

            res = ET.SubElement(tmr, "Result")
            ET.SubElement(res, "Leg").text = str(leg_data['leg'])
            ET.SubElement(res, "LegOrder").text = "1"
            ET.SubElement(res, "BibNumber").text = str(team['bib'])
            ET.SubElement(res, "StartTime").text = format_iso(leg_data['start_time'])

            status = leg_data['status']
            has_splits = status in ('OK', 'DidNotFinish', 'Disqualified')

            if has_splits and leg_data.get('finish_time'):
                ET.SubElement(res, "FinishTime").text = format_iso(leg_data['finish_time'])
                ET.SubElement(res, "Time").text = str(leg_data['leg_time'])

            if leg_data.get('time_behind') is not None:
                ET.SubElement(res, "TimeBehind").set("type", "Leg")
                res.find("TimeBehind").text = str(leg_data['time_behind'])

            if leg_data.get('position') is not None:
                ET.SubElement(res, "Position").set("type", "Leg")
                res.find("Position").text = str(leg_data['position'])

            ET.SubElement(res, "Status").text = status

            overall = ET.SubElement(res, "OverallResult")
            if leg_data.get('overall_time') is not None:
                ET.SubElement(overall, "Time").text = str(leg_data['overall_time'])
            if leg_data.get('overall_behind') is not None:
                ET.SubElement(overall, "TimeBehind").text = str(leg_data['overall_behind'])
            if leg_data.get('overall_pos') is not None:
                ET.SubElement(overall, "Position").text = str(leg_data['overall_pos'])
            ET.SubElement(overall, "Status").text = status

            course_el = ET.SubElement(res, "Course")
            ET.SubElement(course_el, "Name").text = leg_data['course_name']
            ET.SubElement(course_el, "Length").text = str(leg_data['course_length'])

            if has_splits and leg_data.get('splits'):
                for ctrl_code, cum_time in leg_data['splits']:
                    st = ET.SubElement(res, "SplitTime")
                    ET.SubElement(st, "ControlCode").text = ctrl_code
                    ET.SubElement(st, "Time").text = str(cum_time)

    ET.indent(root, space="  ")
    return ET.ElementTree(root)


def main():
    p = argparse.ArgumentParser(description="Generate synthetic runs for real teams from CSV")
    p.add_argument('--courses', required=True, help="Path to CourseData XML file")
    p.add_argument('--teams-csv', required=True, help="Path to team registration CSV file")
    p.add_argument('--ref-results', help="Path to existing ResultList for speed calibration")
    p.add_argument('--out', help="Output ResultList XML path")
    p.add_argument('--event-name', help="Event name for the output")
    p.add_argument('--mass-start', help="Mass start time (ISO 8601)")
    p.add_argument('--speed-mode', choices=['percentile', 'top10', 'manual'],
                   help="Speed generation mode")
    p.add_argument('--avg-speed', type=float, help="Average speed in km/h (manual mode)")
    p.add_argument('--speed-variance', type=float, help="Speed variance +/- %% (manual mode)")
    p.add_argument('--dnf-rate', type=float, help="DNF probability per runner")
    p.add_argument('--mp-rate', type=float, help="Missing punch probability per runner")
    p.add_argument('--dsq-rate', type=float, help="DSQ probability per runner")
    p.add_argument('--dns-rate', type=float, help="DNS probability per runner")
    p.add_argument('--seed', type=int, help="Random seed for reproducibility")
    p.add_argument('--non-interactive', action='store_true', help="Skip prompts, use defaults")
    args = p.parse_args()

    # Parse courses
    print(f"  Loading courses from {args.courses}...")
    courses_by_leg = parse_coursedata(args.courses)
    if not courses_by_leg:
        print("  ERROR: No courses found in CourseData file.")
        sys.exit(1)

    n_legs_available = max(courses_by_leg.keys())
    course_names = [sorted(courses_by_leg[leg].keys()) for leg in sorted(courses_by_leg.keys())]
    flat_names = [n for names in course_names for n in names]
    class_name_el = ET.parse(args.courses).getroot()
    cf_els = class_name_el.findall('.//{http://www.orienteering.org/datastandard/3.0}CourseFamily')
    course_class_name = cf_els[0].text if cf_els else "Relay"

    print(f"  Loaded {len(flat_names)} courses ({n_legs_available} legs available) — {course_class_name}")
    for leg in sorted(courses_by_leg.keys()):
        names = sorted(courses_by_leg[leg].keys())
        print(f"    Leg {leg}: {', '.join(names)}")

    # Parse teams from CSV
    print(f"\n  Loading teams from {args.teams_csv}...")
    csv_teams, csv_n_legs = parse_teams_csv(args.teams_csv)
    print(f"  Found {len(csv_teams)} teams with {csv_n_legs} legs")

    # Validate leg count
    if csv_n_legs > n_legs_available:
        print(f"  ERROR: CSV has {csv_n_legs} legs but CourseData only has {n_legs_available}.")
        sys.exit(1)

    # Use only legs present in both CSV and CourseData
    n_legs = csv_n_legs
    courses_by_leg = {leg: courses_by_leg[leg] for leg in range(1, n_legs + 1)}

    # Detect class from CSV if available
    csv_classes = set(t['class'] for t in csv_teams if t['class'])
    class_name = csv_classes.pop() if len(csv_classes) == 1 else course_class_name

    is_individual = (n_legs == 1)
    if is_individual:
        print(f"  Mode: individual race (1 runner per team)")

    # Analyze reference results
    ref_stats = None
    if args.ref_results:
        print(f"\n  Analyzing speed distribution from {args.ref_results}...")
        ref_stats = analyze_ref_results(args.ref_results)
        print(f"  Found speed data for {sum(len(v['speeds']) for v in ref_stats.values())} OK runners")

    # Interactive setup
    if args.non_interactive:
        if not args.event_name:
            if is_individual:
                args.event_name = "Synthetic Individual Race"
            elif n_legs == 7:
                args.event_name = "Synthetic Jukolan Viesti"
            else:
                args.event_name = "Synthetic Relay Race"
        if not args.mass_start:
            args.mass_start = "2025-06-14T23:00:00+03:00"
        if not args.speed_mode:
            args.speed_mode = "percentile" if ref_stats else "manual"
        if args.dnf_rate is None:
            args.dnf_rate = 0.02
        if args.mp_rate is None:
            args.mp_rate = 0.01
        if args.dsq_rate is None:
            args.dsq_rate = 0.005
        if args.dns_rate is None:
            args.dns_rate = 0.01
        if args.avg_speed is None and args.speed_mode == 'manual':
            args.avg_speed = 6.5
        if args.speed_variance is None and args.speed_mode == 'manual':
            args.speed_variance = 15
    else:
        interactive_setup(courses_by_leg, ref_stats, args, n_legs)

    if not args.out:
        args.out = prompt("Output file path", default="data/artificial_resultlist.xml")

    # Setup RNG
    rng = random.Random(args.seed)
    if args.seed is not None:
        random.seed(args.seed)

    # Build speed sampler
    speed_sampler = build_speed_sampler(ref_stats, sorted(courses_by_leg.keys()),
                                        args.speed_mode, args.avg_speed, args.speed_variance)

    # Parse mass start
    try:
        mass_start = datetime.fromisoformat(args.mass_start)
    except (ValueError, TypeError):
        print(f"  ERROR: Invalid mass start time: {args.mass_start}")
        sys.exit(1)
    if mass_start.tzinfo is None:
        mass_start = mass_start.replace(tzinfo=timezone(timedelta(hours=3)))

    n_teams = len(csv_teams)
    print(f"\n  Generating runs for {n_teams} teams x {n_legs} legs = {n_teams * n_legs} runners...")
    print(f"  Speed mode: {args.speed_mode}")
    print(f"  Status rates: DNS={args.dns_rate:.1%}, DNF={args.dnf_rate:.1%}, "
          f"MP={args.mp_rate:.1%}, DSQ={args.dsq_rate:.1%}")
    if is_individual:
        print(f"  Note: individual race — all runners start at mass start time")

    # Per-leg course assignment
    leg_course_prefs = {}
    for leg in sorted(courses_by_leg.keys()):
        best = max(courses_by_leg[leg].items(),
                   key=lambda item: len(item[1]['controls']))
        leg_course_prefs[leg] = best

    # Generate runs
    status_counts = defaultdict(int)
    all_teams_data = []
    n_tbd_generated = 0

    for csv_team in csv_teams:
        bib = csv_team['bib']
        team_name = csv_team['team_name']
        team = {'bib': bib, 'team_name': team_name, 'legs': []}
        cumulative_time = 0

        for leg in sorted(courses_by_leg.keys()):
            leg_idx = leg - 1  # 0-based

            if is_individual:
                leg_start = mass_start
            else:
                leg_start = mass_start + timedelta(seconds=cumulative_time)

            # Pick course
            course_name, course_data = leg_course_prefs[leg]

            # Get runner name
            runner = csv_team['runners'][leg_idx] if leg_idx < len(csv_team['runners']) else None
            if runner is None:
                given, family = generate_runner_name(rng)
                n_tbd_generated += 1
            else:
                given = runner['given']
                family = runner['family']

            # Determine status
            roll = rng.random()
            if roll < args.dns_rate:
                status = 'DidNotStart'
            elif roll < args.dns_rate + args.dnf_rate:
                status = 'DidNotFinish'
            elif roll < args.dns_rate + args.dnf_rate + args.mp_rate:
                status = 'OK'  # MP: will remove a split later
            elif roll < args.dns_rate + args.dnf_rate + args.mp_rate + args.dsq_rate:
                status = 'Disqualified'
            else:
                status = 'OK'

            status_counts[status] += 1

            person_id = f"{bib}:{leg}"

            leg_data = {
                'leg': leg,
                'given': given,
                'family': family,
                'person_id': person_id,
                'start_time': leg_start,
                'course_name': course_name,
                'course_length': course_data['length'],
                'status': status,
                'finish_time': None,
                'leg_time': None,
                'time_behind': None,
                'position': None,
                'overall_time': None,
                'overall_behind': None,
                'overall_pos': None,
                'splits': [],
            }

            if status == 'DidNotStart':
                team['legs'].append(leg_data)
                continue

            # Generate speed and compute leg time
            kmh = speed_sampler(person_id, leg, rng)
            km = course_data['length'] / 1000.0
            leg_time = int(round((km / kmh) * 3600)) if kmh > 0 else 3600

            is_mp = (status == 'OK' and roll < args.dns_rate + args.dnf_rate + args.mp_rate)
            is_dnf = (status == 'DidNotFinish')

            splits = generate_splits(course_data['controls'], course_data['leg_lengths'], leg_time)

            if is_dnf:
                if splits and len(splits) > 2:
                    cut_idx = rng.randint(1, len(splits) - 2)
                    splits = splits[:cut_idx]
                    leg_time = splits[-1][1] if splits else 0
                status = 'DidNotFinish'

            if is_mp and splits:
                if len(splits) > 2:
                    rm_idx = rng.randint(1, len(splits) - 2)
                    splits.pop(rm_idx)

            finish_time = leg_start + timedelta(seconds=leg_time)

            leg_data['finish_time'] = finish_time
            leg_data['leg_time'] = leg_time
            leg_data['splits'] = splits
            if is_dnf:
                leg_data['status'] = 'DidNotFinish'
            elif is_mp:
                leg_data['status'] = 'OK'

            cumulative_time += leg_time
            team['legs'].append(leg_data)

        all_teams_data.append(team)

    # Compute per-leg positions and time-behind
    for leg in sorted(courses_by_leg.keys()):
        leg_ok = [t for t in all_teams_data if len(t['legs']) >= leg and t['legs'][leg - 1]['status'] == 'OK']
        leg_ok.sort(key=lambda t: t['legs'][leg - 1]['leg_time'])
        if not leg_ok:
            continue
        leader_time = leg_ok[0]['legs'][leg - 1]['leg_time']
        for i, t in enumerate(leg_ok):
            ld = t['legs'][leg - 1]
            ld['position'] = i + 1
            ld['time_behind'] = ld['leg_time'] - leader_time

    # Compute overall results
    for team in all_teams_data:
        cum = 0
        all_ok = True
        for ld in team['legs']:
            if ld['status'] != 'OK' or ld['leg_time'] is None:
                all_ok = False
                ld['overall_time'] = None
                ld['overall_pos'] = None
                ld['overall_behind'] = None
                continue
            cum += ld['leg_time']
            ld['overall_time'] = cum

        if not all_ok:
            for ld in team['legs']:
                ld['overall_pos'] = None
                ld['overall_behind'] = None

    all_ok_teams = [t for t in all_teams_data if all(ld['status'] == 'OK' for ld in t['legs'])]
    all_ok_teams.sort(key=lambda t: t['legs'][-1]['overall_time'])
    if all_ok_teams:
        leader_total = all_ok_teams[0]['legs'][-1]['overall_time']
        for i, t in enumerate(all_ok_teams):
            for ld in t['legs']:
                ld['overall_pos'] = i + 1
                if ld['overall_time'] is not None:
                    ld['overall_behind'] = ld['overall_time'] - leader_total

    # Build XML
    print(f"\n  Writing {args.out}...")
    tree = build_resultlist_xml(args.event_name, mass_start, all_teams_data, class_name)
    tree.write(args.out, encoding='UTF-8', xml_declaration=True)

    # Summary
    total = sum(status_counts.values())
    print(f"\n  Done. {n_teams} teams x {n_legs} legs = {total} runners.")
    if n_tbd_generated:
        print(f"  Generated synthetic names for {n_tbd_generated} TBD/TBA runner slots.")
    parts = []
    for s in ['OK', 'DidNotFinish', 'Disqualified', 'DidNotStart']:
        c = status_counts.get(s, 0)
        if c > 0:
            parts.append(f"{s}={c}")
    print(f"  Status: {', '.join(parts)}")
    if args.mp_rate and args.mp_rate > 0:
        mp_actual = sum(1 for t in all_teams_data for ld in t['legs']
                        if ld['status'] == 'OK' and len(ld['splits']) < len(leg_course_prefs[ld['leg']][1]['controls']) - 1)
        if mp_actual:
            print(f"  (approx {mp_actual} runners have missing punches)")

    print(f"\n  Validate:  python iofvalidator.py {args.out}")
    print(f"  Simulate:  python simulator.py --iof {args.out} --limit-teams 5")


if __name__ == '__main__':
    main()
