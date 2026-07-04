# relaysimulator

Simulates relay race events in real time, replaying IOF3 XML result files as
live-like updates over WebSockets or directly to a **Navisport** desktop
instance.

Use cases include:

* Testing dashboards or visualizations without a live competition.
* Developing and debugging Navisport integration workflows.
* Demonstrating event flow and data handling.

* [simulator.md](simulator.md) — architecture, CLI reference, usage modes,
  Navisport integration details, listener.py, check-in queue simulation
* [protocol.md](protocol.md) *(if it exists)* — WebSocket message schema

---

## Quick reference

| Task | Command |
|------|---------|
| Install | `pip install -r requirements.txt` |
| Convert IOF XML → Navisport CSV | `python utils/iof_to_navisport.py --iof <file> --out <csv>` |
| Simulate (Navisport only) | `python simulator.py -i <file> --navisport <url> --navisport-event-id <uuid> --speed 2 --no-ws` |
| Simulate (WS only) | `python simulator.py -i <file> -P 8080 --speed 2` |
| Mock listener | `python listener.py --port 8080` |

See [simulator.md](simulator.md) for the full CLI reference, usage modes
(A–D), the `--debug-navisport` walkthrough, checkpoint requirements,
and relay-race specifics.

---

## Getting Started

1. **Install dependencies**

   ```bash
   python3 -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   ```

2. **Get IOF XML data**

   Fetch e.g. Jukola 2025 veterans results:
   ```bash
   mkdir -p data
   curl -o data/results_j2025_ve_iof.xml \
     https://results.jukola.com/tulokset/results_j2025_ve_iof.xml
   ```
   The official XML sometimes has wrong date values — fix if needed:
   ```bash
   python utils/fix_jukola_xml_date_values.py \
     data/results_j2025_ve_iof.xml \
     data/results_j2025_ve_iof_fixed.xml
   ```

3. **Start the mock listener** (for WebSocket mode)

   ```bash
   python listener.py --port 8080
   ```

4. **Run the simulator**

   ```bash
   python simulator.py -i data/results_j2025_ve_iof.xml -P 8080 --speed 2
   ```

5. **Open a dashboard**

   Open `dashboard.html` in a browser — it connects to the WebSocket
   and visualises the race in real time.

   For Navisport mode see the usage modes in [simulator.md](simulator.md).

---

## Project layout

```
├─ simulator.py               # main simulation engine (see simulator.md)
├─ listener.py                # local mock server (WS + Socket.IO)
├─ server_ws.py               # (legacy) simple WebSocket server
├─ dashboard.html             # example visualization
├─ simulator.conf             # check-in queue config
├─ simulator.md               # full documentation
├─ README.md
├─ utils/
│   ├─ iof_to_navisport.py           # IOF XML → Navisport CSV
│   ├─ fix_jukola_xml_date_values.py # fix Jukola date-offset bug
│   ├─ iofvalidator.py               # validate against IOF v3 XSD
│   ├─ extract_courses.py            # ResultList → CourseData
│   └─ jukola_split_controls.html    # map split labels to control codes
└─ data/
    └─ results_j*.xml         # IOF3 files (gitignored)
```

---

## Utilities (`utils/`)

| Script | Purpose |
|--------|---------|
| `iof_to_navisport.py --iof <xml> --out <csv>` | Converts IOF XML to a Navisport CSV for bulk team/runner import. Maps bib numbers, names, leg assignments, and auto-generates chip numbers (`bib×10 + leg`). Supports 4-leg (Venla) and 7-leg (Jukola) events. |
| `fix_jukola_xml_date_values.py <input> <output>` | Fixes date-offset errors in Jukola IOF XML files. The official Jukola results sometimes have incorrect day values in timestamps; this shifts dates past midnight by one day. |
| `iofvalidator.py <xml>` | Validates an IOF XML file against the official IOF Data Standard v3 XSD schema. Downloads the schema automatically on first run (cached as `IOF.xsd`). Uses `lxml` for strict validation. |
| `extract_courses.py --iof <xml> --out <courses.xml>` | Extracts course/control data from a ResultList XML into IOF CourseData format. With `--radat` and `--georef`, it also computes leg distances (haversine) and map pixel positions via bilinear interpolation. |
| `jukola_split_controls.html` | Browser tool that maps Jukola/Venla split-time labels to actual control codes. Given a team page URL, it scrapes each runner's punch data and matches them to intermediate times using timing offsets. Exports results as CSV. |

---

## WebSocket Output

Messages are JSON. Basic types: `race_start`, `control_passed`,
`leg_finished`, `race_end`.

```json
{
  "type": "control_passed",
  "team": "Team X",
  "leg": 1,
  "control": "5",
  "time": "00:32:15"
}
```

---

## License

\[Specify license here, e.g., MIT or Apache 2.0]
