#!/usr/bin/env python3

import sys
import os
import urllib.request
from lxml import etree

# IOF v3 schema (virallinen repo)
XSD_URL = "https://raw.githubusercontent.com/international-orienteering-federation/datastandard-v3/master/IOF.xsd"
LOCAL_XSD = "IOF.xsd"


ILLEGAL_CHARS = {
    0xFFFD: "U+FFFD (replacement character, data corruption)",
}

# XML 1.0 allows only \t (9), \n (10), \r (13) among control chars
XML_CONTROL = set(range(0x00, 0x20)) - {0x09, 0x0A, 0x0D}


def download_xsd_if_missing():
    if os.path.exists(LOCAL_XSD):
        print(f"ℹ️ Käytetään olemassa olevaa {LOCAL_XSD}")
        return

    print("⬇️ Ladataan IOF.xsd GitHubista...")
    try:
        urllib.request.urlretrieve(XSD_URL, LOCAL_XSD)
        print("✅ XSD ladattu")
    except Exception as e:
        print("❌ XSD lataus epäonnistui:")
        print(e)
        sys.exit(1)


def _scan_text(text, label, elem_path, results):
    for i, ch in enumerate(text):
        cp = ord(ch)
        desc = ILLEGAL_CHARS.get(cp)
        if desc is None and cp in XML_CONTROL:
            desc = f"U+{cp:04X} (XML-illegal control character)"
        if desc is not None:
            ctx_start = max(0, i - 20)
            ctx_end = min(len(text), i + 20)
            context = text[ctx_start:ctx_end]
            results.append(f"  {label}: {desc}\n    XPath: {elem_path}\n    Context: {context!r}\n")


def find_illegal_chars(xml_path):
    """
    Scan XML file for corrupted or XML-illegal characters
    (U+FFFD replacement char, control chars, etc.)
    Returns list of human-readable findings.
    """
    try:
        tree = etree.parse(xml_path)
    except etree.XMLSyntaxError as e:
        return [f"❌ XML not well-formed, cannot scan: {e}"]

    results = []

    for elem in tree.iter():
        tag = elem.tag if isinstance(elem.tag, str) else repr(elem.tag)
        path = tree.getelementpath(elem) or tag

        if elem.text and elem.text.strip():
            _scan_text(elem.text, "text", path, results)

        if elem.tail and elem.tail.strip():
            _scan_text(elem.tail, "tail", path, results)

        for attr_name, attr_val in elem.attrib.items():
            if attr_val:
                _scan_text(attr_val, f"@{attr_name}", f"{path}/@{attr_name}", results)

    return results


def validate_xml(xml_path, xsd_path):
    try:
        # Lataa XSD
        with open(xsd_path, "rb") as f:
            schema_root = etree.XML(f.read())
        schema = etree.XMLSchema(schema_root)

        # Lataa XML
        with open(xml_path, "rb") as f:
            xml_doc = etree.XML(f.read())

        # Validoi
        if schema.validate(xml_doc):
            print("✅ XML on validi IOF Data Standard v3 mukaan.")
            return True
        else:
            print("❌ XML EI ole validi.\n")
            for error in schema.error_log:
                print(f"Linja {error.line}, sarake {error.column}: {error.message}")
            return False

    except etree.XMLSyntaxError as e:
        print("❌ XML ei ole well-formed:")
        print(e)
    except etree.XMLSchemaParseError as e:
        print("❌ XSD virhe:")
        print(e)
    except Exception as e:
        print("❌ Tuntematon virhe:")
        print(e)

    return False


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Käyttö:")
        print("  python validate_iof_xml.py courses.xml")
        sys.exit(1)

    xml_file = sys.argv[1]

    # 1. Scan for illegal / corrupted characters
    illegal = find_illegal_chars(xml_file)
    if illegal:
        print(f"⚠️  {len(illegal)} ongelmallista merkkiä löytyi:\n")
        for line in illegal:
            print(line)
    else:
        print("✅ Ei ongelmallisia merkkejä.")

    # 2. XSD validation
    download_xsd_if_missing()
    valid = validate_xml(xml_file, LOCAL_XSD)

    sys.exit(0 if valid and not illegal else 1)
