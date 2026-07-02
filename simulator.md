# Race Simulator — `simulator.py`

Replays real IOF3 XML race data into a live Navisport server and/or a
WebSocket relay display, with configurable speed, team filtering, and
interactive debug confirmation.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                         simulator.py                             │
│                                                                  │
│  ┌──────────────┐   ┌────────────────────────────────────────┐  │
│  │ IOF3 Parser  │   │          Timeline Engine                │  │
│  │              │   │  shifts timestamps to now, applies      │  │
│  │  punch events│   │  speed factor, schedules each event     │  │
│  │  login events│   │  at the right wall-clock moment         │  │
│  │  purku events│   └──────────────┬─────────────────────────┘  │
│  └──────────────┘                  │                             │
│                          ┌─────────┴──────────┐                 │
│                          ▼                     ▼                 │
│             ┌─────────────────┐   ┌────────────────────────┐    │
│             │  NavisportSender│   │   DeviceClient (WS)    │    │
│             │                 │   │                        │    │
│             │  login → Result │   │  per-device WebSocket  │    │
│             │  punch → Passing│   │  connection to         │    │
│             │  finish→ Result │   │  listener.py /sim      │    │
│             │  purku → Result │   │                        │    │
│             └────────┬────────┘   └──────────┬─────────────┘    │
└──────────────────────┼────────────────────────┼──────────────────┘
                       ▼                        ▼
              Real Navisport server      listener.py
              (Socket.IO on port 80)     (WebSocket /sim)
