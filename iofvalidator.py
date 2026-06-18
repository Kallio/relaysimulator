#!/usr/bin/env python3

import sys
import os
import urllib.request
from lxml import etree

# IOF v3 schema (virallinen repo)
XSD_URL = "https://raw.githubusercontent.com/international-orienteering-federation/datastandard-v3/master/IOF.xsd"
LOCAL_XSD = "IOF.xsd"


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

    download_xsd_if_missing()
    validate_xml(xml_file, LOCAL_XSD)
