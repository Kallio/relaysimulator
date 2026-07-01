#!/usr/bin/env python3
# simulator.py
import argparse
import asyncio
import xml.etree.ElementTree as ET
import json
import random
import aiohttp
import websockets
import os
import re
import sys
import uuid

from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Tuple, Optional

# --- Navisport integration (optional) ---
try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from navisport_register import NavisportConnector, now_iso as _navi_now_iso, build_chip_result as _build_chip_result, build_single_runner
except ImportError:
    NavisportConnector = None  # type: ignore
    _navi_now_iso = None


class NavisportSender:
    """
    Async wrapper around NavisportConnector for use inside the simulator.

    Checkpoints are cached once at connect time.  Runner start times are
    tracked locally from login/first-punch events so that passing.time
    (elapsed race seconds) can be computed without a round-trip per punch.
    Sync socketio calls are offloaded to the default thread executor so
    they never block the asyncio event loop.
    """

    def __init__(self, host: str, event_id: str, chip_base: int = 0, debug: bool = False):
        self.host = host
        self.event_id = event_id
        self._chip_base = chip_base
        self._conn: Optional[Any] = None
        self._cp_by_code: Dict[str, dict] = {}   # control code → checkpoint
        self._start_times: Dict[str, str] = {}   # runner_id → ISO start timestamp
        self._result_ids: Dict[str, str] = {}    # chip → result_id
        self._debug = debug
        self._debug_lock = threading.Lock()       # serialise interactive prompts

    async def connect(self):
        loop = asyncio.get_event_loop()
        conn = NavisportConnector(host=self.host)
        await loop.run_in_executor(None, conn.connect)
        self._conn = conn
        cps = await loop.run_in_executor(None, conn.get_checkpoints, self.event_id)
        self._cp_by_code = {cp['code']: cp for cp in (cps or []) if cp.get('code')}
        print(f"[navisport] Connected to {self.host}, event {self.event_id}, "
              f"{len(self._cp_by_code)} checkpoints cached")

    def _resolve_chip(self, ev: Dict) -> str:
        bib = int(ev.get('team_id', 0))
        leg = ev.get('leg', 1) or 1
        chip = ev.get('chip', '')
        if chip:
            return str(chip)
        return str(self._chip_base + max(bib, 0) * 1000 + leg)

    # ------------------------------------------------------------------
    # Helpers (sync, called from executor threads)
    # ------------------------------------------------------------------

    def _checkpoint_for(self, code: str) -> Tuple[Optional[str], str]:
        cp = self._cp_by_code.get(str(code))
        if cp:
            devices = cp.get('devices') or []
            return cp['id'], devices[0] if devices else str(code)
        return None, str(code)

    def _compute_elapsed(self, runner_id: str, timestamp: str) -> Optional[int]:
        start = self._start_times.get(str(runner_id))
        if not start:
            return None
        try:
            s = datetime.fromisoformat(start.replace('Z', '+00:00'))
            t = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
            return max(0, int((t - s).total_seconds()))
        except Exception:
            return None

    def _debug_confirm(self, label: str, payload: dict) -> bool:
        """
        In debug mode: print payload and prompt user.
        Returns True → send, False → skip.
        Acquires _debug_lock so concurrent threads prompt one at a time.
        """
        import json as _json
        with self._debug_lock:
            print(f"\n[debug] {'─'*60}")
            print(f"[debug]  {label}")
            print(f"[debug] {'─'*60}")
            print(_json.dumps(payload, indent=2, ensure_ascii=False))
            while True:
                try:
                    ans = input("[debug] Send? [y]es / [n]o / [a]ll (disable debug) / [q]uit: ").strip().lower()
                except EOFError:
                    return True
                if ans in ('y', 'yes', ''):
                    return True
                if ans in ('n', 'no'):
                    print("[debug] Skipped.")
                    return False
                if ans in ('a', 'all'):
                    self._debug = False
                    print("[debug] Debug mode off — sending all remaining commands automatically.")
                    return True
                if ans in ('q', 'quit', 'exit'):
                    print("[debug] Quit.")
                    import sys
                    sys.exit(0)
                print("[debug] Enter y, n, a, or q.")

    def _send_result(self, result: dict, event_id: str, label: str = '') -> str:
        """Send a Result/Update, optionally gated by debug confirmation."""
        if self._debug:
            tag = label or (
                f"Result/Update  type={result.get('resultType','?')}  "
                f"name={result.get('name','?')}  chip={result.get('chip','?')}  "
                f"status={result.get('status','?')}"
            )
            if not self._debug_confirm(tag, result):
                return 'skipped'
        return self._conn.send_result(result, event_id)

    def _send_passing(self, passing: dict, label: str = '') -> str:
        """Send a Passing/Update, optionally gated by debug confirmation."""
        if self._debug:
            tag = label or (
                f"Passing/Update  chip={passing.get('chip','?')}  "
                f"deviceId={passing.get('deviceId','?')}  "
                f"time={passing.get('time','?')}s"
            )
            if not self._debug_confirm(tag, passing):
                return 'skipped'
        return self._conn.send_passing(passing)

    def _sync_send_login(self, ev: Dict, timestamp: str):
        """Register runner on Navisport (if not yet) and set status to Competing."""
        if not self._conn:
            return
        event = self._conn.get_event(self.event_id)
        if not event:
            return
        runner_id = str(ev.get('runner_id', ''))
        bib = int(ev.get('team_id', 0))
        leg = ev.get('leg', 1) or 1
        chip = self._resolve_chip(ev)
        name = ev.get('runner_name', '') or ''
        club = ev.get('club', '') or ''

        all_results = event.get('results', [])

        # Try lookup by chip first
        result = next(
            (r for r in all_results
             if str(r.get('chip', '')) == chip and r.get('resultType') == 'Individual'),
            None,
        )
        # Then try bib+leg
        if not result and bib > 0:
            team_id = next(
                (r['id'] for r in all_results
                 if r.get('resultType') == 'Team' and str(r.get('bibNumber', '')) == str(bib)),
                None,
            )
            if team_id:
                result = next(
                    (r for r in all_results
                     if r.get('resultType') == 'Individual'
                     and r.get('parentId') == team_id
                     and r.get('leg') == leg),
                    None,
                )

        iof_start = ev.get('start_time')

        if result:
            self._start_times[runner_id] = result.get('startTime') or iof_start or timestamp
            self._result_ids[chip] = result['id']
            updated = {
                **result,
                'chip': chip,
                'status': 'Competing',
                'registerTime': timestamp,
                'updated': _navi_now_iso(),
            }
            if not result.get('startTime') and iof_start:
                updated['startTime'] = iof_start
                updated['startTimeSource'] = 'Timing device'
            self._send_result(updated, self.event_id,
                              f"Result/Update [login]  name={updated.get('name','?')}  "
                              f"chip={chip}  bib={bib}  leg={leg}  status=Competing")
            print(f"[navisport] login: updated result {result['id']} chip={chip}")
        else:
            new_results = build_single_runner(
                event_id=self.event_id,
                name=name,
                club=club or None,
                nationality='FIN',
                bib=bib if bib > 0 else None,
                chip=chip,
                leg=leg,
            )
            individual = next((r for r in new_results if r.get('resultType') == 'Individual'), None)
            if individual:
                if iof_start:
                    individual['startTime'] = iof_start
                    individual['startTimeSource'] = 'Timing device'
                self._start_times[runner_id] = iof_start or timestamp
                self._result_ids[chip] = individual['id']
            for r in new_results:
                self._send_result(r, self.event_id,
                                  f"Result/Update [new {r.get('resultType','?')}]  "
                                  f"name={r.get('name','?')}  chip={chip}  bib={bib}  leg={leg}")
            print(f"[navisport] login: registered {len(new_results)} result(s) chip={chip} bib={bib} leg={leg}")

    def _sync_send_punch(self, ev: Dict, timestamp: str):
        if not self._conn:
            return
        runner_id = str(ev.get('runner_id', ''))
        code = str(ev.get('device_id', ''))
        device_type = ev.get('device_type', '')
        chip = self._resolve_chip(ev)
        cp_id, dev_id = self._checkpoint_for(code)
        elapsed = self._compute_elapsed(runner_id, timestamp)
        passing = {
            'id': str(uuid.uuid4()),
            'eventId': self.event_id,
            'chip': chip,
            'deviceId': dev_id,
            'timestamp': timestamp,
            'checkpointId': cp_id,
        }
        if elapsed is not None:
            passing['time'] = elapsed
        result_id = self._result_ids.get(chip)
        if result_id:
            passing['resultId'] = result_id
        self._send_passing(passing,
                           f"Passing/Update  chip={chip}  control={code}  "
                           f"elapsed={elapsed}s  cp={'found' if cp_id else 'unknown'}")

        is_finish = device_type == 'finish' or str(code).lower() in ('maali', 'finish', 'f')
        if is_finish:
            self._sync_finish_result(chip, timestamp, elapsed)

    def _sync_finish_result(self, chip: str, timestamp: str, elapsed: Optional[int]):
        """Send Result/Update with finishTime for the finishing runner."""
        event = self._conn.get_event(self.event_id)
        if not event:
            return
        candidates = [
            r for r in event.get('results', [])
            if str(r.get('chip', '')) == chip or str(r.get('secondaryChip', '')) == chip
        ]
        candidates.sort(key=lambda r: (
            0 if r.get('status') == 'Competing' else 1,
            r.get('leg') or 0,
            r.get('startTime') or '',
        ))
        result = next(
            (r for r in candidates if r.get('status') in ('Registered', 'Competing')),
            candidates[0] if candidates else None,
        )
        if not result:
            return
        # Fallback: compute elapsed from result.startTime if not cached locally
        if elapsed is None and result.get('startTime'):
            try:
                s = datetime.fromisoformat(result['startTime'].replace('Z', '+00:00'))
                t = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
                elapsed = max(0, int((t - s).total_seconds()))
            except Exception:
                pass
        finish_result = {
            **result,
            'finishTime': timestamp,
            'finishTimeSource': 'Timing device',
            'status': 'Finished' if result.get('status') in ('Competing', 'Registered', 'Dns') else result.get('status'),
            'updated': _navi_now_iso(),
        }
        if elapsed is not None:
            finish_result['time'] = elapsed
        self._send_result(finish_result, self.event_id,
                          f"Result/Update [finish]  chip={chip}  elapsed={elapsed}s  "
                          f"finishTime={timestamp}")

    # ------------------------------------------------------------------
    # Async entry points called from schedule_and_send
    # ------------------------------------------------------------------

    def _sync_send_purku(self, ev: Dict, punches: list, purku_ts: str):
        """Send a full chip card read (all punches) to Navisport via Result/Update."""
        if not self._conn:
            return
        event_data = self._conn.get_event(self.event_id)
        if not event_data:
            print(f"[navisport] purku: event {self.event_id} not found")
            return
        runner_id = str(ev.get('runner_id', ''))
        chip = self._resolve_chip(ev)
        result = next(
            (r for r in event_data.get('results', [])
             if str(r.get('chip', '')) == chip or str(r.get('secondaryChip', '')) == chip),
            None,
        )
        if not result:
            print(f"[navisport] purku: no result found for chip {chip}")
            return

        start_time_str = self._start_times.get(runner_id) or result.get('startTime')
        start_dt = None
        if start_time_str:
            try:
                start_dt = datetime.fromisoformat(start_time_str.replace('Z', '+00:00'))
            except Exception:
                pass

        controls = []
        for punch in punches:
            code = str(punch.get('control', ''))
            punch_ts_str = punch.get('time', '')
            if not code or not punch_ts_str:
                continue
            if start_dt:
                try:
                    pt = datetime.fromisoformat(punch_ts_str.replace('Z', '+00:00'))
                    secs = max(0, int((pt - start_dt).total_seconds()))
                    controls.append((code, secs))
                except Exception:
                    pass
            else:
                controls.append((code, 0))

        if not controls:
            print(f"[navisport] purku: no valid punches for chip {chip}")
            return

        chip_result = _build_chip_result(
            result_id=result['id'],
            event_id=self.event_id,
            controls=controls,
            start_time=start_time_str,
            status=None,  # let Navisport validate controls and set status
        )
        chip_result['readTime'] = purku_ts
        self._send_result(chip_result, self.event_id,
                          f"Result/Update [purku]  chip={chip}  punches={len(controls)}  "
                          f"startTime={start_time_str}")
        print(f"[navisport] purku: sent {len(controls)} punches for chip {chip}")

    async def on_event(self, event: Dict[str, Any], sent_ts: str, msg_obj: Optional[Dict[str, Any]] = None):
        if not self._conn:
            return
        loop = asyncio.get_event_loop()
        etype = event.get('event')

        if etype == 'login':
            await loop.run_in_executor(None, self._sync_send_login, event, sent_ts)

        elif etype == 'punch':
            await loop.run_in_executor(None, self._sync_send_punch, event, sent_ts)

        elif etype == 'results_purku':
            # Use shifted punches from msg_obj (timestamps already adjusted for sim speed)
            punches = (msg_obj or {}).get('punches', event.get('punches', []))
            await loop.run_in_executor(None, self._sync_send_purku, event, punches, sent_ts)

    async def close(self):
        if self._conn:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self._conn.disconnect)
            self._conn = None
            print("[navisport] Disconnected")