```

Two independent output paths run in parallel by default. Either can be
disabled:

- **`--no-ws`** — disable the WebSocket DeviceClient path (use when only
  targeting a real Navisport server, with no `listener.py` running)
- Omit `--navisport` — disable the Navisport Socket.IO path (WS-only mode)

---

## Navisport integration — what happens per event type

### Login event
1. Fetches the current event from Navisport to find the runner's result
2. Lookup order: chip number → bib+leg
3. **If result found** (pre-registered via `register-all`): updates `chip`,
   `status=Competing`, and sets `startTime` from the IOF XML start time if the
   result doesn't already have one
4. **If no result found**: calls `build_single_runner` to create a new Team +
   Individual result pair, setting `startTime` from the IOF XML immediately
5. Caches `chip → result_id` and `runner_id → startTime` locally for
   elapsed-time computation in subsequent passings

### Punch event (intermediate control)
Sends a `Passing/Update` with:
- `chip`, `checkpointId` (resolved from cached checkpoint map), `deviceId`,
  `timestamp`
- `time` — elapsed race seconds, computed from the cached start time
- `resultId` — looked up from the `chip → result_id` cache; Navisport uses
  this to link the passing to the correct result

### Finish punch
Detected when the checkpoint's Navisport `type` is `Finish`, or when
`device_type` is `finish`, or when the control code is one of
`maali` / `finish` / `f`. The finish checkpoint must have a timing device
attached — the simulator aborts at connect time if it does not.

Sends the `Passing/Update` as above, then immediately sends a `Result/Update`
with `finishTime`, `finishTimeSource=Timing device`, `status=Finished`, and
the total elapsed time.

### Purku (chip dump)
Scheduled 10–15 minutes after the runner's last punch. Sends a full
`Result/Update` via `build_chip_result` with all split times as
`controlTimes`. The `status` field depends on the runner's IOF XML
`<Status>`:

| IOF XML `<Status>` | Navisport `status` sent | Effect |
|---|---|---|
| `OK` | *(omitted)* | Navisport validates controls and sets status itself |
| `DidNotFinish` | `Dnf` | Navisport records DNF |
| `DidNotStart` | `Dns` | Navisport records DNS |
| `Disqualified` | `Dsq` | Navisport records DSQ |

For `OK` runners, `status` is intentionally omitted so Navisport can
detect missing punches on its own (chip may have died mid-race).

### Hylkäysesitys → itkumuuri → manual OK (backup paper approval)

For runners whose IOF XML status is `OK`, the simulation models the full
real-world flow where a chip read may show missing punches:

1. **Purku** — chip is downloaded; `status` is omitted so Navisport validates
   the controls itself. Missing punches result in a temporary **hylkäysesitys**
   (disqualification proposal) with `status=Dnf`.
2. **Itkumuuri** — 5–15 minutes after purku the runner reaches the appeals desk.
   Officials verify the backup paper card.
3. **Manual OK** — 5–45 minutes after itkumuuri, officials approve the result.
   A `Result/Update` with `status='Ok'` is sent.

The full post-finish timeline for an OK runner:

```
finish punch          → Passing/Update + Result/Update(finishTime, status=Finished)
+ 10–15 min purku     → Result/Update(controlTimes)   — Navisport validates chip
+ 5–15 min itkumuuri  → itkumuuri event (hylkäysesitys, officials check paper)
+ 5–45 min manual_ok  → Result/Update(status=Ok)       — paper approved
```

This flow applies to **all** OK runners with real chip data, reflecting the
standard relay practice where backup papers are checked at the finish area.
Runners whose IOF XML status is not `OK` (DNF, DSQ) go through a separate
itkumuuri event without the manual_ok follow-up.

---

## CLI reference

```
python3 simulator.py -i <iof.xml> [options]
```

### Required

| Flag | Description |
|------|-------------|
| `-i` / `--iof` | Path to IOF3 XML result file |

### Team filtering

| Flag | Example | Behavior |
|------|---------|----------|
| `-r` / `--team-range` | `"1,3,5,14-55"` | Include only teams whose bib is in the set/range |
| `--limit-teams N` | `10` | Cap at the first N teams in XML order (applied after `--team-range`) |
| `--legs SPEC` | — | Leg numbers to simulate, same syntax as `--team-range`: `"4"`, `"2-4"`, `"1,3"` |

The two flags compose: `--team-range "101-200" --limit-teams 50` picks the
first 50 teams with bib 101–200.

### Simulation control

| Flag | Default | Description |
|------|---------|-------------|
| `-s` / `--speed` | `1.0` | Speed multiplier. `1.0` = real-time, `500` = 500× compressed |
| `-t` / `--start-offset` | `0.0` | Skip the first N hours of the race timeline |
| `--login-only` | off | Generate only login/check-in and mass-start events; skip punches, purku, itkumuuri |
| `-m` / `--finish-control` | — | Control code to treat as the finish; its device ID is renamed to `maali_1` |
| `--mass-starts` | — | Comma-separated ISO timestamps to inject as mass-start events |
| `--mass-start-time` | auto | Race start signal time (ISO). Defaults to the earliest `StartTime` in the XML |
| `--race` | auto | `venla`, `jukola`, or `auto` (auto-detects from `<Event><Name>`) |

### WebSocket output (relay display)

| Flag | Default | Description |
|------|---------|-------------|
| `-H` / `--host` | `127.0.0.1` | WebSocket server host |
| `-P` / `--port` | `8080` | WebSocket server port |
| `-o` / `--one-conn-per-device` | on | Reuse one WebSocket connection per device ID |
| `--no-ws` | off | **Skip all WebSocket DeviceClient connections.** Use when targeting real Navisport only, without `listener.py` running |

### Navisport output

| Flag | Description |
|------|-------------|
| `--navisport <url>` | Navisport local server URL, e.g. `http://navisport.local` or `http://127.0.0.1` |
| `--navisport-event-id <uuid>` | Navisport event UUID (required when `--navisport` is set) |
| `--navisport-chip-base N` | Base for auto-generated chip numbers: `chip = base + bib×1000 + leg`. Use to avoid collisions with pre-registered chips (default: `0`) |
| `--debug-navisport` | **Interactive step-through mode** — shows the full JSON payload before each send and asks for confirmation (see below) |

### Device counts / config

| Flag | Default | Description |
|------|---------|-------------|
| `-l` / `--login-devices` | from config | Number of check-in reader devices (overrides `simulator.conf login.device_count`) |
| `-d` / `--purku-devices` | `5` | Number of chip download stations |
| `-k` / `--itkumuuri-devices` | `3` | Number of appeal desk devices |
| `--config` | `simulator.conf` | Path to JSON config file |
| `-f` / `--controls-file` | — | File containing allowed control codes (one per line or JSON list); all others are filtered out |
| `-u` / `--controls-url` | — | URL returning a JSON array of allowed control codes |

---

## Usage modes

### Mode A — WebSocket only (no Navisport)

Use `listener.py` as a local stand-in. Useful for testing check-in
queue logic, event ordering, and broken-reader simulation before
connecting to real Navisport.

```bash
# Terminal 1: start the combined listener
python3 listener.py --port 8080

# Terminal 2: WebSocket mode, login-only test
python3 simulator.py -i results_2025_ve_iof.xml \
    -P 8080 --speed 10 --login-only --limit-teams 50
```

### Mode B — Navisport only (no relay display)

Sends directly to a running Navisport desktop app. Use `--no-ws` to
suppress the WebSocket DeviceClient connections (which would otherwise
error if no `listener.py` is running).

