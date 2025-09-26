import xml.etree.ElementTree as ET
from datetime import datetime

def infer_leg_from_course_name(name):
    """
    Päätellään osuuden numero kurssin nimen perusteella.
    Esim. J401 → leg 4, J302 → leg 3, J101 → leg 1
    """
    if not name or len(name) < 4:
        return None
    if name[0] in ('J', 'V') and name[1:4].isdigit():
        return int(name[1])
    return None

def resultlist_to_coursedata(iof_in, courses_out):
    tree = ET.parse(iof_in)
    root = tree.getroot()
    ns_uri = root.tag.split('}')[0].strip('{')
    ns = {"iof": ns_uri}

    # --- Hae classname ensimmäisestä ClassResult/Class/Name ---
    classname_el = root.find(".//iof:ClassResult/iof:Class/iof:Name", ns)
    if classname_el is None or not classname_el.text:
        classname = "UnknownClass"
    else:
        classname = classname_el.text.strip()

    course_data = ET.Element("CourseData", {
        "xmlns": ns_uri,
        "xmlns:xsi": "http://www.w3.org/2001/XMLSchema-instance",
        "iofVersion": "3.0",
        "createTime": datetime.now().isoformat(),
        "creator": "ResultList->CourseData Script"
    })

    event_el = ET.SubElement(course_data, "Event")
    ET.SubElement(event_el, "Name").text = "Event"

    race_data = ET.SubElement(course_data, "RaceCourseData")

    # --- Kerää kaikki radat ja rastit ---
    courses = {}
    for tm in root.findall(".//iof:TeamMemberResult", ns):
        result = tm.find("iof:Result", ns)
        if result is None:
            continue
        course_name_el = result.find("iof:Course/iof:Name", ns)
        if course_name_el is None:
            continue
        course_name = course_name_el.text.strip()

        splits = []
        for st in result.findall("iof:SplitTime", ns):
            code_el = st.find("iof:ControlCode", ns)
            if code_el is not None and code_el.text:
                splits.append(code_el.text.strip())

        # Lisää radat dictionaryyn
        if course_name not in courses:
            courses[course_name] = splits

    # --- Luo Course-elementit ---
    for name, controls in sorted(courses.items()):
        c = ET.SubElement(race_data, "Course")
        ET.SubElement(c, "Name").text = name
        ET.SubElement(c, "CourseFamily").text = classname
        ET.SubElement(c, "Length").text = "0"
        ET.SubElement(c, "Climb").text = "0"

        # Start
        cc_start = ET.SubElement(c, "CourseControl", {"type": "Start"})
        ET.SubElement(cc_start, "Control").text = "S1"

        # Rastien järjestys
        for code in controls:
            cc = ET.SubElement(c, "CourseControl", {"type": "Control"})
            ET.SubElement(cc, "Control").text = code

        # Finish
        cc_finish = ET.SubElement(c, "CourseControl", {"type": "Finish"})
        ET.SubElement(cc_finish, "Control").text = "F1"

    # --- Luo ClassCourseAssignment per rata ---
    for course_name in courses.keys():
        leg = infer_leg_from_course_name(course_name)
        if leg is not None:
            cca = ET.SubElement(race_data, "ClassCourseAssignment", {"numberOfCompetitors": "0"})
            ET.SubElement(cca, "ClassName").text = classname
            ET.SubElement(cca, "CourseFamily").text = classname
            ET.SubElement(cca, "AllowedOnLeg").text = str(leg)
            ET.SubElement(cca, "CourseName").text = course_name

    # --- Kirjoita tiedostoon ---
    tree_out = ET.ElementTree(course_data)
    tree_out.write(courses_out, encoding="UTF-8", xml_declaration=True)
    print(f"Kirjoitettu {courses_out}")

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--iof", required=True, help="ResultList IOF XML")
    p.add_argument("--out", required=True, help="CourseData.xml output")
    args = p.parse_args()
    resultlist_to_coursedata(args.iof, args.out)