# --- lajittelun apufunktio ---
def event_sort_key(ev: Dict[str, Any]):
    etype = ev.get("event", "")
    devid = ev.get("device_id", "")

    if etype == "login":
        m = re.match(r"login_(\d+)", str(devid))
        return (0, int(m.group(1)) if m else 9999)

    if etype == "mass_start":
        return (1, 0)

    if etype == "punch":
        try:
            return (2, int(devid))
        except Exception:
            return (2, 9999)

    if etype == "results_purku":
        m = re.match(r"purku_(\d+)", str(devid))
        return (3, int(m.group(1)) if m else 9999)

    if etype == "itkumuuri":
        return (4, 0)

    return (99, str(devid))

# --- Predefined login devices ---
PURKU_DEVICES      = [f"purku_{i}" for i in range(1, 6)]      # 5 tulosten purku
ITKUMUURI_DEVICES = [f"itkumuuri_{i}" for i in range(1, 4)] # 3 itkumuuri



# --- Check-in staging (bib-based, Jukola/Venla rules) ---

def checkin_window_for_bib(bib: int, windows: list) -> tuple:
    """
    Return (earliest_minutes_before_start, latest_minutes_before_start)
    for a bib number based on the config-defined windows list.

    Each entry in windows: {bib_min, bib_max, earliest_min_before_start, latest_min_before_start}
    """
    for w in windows:
        if w['bib_min'] <= bib <= w['bib_max']:
            return (w['earliest_min_before_start'], w['latest_min_before_start'])
    return (75, 20)