```bash
python3 simulator.py -i results_2025_ve_iof.xml \
    --navisport "http://navisport.local" \
    --navisport-event-id "<uuid>" \
    --speed 2.0 --limit-teams 100 --no-ws
```

The Navisport URL is the same as `--host` in `navisport_register.py`.

### Mode C — Both outputs simultaneously

Run `listener.py` on a different port and point the simulator at both:

```bash
python3 listener.py --port 8080

python3 simulator.py -i results_2025_ve_iof.xml \
    -P 8080 \
    --navisport "http://navisport.local" \
    --navisport-event-id "<uuid>" \
    --speed 5.0 --limit-teams 20
```

### Mode D — Navisport with interactive debug

Step through every payload before it is sent. Useful for verifying
exact field values and selectively skipping problem cases.

```bash
python3 simulator.py -i results_2025_ve_iof.xml \
    --navisport "http://127.0.0.1" \
    --navisport-event-id "<uuid>" \
    --speed 500 --limit-teams 1 -r "200" \
    --no-ws --debug-navisport
```

---

## `--debug-navisport` — interactive step-through

When this flag is set, each send (Result/Update or Passing/Update) is
intercepted before it reaches Navisport. The full JSON payload is printed
and the user is prompted:

```
[debug] ────────────────────────────────────────────────────────────
[debug]  Result/Update [login]  name=Mikael Mattsson  chip=200001  bib=200  leg=1  status=Competing
[debug] ────────────────────────────────────────────────────────────
{
  "id": "212a3e7a-4e0a-4363-8b8d-ff5222935bd0",
  "eventId": "0a2cdd9f-7c1d-465c-8021-e82efb69d2be",
  "chip": "200001",
  "status": "Competing",
  "startTime": "2025-06-14T13:30:00+03:00",
  ...
}
[debug] Send? [y]es / [n]o / [a]ll (disable debug) / [q]uit:
```

| Key | Action |
|-----|--------|
| `y` / Enter | Send this payload, continue prompting for the next |
| `n` | Skip (not sent to Navisport), continue |
| `a` | Send this one and all remaining without further prompts |
| `q` | Exit immediately |

The prompt label shows the operation type and key fields so you can
decide without reading the full JSON every time:

- `Result/Update [login]` — runner check-in / status update
- `Result/Update [new Individual]` / `[new Team]` — fresh registration
- `Passing/Update` — intermediate control punch
- `Result/Update [finish]` — finish time update
- `Result/Update [purku]` — full chip dump with split times
- `Result/Update [manual_ok]` — officials approved result from backup paper

The simulation is fully paused while a prompt is displayed — the asyncio
scheduler does not advance to the next event until you answer. This means
you see one event at a time in strict chronological order, never a backlog
of queued prompts.

---

## Navisport checkpoint requirements

At connect time the simulator fetches the checkpoint list from Navisport
(via `Event/Select`, with up to 3 retries). It then:

1. Builds a lookup map keyed by control `code` (integer or string). For
   checkpoints that have no numeric code, the `name` field is used as the
   key instead.
2. Prints the full checkpoint table so you can verify codes and device
   assignments before the simulation starts.
3. **Aborts immediately** if:
   - No checkpoint of type `Finish` is found for the event.
   - A `Finish` checkpoint exists but has no timing device attached.

Punches for unknown control codes are silently skipped (one warning per
code). If new checkpoints are added to Navisport mid-simulation they are
picked up automatically on the next 5-minute cache refresh.

---

## Check-in queue simulation

Configured via `simulator.conf` (JSON, created automatically on first run
if missing):

```json
{
  "login": {
    "device_count": 10,
    "processing_seconds": 20,
    "broken_reader_probability": 0.05,
    "broken_reader_extra_delay_seconds": 60,
    "broken_reader_downtime_seconds": 300,
    "non_first_leg_checkin_minutes_before_start": 60,
    "first_leg_checkin_windows": [
      {"bib_min": 1301, "bib_max": 999999, "earliest_min_before_start": 75, "latest_min_before_start": 65},
      {"bib_min":  801, "bib_max":   1300, "earliest_min_before_start": 65, "latest_min_before_start": 50},
      {"bib_min":  401, "bib_max":    800, "earliest_min_before_start": 50, "latest_min_before_start": 35},
      {"bib_min":    0, "bib_max":    400, "earliest_min_before_start": 35, "latest_min_before_start": 20}
    ]
  }
}
```

**Leg-1 runners** arrive in bib-number-based windows before the mass
start. **Leg 2+ runners** check in `non_first_leg_checkin_minutes_before_start`
minutes before their individual IOF XML start time.

