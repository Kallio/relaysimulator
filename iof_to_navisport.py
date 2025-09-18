import csv
import xml.etree.ElementTree as ET
import argparse

def iof_to_navisport(iof_path, out_csv, max_legs=None):
    tree = ET.parse(iof_path)
    root = tree.getroot()
    ns = {'iof': root.tag.split('}')[0].strip('{')}  # IOF namespace

    rows = []

    for class_result in root.findall('.//iof:ClassResult', ns):
        sarja = class_result.findtext('iof:Class/iof:Name', default='', namespaces=ns)

        for team in class_result.findall('iof:TeamResult', ns):
            bib = team.findtext('iof:BibNumber', default='', namespaces=ns)
            team_name = team.findtext('iof:Name', default='', namespaces=ns)
            org = team.find('iof:Organisation', ns)
            seura = org.findtext('iof:Name', default='', namespaces=ns) if org is not None else ''
            country = org.findtext('iof:Country', default='', namespaces=ns) if org is not None else ''

            # Laske osuudet joukkueesta
            members = team.findall('iof:TeamMemberResult', ns)
            num_legs = len(members)
            if max_legs is None:
                # Päättele: jos >4 niin oletetaan Jukola (7 osuutta), muuten Venlat (4 osuutta)
                if num_legs > 4:
                    max_legs = 7
                else:
                    max_legs = 4

            # --- Luo CSV-header dynaamisesti ---
            header = [
                "Kilpailunumero","Sarja","Joukkueen nimi","Kansalaisuus","Seura"," "
            ]
            for i in range(1, max_legs+1):
                header.extend([
                    f"Nimi-{i}", f"Kilpailukortti-{i}", f"Lainakortti-{i}",
                    f"Osuus-{i}", f"Alaosuus-{i}", f"Rata-{i}", f"Lähtöaika-{i}"
                ])
                header.append(" " * i)  # spacer kuten esimerkissänne
            if not rows:  # kirjoita header vain kerran
                rows.append(header)

            # --- Luo yksi joukkue-rivi ---
            row = [bib, sarja, team_name, country, seura, " "]

            for i in range(1, max_legs+1):
                if i <= num_legs:
                    member = members[i-1]
                    person = member.find('iof:Person', ns)
                    given = person.findtext('iof:Name/iof:Given', default='', namespaces=ns)
                    family = person.findtext('iof:Name/iof:Family', default='', namespaces=ns)
                    name = f"{given} {family}".strip()

                    result = member.find('iof:Result', ns)
                    leg = result.findtext('iof:Leg', default='', namespaces=ns) if result is not None else str(i)
                    course = result.findtext('iof:Course/iof:Name', default='', namespaces=ns) if result is not None else ""

                    row.extend([
                        name, "", "Kyllä", leg, "", course, ""  # kortti tyhjä, lähtöaika tyhjä
                    ])
                else:
                    # Täytä tyhjillä
                    row.extend([""] * 7)

                row.append(" " * i)

            rows.append(row)

    # --- Kirjoita CSV ---
    with open(out_csv, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f, quoting=csv.QUOTE_ALL)
        writer.writerows(rows)

    print(f"Kirjoitettu {len(rows)-1} joukkuetta tiedostoon {out_csv}")

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--iof", required=True, help="polku iof.xml tiedostoon")
    p.add_argument("--out", required=True, help="polku navisport csv:lle")
    p.add_argument("--max-legs", type=int, choices=[4,7], help="pakota osuuksien määrä (4=Venlat,7=Jukola)")
    args = p.parse_args()

    iof_to_navisport(args.iof, args.out, args.max_legs)