def assign_checkin_events(all_by_runner: dict, mass_start_ts: datetime,
                          bib_map: dict, login_config: dict) -> list:
    """
    Generate login (check-in) events.

    Leg 1 runners use bib-number-based staging windows defined in login_config.
    Leg >1 runners check in ~non_first_leg_checkin_minutes_before_start
    before their individual StartTime.

    Returns list of (timestamp, event_dict) tuples.
    Each event carries a ``login_device`` key (device_id) for later queue
    simulation — the actual device assignment may be rebalanced during
    event processing if login devices are oversubscribed.
    """
    import random as _rnd
    device_count = login_config.get('device_count', 10)
    login_devices = [f"login_{i}" for i in range(1, device_count + 1)]
    windows = login_config.get('first_leg_checkin_windows', [])
    non_first_min = login_config.get('non_first_leg_checkin_minutes_before_start', 60)

    extra = []
    for runner_id, punches in all_by_runner.items():
        if not punches:
            continue

        first_ev = punches[0][1]
        leg = first_ev.get('leg', 1)
        bib = bib_map.get(runner_id, 999)
        device_id = _rnd.choice(login_devices)
        runner_name = first_ev.get('runner_name', '') or ''
        club = first_ev.get('club', '') or ''

        if leg == 1:
            early_min, late_min = checkin_window_for_bib(bib, windows)
            minutes_before = _rnd.randint(late_min, early_min)
            login_ts = mass_start_ts - timedelta(minutes=minutes_before)
            runner_start_dt = mass_start_ts
            note = f'login {minutes_before}min before mass start (bib={bib}, leg 1)'
        else:
            start_time_str = first_ev.get('start_time')
            if start_time_str:
                try:
                    runner_start_dt = datetime.fromisoformat(start_time_str)
                except Exception:
                    runner_start_dt = mass_start_ts
            else:
                runner_start_dt = mass_start_ts
            login_ts = runner_start_dt - timedelta(minutes=non_first_min)
            note = f'login {non_first_min}min before leg start (bib={bib}, leg {leg})'

        extra.append((login_ts, {
            'timestamp': login_ts.isoformat(),
            'runner_id': runner_id,
            'runner_name': runner_name,
            'club': club,
            'team_id': str(bib),
            'device_id': device_id,
            'device_type': 'login',
            'event': 'login',
            'status': 'ok',
            'leg': leg,
            'start_time': runner_start_dt.isoformat(),
            'note': note,
        }))
    return extra


def detect_race_from_xml(iof_path: str) -> str:
    """Read <Event><Name> from IOF3 XML and return 'venla' or 'jukola'."""
    import xml.etree.ElementTree as _ET
    try:
        tree = _ET.parse(iof_path)
        root = tree.getroot()
        ns_uri = root.tag.split('}')[0].strip('{') if '}' in root.tag else None
        ns = {'iof': ns_uri} if ns_uri else None
        name = root.findtext('.//iof:Name', namespaces=ns) or ''
        name_lower = name.lower()
        if 'venla' in name_lower:
            return 'venla'
        if 'jukola' in name_lower:
            return 'jukola'
    except Exception:
        pass
    return 'venla'  # default


