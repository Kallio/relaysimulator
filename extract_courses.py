import xml.etree.ElementTree as ET
from datetime import datetime

def resultlist_to_coursedata(iof_in, courses_out):
    tree = ET.parse(iof_in)
    root = tree.getroot()
    ns_uri = root.tag.split('}')[0].strip('{')
    ns = {"iof": ns_uri}

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

    # --- kerää kaikki radat ja niiden rastit ---
    courses = {}
    for tm in root.findall(".//iof:TeamMemberResult", ns):
        result = tm.find("iof:Result", ns)
        if result is None:
            continue
        course_name_el = result.find("iof:Course/iof:Name", ns)
        if course_name_el is None:
            continue
        course_name = course_name_el.text.strip()

        # kerätään rastit (SplitTime)
        splits = []
        for st in result.findall("iof:SplitTime", ns):
            code_el = st.find("iof:ControlCode", ns)
            if code_el is not None and code_el.text:
                splits.append(code_el.text.strip())

        # tallenna vain jos ei ole vielä lisätty
        if course_name not in courses:
            courses[course_name] = splits

    # --- luo Course-elementit ---
    for name, controls in sorted(courses.items()):
        c = ET.SubElement(race_data, "Course")
        ET.SubElement(c, "Name").text = name
        ET.SubElement(c, "CourseFamily").text = name
        ET.SubElement(c, "Length").text = "0"  # ResultList ei sisällä pituutta luotettavasti
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

    # --- tee TeamCourseAssignment ---
    for team in root.findall(".//iof:TeamResult", ns):
        bib = team.find("iof:BibNumber", ns)
        if bib is None or not bib.text:
            continue
        tca = ET.SubElement(race_data, "TeamCourseAssignment")
        ET.SubElement(tca, "BibNumber").text = bib.text.strip()

        for tm in team.findall("iof:TeamMemberResult", ns):
            result = tm.find("iof:Result", ns)
            if result is None:
                continue
            leg = result.find("iof:Leg", ns)
            course = result.find("iof:Course/iof:Name", ns)
            if leg is not None and course is not None:
                ass = ET.SubElement(tca, "TeamMemberCourseAssignment")
                ET.SubElement(ass, "Leg").text = leg.text.strip()
                ET.SubElement(ass, "CourseName").text = course.text.strip()
                ET.SubElement(ass, "CourseFamily").text = course.text.strip()

    # --- kirjoita tiedostoon ---
    tree_out = ET.ElementTree(course_data)
    tree_out.write(courses_out, encoding="UTF-8", xml_declaration=True)
    print(f"Kirjoitettu {courses_out}")

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--iof", required=True, help="ResultList IOF XML")
    p.add_argument("--out", required=True, help="Navisport CourseData.xml")
    args = p.parse_args()
    resultlist_to_coursedata(args.iof, args.out)
