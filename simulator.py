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
    from navisport_register import NavisportConnector, now_iso as _navi_now_iso, build_chip_result as _build_chip_result
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

    def __init__(self, host: str, event_id: str):
        self.host = host
        self.event_id = event_id
        self._conn: Optional[Any] = None
        self._cp_by_code: Dict[str, dict] = {}   # control code → checkpoint
        self._start_times: Dict[str, str] = {}   # runner_id → ISO start timestamp

    async def connect(self):
        loop = asyncio.get_event_loop()
        conn = NavisportConnector(host=self.host)
        await loop.run_in_executor(None, conn.connect)
        self._conn = conn
        cps = await loop.run_in_executor(None, conn.get_checkpoints, self.event_id)
        self._cp_by_code = {cp['code']: cp for cp in (cps or []) if cp.get('code')}
        print(f"[navisport] Connected to {self.host}, event {self.event_id}, "
              f"{len(self._cp_by_code)} checkpoints cached")

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

    def _sync_send_login(self, runner_id: str, timestamp: str):
        """Set runner status to Competing when chip is logged in."""
        if not self._conn:
            return
        event = self._conn.get_event(self.event_id)
        if not event:
            return
        chip_str = str(runner_id)
        result = next(
            (r for r in event.get('results', [])
             if str(r.get('chip', '')) == chip_str and r.get('status') == 'Registered'),
            None,
        )
        if result:
            self._conn.send_result({
                **result,
                'status': 'Competing',
                'registerTime': timestamp,
                'updated': _navi_now_iso(),
            }, self.event_id)

    def _sync_send_punch(self, runner_id: str, code: str, timestamp: str, device_type: str):
        if not self._conn:
            return
        cp_id, dev_id = self._checkpoint_for(code)
        elapsed = self._compute_elapsed(runner_id, timestamp)
        passing = {
            'id': str(uuid.uuid4()),
            'eventId': self.event_id,
            'chip': str(runner_id),
            'deviceId': dev_id,
            'timestamp': timestamp,
            'checkpointId': cp_id,
        }
        if elapsed is not None:
            passing['time'] = elapsed
        self._conn.send_passing(passing)

        is_finish = device_type == 'finish' or str(code).lower() in ('maali', 'finish', 'f')
        if is_finish:
            self._sync_finish_result(runner_id, timestamp, elapsed)

    def _sync_finish_result(self, runner_id: str, timestamp: str, elapsed: Optional[int]):
        """Send Result/Update with finishTime for the finishing runner."""
        event = self._conn.get_event(self.event_id)
        if not event:
            return
        chip_str = str(runner_id)
        candidates = [
            r for r in event.get('results', [])
            if str(r.get('chip', '')) == chip_str or str(r.get('secondaryChip', '')) == chip_str
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
        self._conn.send_result(finish_result, self.event_id)

    # ------------------------------------------------------------------
    # Async entry points called from schedule_and_send
    # ------------------------------------------------------------------

    def _sync_send_purku(self, runner_id: str, punches: list, purku_ts: str):
        """Send a full chip card read (all punches) to Navisport via Result/Update."""
        if not self._conn:
            return
        event_data = self._conn.get_event(self.event_id)
        if not event_data:
            print(f"[navisport] purku: event {self.event_id} not found")
            return
        chip_str = str(runner_id)
        result = next(
            (r for r in event_data.get('results', [])
             if str(r.get('chip', '')) == chip_str or str(r.get('secondaryChip', '')) == chip_str),
            None,
        )
        if not result:
            print(f"[navisport] purku: no result found for chip {chip_str}")
            return

        start_time_str = self._start_times.get(chip_str) or result.get('startTime')
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
            print(f"[navisport] purku: no valid punches for chip {chip_str}")
            return

        # Determine status from result or default to Ok
        raw_status = result.get('status', 'Ok')
        chip_result = _build_chip_result(
            result_id=result['id'],
            event_id=self.event_id,
            controls=controls,
            start_time=start_time_str,
            status=raw_status,
        )
        chip_result['readTime'] = purku_ts
        self._conn.send_result(chip_result, self.event_id)
        print(f"[navisport] purku: sent {len(controls)} punches for chip {chip_str}")

    async def on_event(self, event: Dict[str, Any], sent_ts: str, msg_obj: Optional[Dict[str, Any]] = None):
        if not self._conn:
            return
        loop = asyncio.get_event_loop()
        etype = event.get('event')
        runner_id = str(event.get('runner_id', ''))

        if etype == 'login':
            # Track simulated start time; leg 2+ start time will be set by
            # Navisport changeover logic once the previous leg finishes.
            self._start_times[runner_id] = sent_ts
            await loop.run_in_executor(None, self._sync_send_login, runner_id, sent_ts)

        elif etype == 'punch':
            code = str(event.get('device_id', ''))
            dtype = event.get('device_type', '')
            await loop.run_in_executor(None, self._sync_send_punch, runner_id, code, sent_ts, dtype)

        elif etype == 'results_purku':
            # Use shifted punches from msg_obj (timestamps already adjusted for sim speed)
            punches = (msg_obj or {}).get('punches', event.get('punches', []))
            await loop.run_in_executor(None, self._sync_send_purku, runner_id, punches, sent_ts)

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
LOGIN_DEVICES     = [f"login_{i}" for i in range(1, 11)]    # 10 logins
PURKU_DEVICES      = [f"purku_{i}" for i in range(1, 6)]      # 5 tulosten purku
ITKUMUURI_DEVICES = [f"itkumuuri_{i}" for i in range(1, 4)] # 3 itkumuuri



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
def parse_iof3_events(iof_path: str, team_range: Optional[set[int]] = None) -> List[Dict[str, Any]]:
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
                        'team_id': team_bib_text,
                        'device_id': code,
                        'device_type': guess_device_type(code, None),
                        'raw_time': time_txt,
                        'status': status_txt or 'OK',
                        'event': 'punch'
                    })

    events.sort(key=lambda e: e['timestamp'])
    print(f"Haettu joukkueita XML:stä: {all_teams_count}")
    print(f"Rangen mukaisia joukkueita: {included_teams_count}")
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
                        navisport_sender: Optional['NavisportSender'] = None):

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

    # login, purku, itkumuuri
    for runner, punches in all_by_runner.items():
        if not punches:
            continue
        evs_sorted = sorted(punches, key=lambda x: x[0])
        first_ts = evs_sorted[0][0]
        last_ts = evs_sorted[-1][0]

        # login
        minutes_before = random.randint(14, 60)
        login_ts = first_ts - timedelta(minutes=minutes_before)
        login_event = {
            'timestamp': login_ts.isoformat(),
            'runner_id': runner,
            'device_id': random.choice(LOGIN_DEVICES),
            'device_type': 'login',
            'event': 'login',
            'status': 'ok',
            'note': f'login {minutes_before}min before first punch'
        }
        extra_events.append((login_ts, login_event))

        # dump
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

        # itkumuuri
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
    combined = extra_events + [e for lst in published_by_runner.values() for e in lst]
    combined.sort(key=lambda x: event_sort_key(x[1]))

    # --- 6. Shiftataan nykyhetkeen ---
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

            key = display_id if one_conn_per_device else f"{display_id}_{int(datetime.now().timestamp()*1000)%1000000}"
            if key not in device_clients:
                device_clients[key] = DeviceClient(key, host, port)
                await device_clients[key].connect()

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
    p.add_argument('-m','--finish-control', type=str, help='Control code for finish punch, will be renamed to maali_1')
    p.add_argument('-l','--login-devices', type=int, default=10, help='Number of login devices (default 10)')
    p.add_argument('-d','--purku-devices', type=int, default=5, help='Number of purku devices (default 5)')
    p.add_argument('-k','--itkumuuri-devices', type=int, default=3, help='Number of itkumuuri devices (default 3)')
    p.add_argument('--mass-starts', type=str, default=None,
                   help='Comma-separated ISO timestamps for mass starts, e.g. "2025-06-14T23:00:00+03:00,2025-06-15T09:30:00+03:00"')
    p.add_argument('--navisport', type=str, default=None,
                   help='Navisport local server URL, e.g. "http://127.0.0.1" — enables live passing feed')
    p.add_argument('--navisport-event-id', type=str, default=None,
                   help='Navisport event UUID (required when --navisport is set)')

    args = p.parse_args()
    # --- Create device lists ---
    global LOGIN_DEVICES, PURKU_DEVICES, ITKUMUURI_DEVICES
    LOGIN_DEVICES     = [f"login_{i}" for i in range(1, args.login_devices + 1)]
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

    events = parse_iof3_events(args.iof, team_range=team_range)
    print(f"Parsed {len(events)} events. Speed={args.speed} Host={args.host}:{args.port}")

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
        navisport_sender = NavisportSender(args.navisport, args.navisport_event_id)
        asyncio.run(navisport_sender.connect())

    asyncio.run(run_simulator(events, args.host, args.port,
                              args.speed, args.one_conn_per_device,
                              allowed_controls, args.start_offset, args.finish_control,
                              mass_start_times=mass_start_times,
                              navisport_sender=navisport_sender))

if __name__ == '__main__':
    main()