def parse_mass_start_time(iof_path: str) -> datetime:
    """Find the earliest start time in the XML as the race start signal."""
    import xml.etree.ElementTree as _ET
    tree = _ET.parse(iof_path)
    root = tree.getroot()
    ns_uri = root.tag.split('}')[0].strip('{') if '}' in root.tag else None
    ns = {'iof': ns_uri} if ns_uri else None
    earliest = None
    for st in root.findall('.//iof:StartTime', ns):
        try:
            dt = try_parse_time(st.text or '')
            if earliest is None or dt < earliest:
                earliest = dt
        except Exception:
            pass
    return earliest or datetime.now(timezone.utc)


# --- Configuration ---

DEFAULT_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'simulator.conf')

CONFIG_DEFAULTS = {
    'login': {
        'device_count': 10,
        'processing_seconds': 20,
        'broken_reader_probability': 0.0,
        'broken_reader_extra_delay_seconds': 60,
        'broken_reader_downtime_seconds': 300,
        'non_first_leg_checkin_minutes_before_start': 60,
        'first_leg_checkin_windows': [
            {'bib_min': 1301, 'bib_max': 999999, 'earliest_min_before_start': 75, 'latest_min_before_start': 65},
            {'bib_min': 801,  'bib_max': 1300,   'earliest_min_before_start': 65, 'latest_min_before_start': 50},
            {'bib_min': 401,  'bib_max': 800,    'earliest_min_before_start': 50, 'latest_min_before_start': 35},
            {'bib_min': 0,    'bib_max': 400,    'earliest_min_before_start': 35, 'latest_min_before_start': 20},
        ],
    },
}


def load_config(path: str = DEFAULT_CONFIG_PATH) -> dict:
    """Load JSON config, merging with defaults.  Missing keys fall back to defaults."""
    config = json.loads(json.dumps(CONFIG_DEFAULTS))  # deep copy
    if os.path.exists(path):
        with open(path) as f:
            user = json.load(f)
        for section, values in user.items():
            if section in config and isinstance(values, dict):
                config[section].update(values)
            else:
                config[section] = values
    return config


def generate_default_config(path: str = DEFAULT_CONFIG_PATH):
    """Write default config file if it doesn't exist."""
    if not os.path.exists(path):
        with open(path, 'w') as f:
            json.dump(CONFIG_DEFAULTS, f, indent=2)
        print(f"Default config written to {path}")


# --- Configurable message format helpers ---
def make_message(ev: Dict[str, Any]) -> str:
    # yksi JSON-rivi per tapahtuma
    return json.dumps(ev, separators=(',', ':')) + "\n"

def parse_team_range(range_str: str) -> set[int]:
    """
    Parse string like "1,3,5,14-55" into a set of integers.
    """
    result = set()
    for part in range_str.split(','):
        part = part.strip()
        if '-' in part:
            try:
                start, end = map(int, part.split('-'))
                result.update(range(start, end+1))
            except ValueError:
                raise ValueError(f"Virheellinen range osa: '{part}'")
        else:
            try:
                result.add(int(part))
            except ValueError:
                raise ValueError(f"Virheellinen bib-numero: '{part}'")
    return result

# --- IOF XML parsing ---
def parse_iof3_events(iof_path: str, team_range: Optional[set[int]] = None,
                      team_limit: Optional[int] = None) -> List[Dict[str, Any]]:
    tree = ET.parse(iof_path)
    root = tree.getroot()
    ns_uri = root.tag.split('}')[0].strip('{') if '}' in root.tag else None
    ns = {'iof': ns_uri} if ns_uri else None
    events = []
    all_teams_count = 0
    included_teams_count = 0


    for team in root.findall('.//iof:TeamResult', ns):
        all_teams_count += 1
        team_bib_text = team.findtext('iof:BibNumber', namespaces=ns) or team.get('bib') or team.get('id') or None

        # Suodata joukkueet halutun rangen mukaan
        if team_range and team_bib_text:
            try:
                team_bib_num = int(team_bib_text)
                if team_bib_num not in team_range:
                    continue
            except ValueError:
                continue
        included_teams_count += 1

        # Limit to first N teams (XML order, after bib filter)
        if team_limit and included_teams_count > team_limit:
            break

        org = team.find('iof:Organisation', ns)
        club = org.findtext('iof:Name', namespaces=ns) or '' if org is not None else ''

        if ns:
            members = team.findall('.//iof:TeamMemberResult', ns)
        else:
            members = team.findall('.//TeamMemberResult')
        if not members:
            continue

        for idx, member in enumerate(members, start=1):
            person_id = None
            person_el = member.find('iof:Person', ns)
            if person_el is not None:
                person_id = (
                    person_el.findtext('iof:PersonID', namespaces=ns) or
                    person_el.findtext('iof:Id', namespaces=ns) or
                    person_el.findtext('iof:ID', namespaces=ns)
                )
                name_el = person_el.find('iof:Name', ns)
                runner_name = ""
                if name_el is not None:
                    given = name_el.findtext('iof:Given', namespaces=ns) or ""
                    family = name_el.findtext('iof:Family', namespaces=ns) or ""
                    runner_name = f"{given} {family}".strip()
            else:
                person_id = member.get('id') or member.get('MemberID') or None
                runner_name = None

            if not person_id:
                person_id = f"{team_bib_text or 'team'}:{idx}"

            result = member.find('iof:Result', ns)
            if result is None:
                continue

            start_time_txt = result.findtext('iof:StartTime', namespaces=ns) if ns else result.findtext('StartTime')
            start_dt = try_parse_time(start_time_txt) if start_time_txt else None
            if start_dt and start_dt.tzinfo is None:
                start_dt = start_dt.replace(tzinfo=timezone.utc)

            status_txt = result.findtext('iof:Status', namespaces=ns)

            for split in result.findall('iof:SplitTime', ns):
                code = split.findtext('iof:ControlCode', namespaces=ns)
                time_txt = split.findtext('iof:Time', namespaces=ns)
                if not time_txt or not code:
                    continue

                ts = None
                if start_dt:
                    try:
                        offset = int(time_txt)
                        ts = start_dt + timedelta(seconds=offset)
                    except ValueError:
                        try:
                            hh, mm, ss = map(int, time_txt.split(":"))
                            delta_sec = (hh*3600 + mm*60 + ss) - (start_dt.hour*3600 + start_dt.minute*60 + start_dt.second)
                            ts = start_dt + timedelta(seconds=delta_sec)
                        except Exception:
                            ts = start_dt

                if ts:
                    events.append({
                        'timestamp': ts.isoformat(),
                        'runner_id': person_id,
                        'runner_name': runner_name,
                        'club': club,
                        'team_id': team_bib_text,
                        'device_id': code,
                        'device_type': guess_device_type(code, None),
                        'raw_time': time_txt,
                        'status': status_txt or 'OK',
                        'event': 'punch',
                        'leg': idx,
                        'start_time': start_dt.isoformat() if start_dt else None,
                    })

    events.sort(key=lambda e: e['timestamp'])
    print(f"Teams in XML: {all_teams_count}")
    msg = f"Teams included: {included_teams_count}"
    if team_limit:
        msg += f" (limited to first {team_limit})"
    print(msg)
    return events

