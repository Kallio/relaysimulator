import math
import re
import xml.etree.ElementTree as ET
from datetime import datetime

def infer_leg_from_course_name(name):
    """
    Päätellään osuuden numero kurssin nimen perusteella.
    Esim. J401 -> leg 4, J302 -> leg 3, V110 -> leg 1
    """
    if not name or len(name) < 4:
        return None
    if name[0] in ('J', 'V') and name[1:4].isdigit():
        return int(name[1])
    return None


def haversine_m(lat1, lon1, lat2, lon2):
    R = 6371000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return round(2 * R * math.asin(math.sqrt(a)))


def parse_georef(georef_str):
    """
    Georef format: id_latNW_lonNW_latNE_lonNE_latSE_lonSE_latSW_lonSW
    Returns list of 4 (lat, lon) corners: [NW, NE, SE, SW]
    NW = top-left, NE = top-right, SE = bottom-right, SW = bottom-left.
    """
    parts = georef_str.split("_")
    floats = [float(p) for p in parts[1:]]
    return [(floats[i], floats[i + 1]) for i in range(0, 8, 2)]


def pixel_to_latlon(px, py_neg, corners, img_w=2324, img_h=3220):
    """
    Bilinear interpolation: radat pixel coords -> WGS84.
    px: x pixels from left edge (0 = left)
    py_neg: y as stored in radat (negative means below top)
    corners: [NW, NE, SE, SW] as (lat, lon)
    img_w, img_h: image dimensions in pixels
    """
    u = max(0.0, min(1.0, px / (img_w - 1)))
    v = max(0.0, min(1.0, (-py_neg) / (img_h - 1)))
    (lat_NW, lon_NW), (lat_NE, lon_NE), (lat_SE, lon_SE), (lat_SW, lon_SW) = corners
    lat = (1-u)*(1-v)*lat_NW + u*(1-v)*lat_NE + u*v*lat_SE + (1-u)*v*lat_SW
    lon = (1-u)*(1-v)*lon_NW + u*(1-v)*lon_NE + u*v*lon_SE + (1-u)*v*lon_SW
    return lat, lon