When a device breaks mid-check-in:
1. The device is removed from the active pool immediately
2. The current runner is redirected to another device (+ extra delay)
3. Queued runners redistribute automatically
4. After `broken_reader_downtime_seconds` the device re-joins the pool

---

## Speed modes

| Mode | `--speed` | Wall-clock duration |
|------|-----------|---------------------|
| Real-time | `1.0` | Full race duration |
| Double speed | `2.0` | Half the real time |
| Fast check-in test | `10.0` | 1/10 real time |
| Full race in seconds | `500` | ~seconds for a Jukola-length race |

All timestamps are shifted so the first event aligns with `now` regardless
of the speed factor.

---

## Typical workflow against real Navisport

```bash
# 1. List events to find the UUID
python3 navisport_register.py list-events

# 2. Pre-register all teams from the XML
python3 navisport_register.py register-all \
    --iof results_2025_ve_iof.xml \
    --event-id <uuid> --legs 7

# 3. Test with one team in debug mode
python3 simulator.py -i results_2025_ve_iof.xml \
    --navisport "http://navisport.local" \
    --navisport-event-id <uuid> \
    --speed 500 -r "1" --no-ws --debug-navisport

# 4. Full simulation, 100 teams, 2× speed
python3 simulator.py -i results_2025_ve_iof.xml \
    --navisport "http://navisport.local" \
    --navisport-event-id <uuid> \
    --speed 2.0 --limit-teams 100 --no-ws
```

---

## `listener.py` — local mock server

Combines two protocols on one port for offline testing:

| Protocol | Path | Purpose |
|----------|------|---------|
| WebSocket | `/sim` | Receives JSON events from simulator DeviceClient (login, punch, purku, itkumuuri) |
| Socket.IO | `/` | Mimics the Navisport desktop app API (Event/Select, Result/Update, Passing/Update) |
| HTTP | `/health` | JSON status: `passings`, `results`, `ws_messages`, `checkpoints` |

### Start

```bash
python3 listener.py --port 8080   # default port is 8080
```

### Bundled checkpoints

Ships with a representative checkpoint set (codes 42, 73, 93, 100, 133,
266, 300 with real device UUIDs) so the simulator can resolve `checkpointId`
without a live Navisport instance.

### Result storage

- **Batch `Result/Update`** (`results` key): deduplicates by `id` —
  existing results are updated in-place, new ones appended. Re-registering
  the same teams does not create duplicates.
- **Single `Result/Update`** (`result` key): same deduplication logic.
- **Auto-registration**: on first `Passing/Update` for an unknown chip,
  a minimal `Individual` result is created automatically so finish and purku
  processing can find it.

### Output format

```
[ws] #1 [login          ] runner=200:1  device=login_3  ts=2026-07-01T12:29:12+03:00
[navisport] Event/Select: eventId=0a2cdd9f-... (4 results, 7 checkpoints)
[navisport] Passing #1: chip=200001  cp=42 (Checkpoint)  device=...  ts=...  time=492s
  [navisport] Result/Update: Mikael Mattsson | type=Individual chip=200001 leg=1 status=Finished | time=2738s | 21 controls
```

---

## Relay races — special notes

- **Start times**: leg-1 start = mass start time; leg N+1 start is read
  from the IOF XML `<StartTime>` of that leg. Both are propagated to
  Navisport as `startTime` on the Individual result at check-in time.
- **Chip numbers**: the IOF XML result format does not carry chip numbers.
  The simulator auto-generates them as `chip_base + bib×1000 + leg`.
  Use `--navisport-chip-base` to shift the range away from real chips.
- **Team result totals**: Navisport's Socket.IO receive path does not
  recalculate the Team parent result automatically. The
  `_try_update_team_result` helper in `navisport_register.py` implements
  this but is currently not called by the simulator (summing leg times
  and setting team status requires all legs to be finished first).
- **Relay exchange**: the next leg's `startTime` is set from the IOF XML,
  not derived from the previous leg's finish punch. For live relay
  exchange handling, the finish of leg N would need to trigger a
  `register-runner --start-time` call for leg N+1.

---

## Prerequisites

- [ ] `pip install -r requirements.txt`
- [ ] `navisport_register.py` in the same directory (imported by simulator)
- [ ] IOF3 XML file with `<SplitTime>` elements
- [ ] For Navisport mode:
  - Navisport desktop app running with the target event loaded
  - At least one checkpoint of type **Finish** configured for the event
  - The Finish checkpoint must have a **timing device** attached
  - Intermediate control checkpoints configured with the same codes as in
    the IOF XML (missing codes are skipped with a warning)