def try_parse_time(t: str) -> datetime:
    # Yritetään useita formaatteja; lisää tarvittaessa 2025-06-14T23:00:00+03:00
    fmts = ["%Y-%m-%dT%H:%M:%S%z"]
    for f in fmts:
        try:
            dt = datetime.strptime(t, f)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except Exception:
            pass
    # fallback: nykyhetki UTC
    return datetime.now(timezone.utc)

def guess_device_type(control: str, status: str) -> str:
    # Yksinkertainen heuristiikka;
    if control is None:
        return 'unknown'
    c = control.lower()
    if 'start' in c or c.startswith('s'):
        return 'login'
    if 'mass_start ' in c :
        return 'mass_start'
    if 'finish' in c or 'maali' in c or 'f' == c:
        return 'finish'
    if 'exchange' in c or 'vaihto' in c:
        return 'exchange'
    # oletuksena väliaikarasti
    return 'split'

# --- Simulator core: schedule and send events with speed factor ---
async def load_allowed_controls(file_path: str = None, url: str = None) -> set:
    controls = set()

    # Lue paikallinen tiedosto (JSON-lista käytä avuksi jukola_split_controls.html )
    if file_path and os.path.exists(file_path):
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                text = f.read()
                try:
                    data = json.loads(text)
                    if isinstance(data, list):
                        for item in data:
                            if item is not None:
                                controls.add(str(item).strip().lower())
                    else:
                        print(f"Warning: controls file {file_path} does not contain a JSON list")
                except json.JSONDecodeError:
                    # Fallback: rivikohtainen lista (kuten alkuperäisessä)
                    f.seek(0)
                    for ln in f:
                        code = ln.strip()
                        if code:
                            controls.add(code.lower())
        except Exception as e:
            print(f"Warning: could not read controls file {file_path}: {e}")

    # Hae URLista (odotetaan JSON-listaa) # tämä tulis varmaan suoraa navisportista
    if url:
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.get(url, timeout=10) as resp:
                    if resp.status == 200:
                        try:
                            data = await resp.json()
                        except Exception:
                            text = await resp.text()
                            try:
                                data = json.loads(text)
                            except Exception:
                                data = None
                        if isinstance(data, list):
                            for item in data:
                                if item is not None:
                                    controls.add(str(item).strip().lower())
                        else:
                            print(f"Warning: controls URL did not return a JSON list")
                    else:
                        print(f"Warning: controls URL returned status {resp.status}")
        except Exception as e:
            print(f"Warning: could not fetch controls from {url}: {e}")

    return controls



def normalize(c):
    return None if c is None else str(c).strip().lower()

def control_allowed(control, allowed_controls:set):
    if not allowed_controls:
        return True
    nc = normalize(control)
    return nc in allowed_controls

# globaali sanakirja laitteiden tiloille ja viestimäärille
device_status = {}
device_order = []  # järjestys, jossa laitteet tulostetaan
device_msg_count = {}

def update_dashboard(device_id):
    """Päivittää vain yhden laitteen rivin konsolissa."""
    idx = device_order.index(device_id)
    # siirrä kursori oikealle riville
    sys.stdout.write(f"\033[{len(device_order) - idx}F")  # siirrytään ylös rivien mukaan
    sys.stdout.write("\033[K")  # tyhjennä rivi
    count = device_msg_count.get(device_id, 0)
    status = device_status.get(device_id, "pending")
    print(f"{device_id:14}: {status:20} sent: {count}")
    # siirry takaisin konsolin loppuun
    sys.stdout.write(f"\033[{len(device_order) - idx}E")
    sys.stdout.flush()


class DeviceClient:
    def __init__(self, device_id, host, port):
        self.device_id = device_id
        self.host = host
        self.port = port
        self.ws = None
        self.queue = asyncio.Queue()
        self.sender_task = None
        self.sent_count = 0

        if device_id not in device_order:
            device_order.append(device_id)
            device_status[device_id] = "pending"
            device_msg_count[device_id] = 0
            # tulostetaan aloitusdashboard
            print(f"{device_id:10}: {device_status[device_id]:20} sent: 0")

    async def connect(self):
        if not self.ws:
            try:
                self.ws = await websockets.connect(f"ws://{self.host}:{self.port}/sim")
                device_status[self.device_id] = "connected"
                update_dashboard(self.device_id)
            except Exception as e:
                device_status[self.device_id] = "connect error"
                update_dashboard(self.device_id)
                print(f"[{self.device_id}] connect error: {e}")
                self.ws = None

        if self.ws and not self.sender_task:
            self.sender_task = asyncio.create_task(self._sender())

    async def _reconnect(self):
        for delay in [1, 2, 5]:
            try:
                self.ws = await websockets.connect(f"ws://{self.host}:{self.port}/sim")
                device_status[self.device_id] = "reconnected"
                update_dashboard(self.device_id)
                return
            except Exception as e:
                device_status[self.device_id] = "reconnecting"
                update_dashboard(self.device_id)
                print(f"[{self.device_id}] reconnect failed: {e}")
                await asyncio.sleep(delay)
        self.ws = None
        device_status[self.device_id] = "conn failed"
        update_dashboard(self.device_id)

    async def _sender(self):
        while True:
            msg = await self.queue.get()
            if msg is None:
                break
            for _ in range(3):
                try:
                    await self.ws.send(msg)
                    self.sent_count += 1
                    device_msg_count[self.device_id] = self.sent_count
                    device_status[self.device_id] = "sent"
                    update_dashboard(self.device_id)
                    await asyncio.sleep(0.05)
                    break
                except Exception:
                    device_status[self.device_id] = "send error"
                    update_dashboard(self.device_id)
                    self.ws = None
                    await self._reconnect()
                    if not self.ws:
                        break
        if self.ws:
            await self.ws.close()
        self.ws = None
        device_status[self.device_id] = "disconnected"
        update_dashboard(self.device_id)

    async def send(self, message: str):
        if not self.ws:
            await self.connect()
            if not self.ws:
                return
        await self.queue.put(message)

    async def close(self):
        if self.sender_task:
            await self.queue.put(None)
            await self.sender_task
        if self.ws:
            await self.ws.close()
        self.ws = None
        device_status[self.device_id] = "closed"
        update_dashboard(self.device_id)