def parse_radat(radat_path):
    """
    Parse radat_73.txt.
    Returns:
      control_positions: dict course_name -> [(px, py_neg), ...] type-1 entries in order
      start_positions:   dict course_name -> (px, py_neg) type-2 start position
    """
    control_positions = {}
    start_positions = {}

    with open(radat_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            parts = line.split("|")
            if len(parts) < 4:
                continue

            m = re.match(r"ju\((\w+)\)", parts[2])
            if not m:
                continue
            course_name = m.group(1)

            type1 = []
            start = None

            for seg in parts[3].split("N"):
                tokens = seg.split(";")
                if not tokens or not tokens[0]:
                    continue
                t = tokens[0]
                if t == "2" and len(tokens) >= 3 and start is None:
                    start = (float(tokens[1]), float(tokens[2]))
                elif t == "1" and len(tokens) >= 3:
                    type1.append((float(tokens[1]), float(tokens[2])))

            control_positions[course_name] = type1
            if start is not None:
                start_positions[course_name] = start

    return control_positions, start_positions


def resultlist_to_coursedata(iof_in, courses_out, radat_file=None, georef=None, img_w=2324, img_h=3220):
    tree = ET.parse(iof_in)
    root = tree.getroot()
    ns_uri = root.tag.split('}')[0].strip('{')
    ns = {"iof": ns_uri}

    classname_el = root.find(".//iof:ClassResult/iof:Class/iof:Name", ns)
    classname = classname_el.text.strip() if classname_el is not None and classname_el.text else "UnknownClass"

    event_name_el = root.find(".//iof:Event/iof:Name", ns)
    event_name = event_name_el.text.strip() if event_name_el is not None and event_name_el.text else "Event"

    # --- Kerää kaikki radat, rastit ja pituudet ---
    # Collect ALL occurrences per course and pick the most common split sequence.
    # Requires at least MIN_CONSENSUS identical sequences to accept a course.
    MIN_CONSENSUS = 5

    lengths = {}    # course_name -> length_m
    course_candidates = {}  # course_name -> {tuple(splits): count}

    for tm in root.findall(".//iof:TeamMemberResult", ns):
        result = tm.find("iof:Result", ns)
        if result is None:
            continue
        course_name_el = result.find("iof:Course/iof:Name", ns)
        if course_name_el is None:
            continue
        course_name = course_name_el.text.strip()

        if course_name not in lengths:
            length_el = result.find("iof:Course/iof:Length", ns)
            if length_el is not None and length_el.text:
                try:
                    lengths[course_name] = int(length_el.text.strip())
                except ValueError:
                    pass

        splits = []
        for st in result.findall("iof:SplitTime", ns):
            code_el = st.find("iof:ControlCode", ns)
            if code_el is not None and code_el.text:
                splits.append(code_el.text.strip())

        key = tuple(splits)
        course_candidates.setdefault(course_name, {})
        course_candidates[course_name][key] = course_candidates[course_name].get(key, 0) + 1

    courses = {}  # course_name -> [control_code, ...]
    for course_name, counts in course_candidates.items():
        best_seq, best_count = max(counts.items(), key=lambda x: (len(x[0]), x[1]))
        if best_count < MIN_CONSENSUS:
            print(f"  Ohitetaan {course_name}: paras sekvenssi vain {best_count}x (min {MIN_CONSENSUS})")
            continue
        courses[course_name] = list(best_seq)

    # --- Radtatiedot + georeferensointi ---
    radat_ctrl = {}    # course_name -> [(px, py_neg), ...]
    radat_start = {}   # course_name -> (px, py_neg)
    corners = None

    if radat_file and georef:
        corners = parse_georef(georef)
        radat_ctrl, radat_start = parse_radat(radat_file)
        matched = set(courses) & set(radat_ctrl)
        unmatched = set(courses) - set(radat_ctrl)
        print(f"Kurssit: XML={len(courses)}, radat={len(radat_ctrl)}, matchattu={len(matched)}")
        if unmatched:
            print(f"  Ei radtatietoa: {sorted(unmatched)}")

    # --- Laske uniikit rastipositiot (lat/lon + pikselit) ---
    # code -> [(lat, lon)] and code -> [(px, py_neg)] from all courses
    geo_pts = {}
    map_pts = {}

    if corners:
        for cname, codes in courses.items():
            positions = radat_ctrl.get(cname, [])
            for i, code in enumerate(codes):
                if i < len(positions):
                    px, py_neg = positions[i]
                    lat, lon = pixel_to_latlon(px, py_neg, corners, img_w, img_h)
                    geo_pts.setdefault(code, []).append((lat, lon))
                    map_pts.setdefault(code, []).append((px, py_neg))

        # S1 start position averaged over all course starts
        for cname, (px, py_neg) in radat_start.items():
            lat, lon = pixel_to_latlon(px, py_neg, corners, img_w, img_h)
            geo_pts.setdefault("S1", []).append((lat, lon))
            map_pts.setdefault("S1", []).append((px, py_neg))

    def avg_geo(code):
        pts = geo_pts.get(code)
        if not pts:
            return None
        return (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))

    def avg_map(code):
        pts = map_pts.get(code)
        if not pts:
            return None
        return (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))

    # --- Rakenna XML ---
    course_data = ET.Element("CourseData", {
        "xmlns": ns_uri,
        "xmlns:xsi": "http://www.w3.org/2001/XMLSchema-instance",
        "iofVersion": "3.0",
        "createTime": datetime.now().isoformat(),
        "creator": "ResultList->CourseData Script"
    })

    event_el = ET.SubElement(course_data, "Event")
    ET.SubElement(event_el, "Name").text = event_name

    race_data = ET.SubElement(course_data, "RaceCourseData")

    # Map element (only if georef given)
    if corners:
        map_el = ET.SubElement(race_data, "Map")
        ET.SubElement(map_el, "Scale").text = "10000"
        tl = ET.SubElement(map_el, "MapPositionTopLeft")
        tl.set("x", "0"); tl.set("y", "0"); tl.set("unit", "px")
        br = ET.SubElement(map_el, "MapPositionBottomRight")
        br.set("x", str(img_w - 1)); br.set("y", str(img_h - 1)); br.set("unit", "px")

    # Control elements: S1 + all split-time codes
    all_codes = {"S1"}
    for codes in courses.values():
        all_codes.update(codes)

    for code in sorted(all_codes, key=lambda c: (len(c), c)):
        geo = avg_geo(code)
        if corners and geo is None:
            continue  # no position data, skip when radat is available

        ctrl_el = ET.SubElement(race_data, "Control")
        ET.SubElement(ctrl_el, "Id").text = code

        if geo:
            pos_el = ET.SubElement(ctrl_el, "Position")
            pos_el.set("lat", f"{geo[0]:.7f}")
            pos_el.set("lng", f"{geo[1]:.7f}")

        mp = avg_map(code)
        if mp:
            map_pos_el = ET.SubElement(ctrl_el, "MapPosition")
            map_pos_el.set("x", f"{mp[0]:.1f}")
            map_pos_el.set("y", f"{-mp[1]:.1f}")
            map_pos_el.set("unit", "px")

    # --- Luo Course-elementit ---
    for name, controls in sorted(courses.items()):
        if len(controls) < 2:  # tarvitaan vähintään Start + yksi rastiväli + Finish
            print(f"  Ohitetaan {name}: liian vähän rasteja ({len(controls)})")
            continue
        length = lengths.get(name, 0)
        positions = radat_ctrl.get(name, [])

        # Build list of (code, geo) for this course: start + controls
        start_geo = avg_geo("S1")
        ctrl_geos = []
        for i, code in enumerate(controls):
            if i < len(positions) and corners:
                px, py_neg = positions[i]
                ctrl_geos.append(pixel_to_latlon(px, py_neg, corners, img_w, img_h))
            else:
                ctrl_geos.append(avg_geo(code))

        c = ET.SubElement(race_data, "Course")
        ET.SubElement(c, "Name").text = name
        ET.SubElement(c, "CourseFamily").text = classname
        ET.SubElement(c, "Length").text = str(length)
        ET.SubElement(c, "Climb").text = "0"

        # Start
        cc_start = ET.SubElement(c, "CourseControl", {"type": "Start"})
        ET.SubElement(cc_start, "Control").text = "S1"
        # LegLength from start to first control
        if start_geo and ctrl_geos and ctrl_geos[0]:
            d = haversine_m(start_geo[0], start_geo[1], ctrl_geos[0][0], ctrl_geos[0][1])
            ET.SubElement(cc_start, "LegLength").text = str(d)

        # Controls (all but last = finish)
        for i, code in enumerate(controls[:-1] if controls else []):
            cc = ET.SubElement(c, "CourseControl", {"type": "Control"})
            ET.SubElement(cc, "Control").text = code

            # MapTextPosition before LegLength (XSD order)
            if i < len(positions):
                px, py_neg = positions[i]
                mtp = ET.SubElement(cc, "MapTextPosition")
                mtp.set("x", f"{px:.1f}")
                mtp.set("y", f"{-py_neg:.1f}")
                mtp.set("unit", "px")

            # LegLength to next control
            g_cur = ctrl_geos[i] if i < len(ctrl_geos) else None
            g_next = ctrl_geos[i + 1] if (i + 1) < len(ctrl_geos) else None
            if g_cur and g_next:
                d = haversine_m(g_cur[0], g_cur[1], g_next[0], g_next[1])
                ET.SubElement(cc, "LegLength").text = str(d)

        # Finish: use the actual last split-time code (e.g. 300 = finish punch)
        if controls:
            cc_finish = ET.SubElement(c, "CourseControl", {"type": "Finish"})
            ET.SubElement(cc_finish, "Control").text = controls[-1]
            finish_idx = len(controls) - 1
            if finish_idx < len(positions):
                px, py_neg = positions[finish_idx]
                mtp = ET.SubElement(cc_finish, "MapTextPosition")
                mtp.set("x", f"{px:.1f}")
                mtp.set("y", f"{-py_neg:.1f}")
                mtp.set("unit", "px")

    # --- Luo ClassCourseAssignment per rata (forking: yksi per rata/osuus) ---
    for course_name in sorted(courses.keys()):
        leg = infer_leg_from_course_name(course_name)
        if leg is not None:
            cca = ET.SubElement(race_data, "ClassCourseAssignment", {"numberOfCompetitors": "0"})
            ET.SubElement(cca, "ClassName").text = classname
            ET.SubElement(cca, "AllowedOnLeg").text = str(leg)
            ET.SubElement(cca, "CourseName").text = course_name
            ET.SubElement(cca, "CourseFamily").text = classname

    # --- Kirjoita tiedostoon ---
    ET.indent(course_data, space="  ")
    tree_out = ET.ElementTree(course_data)
    tree_out.write(courses_out, encoding="UTF-8", xml_declaration=True)
    print(f"Kirjoitettu {courses_out}")


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--iof", required=True, help="ResultList IOF XML")
    p.add_argument("--out", required=True, help="CourseData.xml output")
    p.add_argument("--radat", help="radat_73.txt course overlay file e.g. from  curl -o radat_73.txt  https://routegadget.jukola.com/kartat/radat_73.txt")
    p.add_argument(
        "--georef",
        default="66_61.652565_27.122711_61.644249_27.204894_61.590181_27.180791_61.598512_27.098436",
        help="Georef: id_latNW_lonNW_latNE_lonNE_latSE_lonSE_latSW_lonSW"
    )
    p.add_argument("--img-w", type=int, default=2324, help="Image width in pixels (default: 2324)")
    p.add_argument("--img-h", type=int, default=3220, help="Image height in pixels (default: 3220)")
    args = p.parse_args()
    resultlist_to_coursedata(args.iof, args.out, radat_file=args.radat, georef=args.georef, img_w=args.img_w, img_h=args.img_h)
