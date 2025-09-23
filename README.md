# relaysimulator

Simulates relay race events in real time, with results streamed over WebSockets.
The long-term goal is to provide data directly consumable by **Navisport**, but **this branch does not yet include Navisport hooks**.

---

## Table of Contents

* [What is it](#what-is-it)
* [Features](#features)
* [Quick Start](#quick-start)
* [WebSocket Output](#websocket-output)
* [Navisport Integration](#navisport-integration)
* [Development Notes](#development-notes)
* [Limitations](#limitations)
* [Contributing](#contributing)
* [License](#license)

---

## What is it?

`relaysimulator` replays real relay events from official IOF XML result files, generating live-like updates as if the race were happening again.

Use cases include:

* Testing dashboards or visualizations without a live competition.
* Developing integrations for platforms such as **Navisport**.
* Demonstrating event flow and data handling.

---

## Features

* Reads IOF XML relay result files.
* Simulates progress of teams and legs in real time or at configurable speeds.
* Broadcasts updates to connected clients via WebSocket.
* Example dashboards (HTML/JS) for visualizing results.
* Data conversion utilities exist (e.g. `iof_to_navisport.py`), but **direct Navisport output is not yet implemented**.

---

## Quick Start

1. **Install dependencies**

   ```bash
   python3 -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   ```

2. **Prepare IOF XML results**

   * Place your IOF XML files in a folder called `data/` inside the project directory.
   * Make sure the file is **valid IOF XML** (e.g., from Venla, Jukola, or another official event).
   * Fetch data for simulation from: 
    ```bash
    YEAR="2025"  # Set the year here
    TYPE="ju"    # Set to "ju" for juniors or "ve" for veterans
    mkdir data
    curl -o data/results_j${YEAR}_${TYPE}_iof.xml https://results.jukola.com/tulokset/results_j${YEAR}_${TYPE}_iof.xml
    ```
    * fix the data. At least jukola relay 2025 contains wrong walues! 
    ```bash
    python fix_jukola_xml_date_values.py results_j2025_ju_iof.xml > results_j2025_ju_iof_fixed.xml
    ```



   * Example folder structure:

     ```
     relaysimulator/
     ├─ simulator.py
     ├─ server_ws.py
     ├─ dashboard.html
     └─ data/
         ├─ results_j2025_ve_iof.xml
         └─ results_j2025_ju_iof.xml
         └─ results_j2025_ju_iof_fixed.xml 
     ```
   * Use the full path when running the simulator, e.g.:

     ```bash
     python simulator.py --iof data/results_j2025_ju_iof_fixed.xml --speed 2
     ```

3. **Run the WebSocket server**

   ```bash
   python server_ws.py
   ```

4. **Start the simulator**

   ```bash
   python simulator.py --iof data/results_j2025_ve_iof.xml --speed 2
   ```

5. **Connect a dashboard**
   Open `dashboard.html` in a browser — it connects to the WebSocket and visualizes the race in real time.

---

## WebSocket Output

Messages are sent as JSON. Example update:

```json
{
  "type": "control_passed",
  "team": "Team X",
  "leg": 1,
  "control": "5",
  "time": "00:32:15"
}
```

Current message types include:

* `race_start`
* `control_passed`
* `leg_finished`
* `race_end`

---

## Navisport Integration

⚠️ **Note:** The current branch does **not** include a Navisport interface.
Instead:

* Data is exposed via WebSockets.
* Conversion utilities (`iof_to_navisport.py`) demonstrate the path toward eventual Navisport compatibility.
* Planned next steps include adding a Navisport connector or export format so simulations can be replayed directly in Navisport.

---

## Development Notes

* Language: Python 3
* Key packages: `aiohttp`, `websockets`, XML parsing libraries
* Extendable: you can add new exporters (e.g. to Navisport REST API once hooks are written).

---

## Limitations

* No direct Navisport output yet.
* Simulation relies on static IOF result data (no live GPS, for example).
* Dashboards are minimal demos.

---

## Contributing

Contributions are welcome, especially in:

* Adding Navisport integration
* Improving WebSocket protocol specification
* Creating richer dashboards

---

## License

\[Specify license here, e.g., MIT or Apache 2.0]