async def run_simulator(events: List[Dict[str, Any]],
                        host: str,
                        port: int,
                        speed: float,
                        one_conn_per_device: bool,
                        allowed_controls: set,
                        start_offset: float,
                        finish_control: Optional[str] = None,
                        mass_start_times: Optional[List[datetime]] = None,
                        navisport_sender: Optional['NavisportSender'] = None,
                        race: str = 'venla',
                        bib_map: Optional[dict] = None,
                        mass_start_signal: Optional[datetime] = None,
                        login_config: Optional[dict] = None,
                        login_only: bool = False,
                        no_ws: bool = False):

    if navisport_sender:
        await navisport_sender.connect()

    device_clients: Dict[str, DeviceClient] = {}

    # --- 1. Aikajanan valmistelu ---
    all_timeline: List[Tuple[datetime, Dict[str, Any]]] = [
        (datetime.fromisoformat(ev['timestamp']), ev) for ev in events
    ]
    if not all_timeline:
        print("No events found.")
        return
    all_timeline.sort(key=lambda x: x[0])

    # --- 2. Start offset ---
    base_time = all_timeline[0][0]
    cutoff_time = base_time + timedelta(hours=start_offset)
    filtered = [(ts, ev) for ts, ev in all_timeline if ts >= cutoff_time]
    if not filtered:
        print(f"All events skipped by start-offset {start_offset}h")
        return

    # --- 3. Punchit juoksijoittain ---
    all_by_runner: Dict[str, List[Tuple[datetime, Dict[str, Any]]]] = {}
    for ts, ev in filtered:
        if ev.get('event') != 'punch':
            continue
        runner = ev.get('runner_id') or ev.get('runner_name') or 'unknown'
        all_by_runner.setdefault(runner, []).append((ts, ev))

    published_by_runner: Dict[str, List[Tuple[datetime, Dict[str, Any]]]] = {}
    for runner, punches in all_by_runner.items():
        published_by_runner[runner] = list(punches)

    # --- 4. Extra eventit ---
    extra_events: List[Tuple[datetime, Dict[str, Any]]] = []

    # mass start
    for ts in (mass_start_times or []):
        event = {
            'timestamp': ts.isoformat(),
            'runner_id': 'mass_start',
            'device_id': 'mass_start',
            'device_type': 'mass_start',
            'event': 'mass_start',
            'note': 'Mass start at known time'
        }
        extra_events.append((ts, event))
        print(f"Mass start event added at {ts.isoformat()}")

    # login — bib-based + non-first-leg staging, with queue simulation config
    mass_start_signal = mass_start_signal or (mass_start_times or [None])[0] or base_time
    _login_cfg = login_config or {}
    if 'login' in _login_cfg:
        _login_cfg = _login_cfg['login']
    extra_events += assign_checkin_events(all_by_runner, mass_start_signal,
                                          bib_map or {}, _login_cfg)

    if login_only:
        combined = extra_events[:]  # only login + mass start events
    else:
        combined = extra_events + [e for lst in published_by_runner.values() for e in lst]

    if not login_only:
        # dump, itkumuuri
        for runner, punches in all_by_runner.items():
            if not punches:
                continue
            evs_sorted = sorted(punches, key=lambda x: x[0])
            last_ts = evs_sorted[-1][0]

            # purku (chip dump after finish)
            minutes_after = random.randint(10, 15)
            purku_ts = last_ts + timedelta(minutes=minutes_after)
            punches_dump = [{
                'control': e.get('device_id'),
                'time': e.get('timestamp'),
                'status': e.get('status'),
                'device_type': e.get('device_type')
            } for _, e in evs_sorted]

            purku_event = {
                'timestamp': purku_ts.isoformat(),
                'runner_id': runner,
                'device_id': random.choice(PURKU_DEVICES),
                'device_type': 'results_purku',
                'event': 'results_purku',
                'purku_time': purku_ts.isoformat(),
                'punches': punches_dump,
                'note': f'purku {minutes_after}min after last punch'
            }
            extra_events.append((purku_ts, purku_event))

            # itkumuuri (DQ appeal desk)
            runner_status = None
            for _, e in evs_sorted:
                if e.get('status'):
                    runner_status = e['status']
                    break
            if runner_status and runner_status not in ("OK", "Finished"):
                itkumuuri_delay = random.randint(5, 60)
                itkumuuri_ts = purku_ts + timedelta(minutes=itkumuuri_delay)
                itkumuuri_event = {
                    'timestamp': itkumuuri_ts.isoformat(),
                    'runner_id': runner,
                    'device_id': random.choice(ITKUMUURI_DEVICES),
                    'device_type': 'itkumuuri',
                    'event': 'itkumuuri',
                    'status': runner_status,
                    'note': f'itkumuuri {itkumuuri_delay}min after dump (status={runner_status})'
                }
                extra_events.append((itkumuuri_ts, itkumuuri_event))

    # --- 5. Yhdistä ja järjestä ---
    if not login_only:
        combined = extra_events + [e for lst in published_by_runner.values() for e in lst]
    else:
        combined = extra_events[:]
    combined.sort(key=lambda x: event_sort_key(x[1]))

    # --- 6. Login queue simulation ---
    # Walk through login events chronologically and compute the actual
    # check-in time given a limited pool of devices and processing_seconds
    # per runner.  When a device breaks mid-check-in it is removed from the
    # pool; runners queued for it are redistributed to other devices.  The
    # device re-joins the pool after broken_reader_downtime_seconds.
    _proc_sec = _login_cfg.get('processing_seconds', 20)
    _broken_prob = _login_cfg.get('broken_reader_probability', 0.0)
    _broken_extra = _login_cfg.get('broken_reader_extra_delay_seconds', 60)
    _broken_downtime = _login_cfg.get('broken_reader_downtime_seconds', 300)
    _dev_count = _login_cfg.get('device_count', 10)
    _login_devices = [f"login_{i}" for i in range(1, _dev_count + 1)]

    _FAR_FUTURE = datetime.max.replace(tzinfo=timezone.utc)

    if _proc_sec > 0 or _broken_prob > 0:
        _busy_until: Dict[str, datetime] = {}
        _available_devices = list(_login_devices)
        _broken_devices: Dict[str, datetime] = {}

        for ts, ev in combined:
            if ev.get('event') != 'login':
                continue

            # Re-add devices whose downtime has elapsed
            for dev_id, fail_ts in list(_broken_devices.items()):
                if ts >= fail_ts + timedelta(seconds=_broken_downtime):
                    _available_devices.append(dev_id)
                    del _broken_devices[dev_id]
                    _busy_until[dev_id] = ts  # reset so next min() picks real time

            # Pick earliest-free among available devices
            if _available_devices:
                dev = min(_available_devices,
                          key=lambda d: _busy_until.get(d, ts))
                actual_start = max(ts, _busy_until.get(dev, ts))
            else:
                dev = _login_devices[0]
                actual_start = ts

            charge = timedelta(seconds=_proc_sec)
            note_parts = []
            redirected = False

            if _broken_prob > 0 and random.random() < _broken_prob:
                # Device breaks — remove from pool
                _broken_devices[dev] = actual_start
                _busy_until[dev] = _FAR_FUTURE
                try:
                    _available_devices.remove(dev)
                except ValueError:
                    pass
                note_parts.append(f'{dev} BROKEN')

                # Redirect this runner to an alternative device
                extra_sec = random.uniform(1, _broken_extra)
                charge += timedelta(seconds=extra_sec)
                note_parts.append(f'+{extra_sec:.0f}s reroute')

                if _available_devices:
                    alt_dev = min(
                        _available_devices,
                        key=lambda d: _busy_until.get(d, ts))
                    alt_start = max(ts, _busy_until.get(alt_dev, ts))
                    dev = alt_dev
                    actual_start = alt_start
                    redirected = True
                    note_parts.append(f'→ {dev}')

            ev['device_id'] = dev
            checkout_ts = actual_start + charge
            _busy_until[dev] = checkout_ts
            ev['timestamp'] = checkout_ts.isoformat()

            queue_wait = (actual_start - ts).total_seconds()
            if queue_wait > 0:
                note_parts.append(f'queued {queue_wait:.0f}s')
            if note_parts:
                ev['note'] = ev.get('note', '') + ' | ' + ' '.join(note_parts)

        # Re-sort because login timestamps may have shifted
        combined.sort(key=lambda x: event_sort_key(x[1]))

    # --- 7. Shiftataan nykyhetkeen ---
    base_time = combined[0][0]
    now = datetime.now(timezone.utc)
    shift = now - base_time

    tasks = []
    for ts, ev in combined:
        delay = (ts - base_time).total_seconds() / speed

        async def schedule_and_send(delay_sec, event, original_ts):
            if event.get('event') == 'punch' and not control_allowed(event.get('device_id'), allowed_controls):
                return

            if finish_control and str(event.get("device_id")) == str(finish_control):
                display_id = "maali_1"
                event["device_type"] = "finish"
            else:
                display_id = event.get('device_id') or f"dev_{event.get('device_type')}"

            await asyncio.sleep(max(0.0, delay_sec))

            sent_ts = (original_ts + shift).isoformat()
            msg_obj = {
                'device_id': display_id,
                'device_type': event.get('device_type'),
                'runner_id': event.get('runner_id'),
                'event': event.get('event'),
                'timestamp': sent_ts
            }

            if event.get('event') == 'login':
                msg_obj.update({'login_time': sent_ts, 'note': event.get('note')})
            elif event.get('event') == 'results_purku':
                shifted_punches = []
                for p in event.get('punches', []):
                    try:
                        orig_p_dt = datetime.fromisoformat(p['time'])
                        shifted_p = (orig_p_dt + shift).isoformat()
                    except Exception:
                        shifted_p = p.get('time')
                    shifted_punches.append({**p, 'time': shifted_p})
                msg_obj.update({'purku_time': sent_ts, 'punches': shifted_punches, 'note': event.get('note')})
            elif event.get('event') == 'itkumuuri':
                msg_obj.update({'status': event.get('status'), 'note': event.get('note')})

            if not no_ws:
                key = display_id if one_conn_per_device else f"{display_id}_{int(datetime.now().timestamp()*1000)%1000000}"
                if key not in device_clients:
                    device_clients[key] = DeviceClient(key, host, port)
                    await device_clients[key].connect()
                await device_clients[key].send(make_message(msg_obj))

            if navisport_sender:
                await navisport_sender.on_event(event, sent_ts, msg_obj)

        tasks.append(asyncio.create_task(schedule_and_send(delay, ev, ts)))

    # --- 7. Odota kaikki ja sulje ---
    await asyncio.gather(*tasks)
    await asyncio.gather(*(c.close() for c in device_clients.values()))
    if navisport_sender:
        await navisport_sender.close()


