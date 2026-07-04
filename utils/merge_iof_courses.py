#!/usr/bin/env python3
"""Merge two IOF course files into a single file with aligned map coordinates.

When two course files reference different crops of the same map (e.g. Jukola
uses the full map, Venloja uses a cropped portion), importing both separately
messes up control positions. This merges them into one file with a shared map.

Usage:
  python utils/merge_iof_courses.py base.xml overlay.xml -o combined.xml
  python utils/merge_iof_courses.py base.xml overlay.xml -o combined.xml --offset 62 2249
"""

import argparse
import xml.etree.ElementTree as ET
from pathlib import Path
from copy import deepcopy
import statistics
import sys

IOF_NS = "http://www.orienteering.org/datastandard/3.0"
ET.register_namespace("", IOF_NS)
ET.register_namespace("xsi", "http://www.w3.org/2001/XMLSchema-instance")


def ns(tag):
    return f"{{{IOF_NS}}}{tag}"


def get_controls(root):
    controls = {}
    for ctrl in root.findall(f".//{ns('RaceCourseData')}/{ns('Control')}"):
        cid_elem = ctrl.find(ns("Id"))
        pos = ctrl.find(ns("MapPosition"))
        geo = ctrl.find(ns("Position"))
        if cid_elem is not None and pos is not None:
            controls[cid_elem.text] = {
                "x": float(pos.get("x")),
                "y": float(pos.get("y")),
                "lat": float(geo.get("lat")) if geo is not None else None,
                "lng": float(geo.get("lng")) if geo is not None else None,
            }
    return controls


def shift_positions(race_elem, dx, dy):
    for ctrl in race_elem.findall(ns("Control")):
        pos = ctrl.find(ns("MapPosition"))
        if pos is not None:
            pos.set("x", f"{float(pos.get('x')) + dx:.1f}")
            pos.set("y", f"{float(pos.get('y')) + dy:.1f}")
    for course in race_elem.findall(ns("Course")):
        for cc in course.findall(ns("CourseControl")):
            tp = cc.find(ns("MapTextPosition"))
            if tp is not None:
                tp.set("x", f"{float(tp.get('x')) + dx:.1f}")
                tp.set("y", f"{float(tp.get('y')) + dy:.1f}")


def compute_offset(base_controls, overlay_controls):
    shared = sorted(set(base_controls.keys()) & set(overlay_controls.keys()))
    if len(shared) < 2:
        print(f"Warning: only {len(shared)} shared control(s), offset may be unreliable", file=sys.stderr)

    offsets = []
    for cid in shared:
        bc = base_controls[cid]
        oc = overlay_controls[cid]
        offsets.append((oc["x"], oc["y"], bc["x"] - oc["x"], bc["y"] - oc["y"], cid))

    print("Shared controls and computed offsets:")
    for ox, oy, dx, dy, cid in offsets:
        lat_diff = ""
        if base_controls[cid]["lat"] and overlay_controls[cid]["lat"]:
            ld = abs(base_controls[cid]["lat"] - overlay_controls[cid]["lat"])
            lngd = abs(base_controls[cid]["lng"] - overlay_controls[cid]["lng"])
            lat_diff = f"  (lat diff={ld:.6f}, lng diff={lngd:.6f})"
        print(f"  {cid}: overlay({ox:.1f}, {oy:.1f}) -> base({ox+dx:.1f}, {oy+dy:.1f})  offset=({dx:.1f}, {dy:.1f}){lat_diff}")

    # Use median for robustness
    dxs = sorted(o[2] for o in offsets)
    dys = sorted(o[3] for o in offsets)
    n = len(dxs)
    offset_x = dxs[n // 2] if n % 2 else (dxs[n // 2 - 1] + dxs[n // 2]) / 2
    offset_y = dys[n // 2] if n % 2 else (dys[n // 2 - 1] + dys[n // 2]) / 2

    return offset_x, offset_y, shared


def main():
    ap = argparse.ArgumentParser(description="Merge two IOF course files with aligned map coordinates")
    ap.add_argument("base", type=Path, help="Base course file (reference map dimensions)")
    ap.add_argument("overlay", type=Path, help="Overlay course file (will be shifted to match base)")
    ap.add_argument("-o", "--output", type=Path, required=True, help="Output merged file")
    ap.add_argument("--offset", type=float, nargs=2, metavar=("DX", "DY"),
                    help="Manual pixel offset (skip auto-computation)")
    args = ap.parse_args()

    for p in [args.base, args.overlay]:
        if not p.exists():
            print(f"Error: {p} not found", file=sys.stderr)
            sys.exit(1)

    base_tree = ET.parse(args.base)
    overlay_tree = ET.parse(args.overlay)
    base_root = base_tree.getroot()
    overlay_root = overlay_tree.getroot()

    base_race = base_root.find(ns("RaceCourseData"))
    overlay_race = overlay_root.find(ns("RaceCourseData"))
    if base_race is None or overlay_race is None:
        print("Error: files must contain a RaceCourseData element", file=sys.stderr)
        sys.exit(1)

    base_map = base_race.find(ns("Map"))
    overlay_map = overlay_race.find(ns("Map"))
    if base_map is None or overlay_map is None:
        print("Error: files must contain a Map element", file=sys.stderr)
        sys.exit(1)

    base_br = base_map.find(ns("MapPositionBottomRight"))
    base_w, base_h = float(base_br.get("x")), float(base_br.get("y"))

    if args.offset:
        offset_x, offset_y = args.offset
        print(f"Using manual offset: ({offset_x:.0f}, {offset_y:.0f})")
    else:
        base_ctrls = get_controls(base_root)
        overlay_ctrls = get_controls(overlay_root)
        offset_x, offset_y, shared = compute_offset(base_ctrls, overlay_ctrls)
        print(f"\nComputed offset: ({offset_x:.1f}, {offset_y:.1f}) from {len(shared)} shared controls")

    # Build combined file
    combined = ET.Element(ns("CourseData"), {
        "iofVersion": "3.0",
        "createTime": "2026-07-04T21:37:18.957018",
        "creator": "merge_iof_courses.py",
    })

    event = base_root.find(ns("Event"))
    if event is not None:
        combined.append(deepcopy(event))

    combined.append(deepcopy(base_race))

    overlay_copy = deepcopy(overlay_race)
    overlay_copy.find(ns("Map")).find(ns("MapPositionBottomRight")).set("x", str(int(base_w)))
    overlay_copy.find(ns("Map")).find(ns("MapPositionBottomRight")).set("y", str(int(base_h)))
    shift_positions(overlay_copy, offset_x, offset_y)
    combined.append(overlay_copy)

    tree = ET.ElementTree(combined)
    tree.write(args.output, xml_declaration=True, encoding="UTF-8")

    base_ct = len(base_race.findall(ns("Control")))
    ovl_ct = len(overlay_race.findall(ns("Control")))
    base_co = len(base_race.findall(ns("Course")))
    ovl_co = len(overlay_race.findall(ns("Course")))

    print(f"\nWrote: {args.output}")
    print(f"  Base:    {base_ct} controls, {base_co} courses")
    print(f"  Overlay: {ovl_ct} controls, {ovl_co} courses")
    print(f"  Map:     {int(base_w)} x {int(base_h)}")


if __name__ == "__main__":
    main()