# --- CLI ---

def main():
    p = argparse.ArgumentParser(description="relay IOF3.xml -> relayreplay for simulating various aspects")
    p.add_argument('-i', '--iof', required=True, help='path to iof3.xml')
    p.add_argument('-H', '--host', default='127.0.0.1', help='server host')
    p.add_argument('-P', '--port', type=int, default=8080, help='server port')
    p.add_argument('-f', '--controls-file', help='Path to file with allowed control codes, one per line')
    p.add_argument('-u', '--controls-url', help='URL returning JSON array of allowed control codes')
    p.add_argument('-s', '--speed', type=float, default=1.0, help='1.0 realtime, 2.0 twice as fast')
    p.add_argument('-o', '--one-conn-per-device', action='store_true', default=True,
                   help='If set, use one TCP connection per device id (default: create unique client per event)')
    p.add_argument('-t', '--start-offset', type=float, default=0.0,
                   help='Start offset in hours to skip from beginning of simulation (default 0)')
    p.add_argument('-r', '--team-range', help='Bib numbers to simulate, e.g., "1,3,5,14-55"')
    p.add_argument('--limit-teams', type=int, default=None,
                   help='Only process the first N teams (XML order, after --team-range filter)')
    p.add_argument('-m','--finish-control', type=str, help='Control code for finish punch, will be renamed to maali_1')
    p.add_argument('--config', type=str, default=DEFAULT_CONFIG_PATH,
                   help=f'Config file path (default: {DEFAULT_CONFIG_PATH})')
    p.add_argument('-l','--login-devices', type=int, default=None,
                   help='Number of login devices (overrides config login.device_count)')
    p.add_argument('-d','--purku-devices', type=int, default=5, help='Number of purku devices (default 5)')
    p.add_argument('-k','--itkumuuri-devices', type=int, default=3, help='Number of itkumuuri devices (default 3)')
    p.add_argument('--login-only', action='store_true',
                   help='Only simulate login/check-in events (skip punches, purku, itkumuuri)')
    p.add_argument('--mass-starts', type=str, default=None,
                   help='Comma-separated ISO timestamps for mass starts, e.g. "2025-06-14T23:00:00+03:00,2025-06-15T09:30:00+03:00"')
    p.add_argument('--mass-start-time', type=str, default=None,
                   help='Race start signal time (ISO). Defaults to earliest StartTime in XML.')
    p.add_argument('--race', type=str, default=None, choices=['venla', 'jukola', 'auto'],
                   help='Race type for check-in staging (default: auto-detect from XML <Event><Name>)')
    p.add_argument('--navisport', type=str, default=None,
                   help='Navisport local server URL, e.g. "http://127.0.0.1" — enables live passing feed')
    p.add_argument('--navisport-event-id', type=str, default=None,
                   help='Navisport event UUID (required when --navisport is set)')
    p.add_argument('--navisport-chip-base', type=int, default=0,
                   help='Base value for auto-generated chip numbers (chip = base + bib*1000 + leg). '
                        'Use to avoid conflicts with pre-registered chips.')
    p.add_argument('--no-ws', action='store_true', default=False,
                   help='Skip WebSocket DeviceClient connections (use when running Navisport-only, '
                        'without a listener.py relay display server)')
    p.add_argument('--debug-navisport', action='store_true', default=False,
                   help='Show each Navisport payload and ask for confirmation before sending. '
                        'Press y=send, n=skip, a=send all remaining, q=quit')

    args = p.parse_args()

    # --- Load config (can be overridden by CLI flags below) ---
    login_config = load_config(args.config)
    if args.login_devices is not None:
        login_config['device_count'] = args.login_devices

    # --- Create device lists ---
    global PURKU_DEVICES, ITKUMUURI_DEVICES
    PURKU_DEVICES     = [f"purku_{i}" for i in range(1, args.purku_devices + 1)]
    ITKUMUURI_DEVICES = [f"itkumuuri_{i}" for i in range(1, args.itkumuuri_devices + 1)]

    mass_start_times = []
    if args.mass_starts:
        for s in args.mass_starts.split(','):
            s = s.strip()
            if s:
                try:
                    mass_start_times.append(datetime.fromisoformat(s))
                except ValueError as e:
                    print(f"Invalid mass start timestamp '{s}': {e}")
                    return

    # Muodosta set bib-numeroista
    team_range = None
    if args.team_range:
        try:
            team_range = parse_team_range(args.team_range)
        except ValueError as e:
            print(e)
            return

    events = parse_iof3_events(args.iof, team_range=team_range,
                               team_limit=args.limit_teams)
    ws_info = "disabled (--no-ws)" if args.no_ws else f"{args.host}:{args.port}"
    print(f"Parsed {len(events)} events. Speed={args.speed} WS={ws_info}")

    # Detect race & mass start time
    race = args.race or detect_race_from_xml(args.iof)
    mass_start_signal = parse_mass_start_time(args.iof)
    if args.mass_start_time:
        try:
            mass_start_signal = datetime.fromisoformat(args.mass_start_time)
        except ValueError as e:
            print(f"Invalid --mass-start-time '{args.mass_start_time}': {e}")
            return
    print(f"Race: {race}, mass start signal: {mass_start_signal.isoformat()}")

    # Build bib map (runner_id → bib) from punch events
    bib_map = {}
    for ev in events:
        rid = ev.get('runner_id')
        bid = ev.get('team_id')
        if rid and bid and rid not in bib_map:
            try:
                bib_map[rid] = int(bid)
            except (ValueError, TypeError):
                pass
    print(f"Bib map: {len(bib_map)} runners")

    allowed_controls = asyncio.run(load_allowed_controls(args.controls_file, args.controls_url))
    if allowed_controls:
        print(f"Loaded {len(allowed_controls)} allowed controls")
    else:
        print("No controls list provided or failed to load — publishing ALL punches")

    navisport_sender = None
    if args.navisport:
        if not args.navisport_event_id:
            print("Error: --navisport-event-id is required when --navisport is set")
            return
        if NavisportConnector is None:
            print("Error: navisport_register.py not found — cannot use --navisport")
            return
        navisport_sender = NavisportSender(args.navisport, args.navisport_event_id,
                                           chip_base=args.navisport_chip_base,
                                           debug=args.debug_navisport)

    asyncio.run(run_simulator(events, args.host, args.port,
                              args.speed, args.one_conn_per_device,
                              allowed_controls, args.start_offset, args.finish_control,
                              mass_start_times=mass_start_times,
                              navisport_sender=navisport_sender,
                              race=race, bib_map=bib_map,
                              mass_start_signal=mass_start_signal,
                              login_config=login_config,
                              login_only=args.login_only,
                              no_ws=args.no_ws))

if __name__ == '__main__':
    main()
