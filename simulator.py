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

from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Tuple

# --- Predefined login devices ---
LOGIN_DEVICES     = [f"login_{i}" for i in range(1, 11)]    # 10 logins
DUMP_DEVICES      = [f"dump_{i}" for i in range(1, 6)]      # 5 tulosten purku
ITKUMUURI_DEVICES = [f"itkumuuri_{i}" for i in range(1, 4)] # 3 itkumuuri



# --- Configurable message format helpers ---
def make_message(ev: Dict[str, Any]) -> str:
    # yksi JSON-rivi per tapahtuma
    return json.dumps(ev, separators=(',', ':')) + "\n"

# --- IOF XML parsing (simple, adapt to your IOF flavour) ---

def parse_iof3_events(iof_path: str) -> List[Dict[str, Any]]:
    tree = ET.parse(iof_path)
    root = tree.getroot()
    ns_uri = root.tag.split('}')[0].strip('{') if '}' in root.tag else None
    ns = {'iof': ns_uri} if ns_uri else None
    events = []

    for team in root.findall('.//iof:TeamResult', ns):
        team_bib = team.findtext('iof:BibNumber', namespaces=ns) or team.get('bib') or team.get('id') or None

        if ns:
            members = team.findall('.//iof:TeamMemberResult', ns)
        else:
            members = team.findall('.//TeamMemberResult')
        if not members:
            continue
        

        for idx, member in enumerate(members,start=1):
            # yritä löytää yksilöllinen henkilö-ID
            person_id = None
            person_el = member.find('iof:Person', ns)
            if person_el is not None:
                person_id = (
                    person_el.findtext('iof:PersonID', namespaces=ns) or
                    person_el.findtext('iof:Id', namespaces=ns) or
                    person_el.findtext('iof:ID', namespaces=ns)
                )
                name_el = person_el.find('iof:Name', ns)
                if name_el is not None:
                    given = name_el.findtext('iof:Given', namespaces=ns) or ""
                    family = name_el.findtext('iof:Family', namespaces=ns) or ""
                    runner_name = f"{given} {family}".strip()
                else:
                    runner_name = None
            else:
                person_id = member.get('id') or member.get('MemberID') or None
                runner_name = None

            if not person_id:
                person_id = f"{team_bib or 'team'}:{idx}"

            result = member.find('iof:Result', ns)
            if result is None:
                continue

            if ns:
                start_time_txt = result.findtext('iof:StartTime', namespaces=ns)
            else:
                start_time_txt = result.findtext('StartTime')

            # START: korjattu start_dt tz-aware ja päivämäärä huomioiden
            start_dt = None
            if start_time_txt:
                start_dt = try_parse_time(start_time_txt)
            if start_dt and start_dt.tzinfo is None:
                start_dt = start_dt.replace(tzinfo=timezone.utc)

            # END

            status_txt = result.findtext('iof:Status', namespaces=ns)

            for split in result.findall('iof:SplitTime', ns):
                code = split.findtext('iof:ControlCode', namespaces=ns)
                time_txt = split.findtext('iof:Time', namespaces=ns)
                if not time_txt or not code:
                    continue

                ts = None
                # Käytä aina StartTime + offset
                if start_dt:
                    ts = None
                    try:
                        # offset sekunneissa
                        offset = int(time_txt)
                        ts = start_dt + timedelta(seconds=offset)
                    except ValueError:
                        # jos hh:mm:ss-formaatti
                        try:
                            hh, mm, ss = map(int, time_txt.split(":"))
                            # laske seconds offset suhteessa start_dt:n kellonaikaan
                            delta_sec = (hh*3600 + mm*60 + ss) - (start_dt.hour*3600 + start_dt.minute*60 + start_dt.second)
                            ts = start_dt + timedelta(seconds=delta_sec)
                        except Exception:
                            ts = start_dt


                if ts:
                    events.append({
                        'timestamp': ts.isoformat(),
                        'runner_id': person_id,
                        'runner_name': runner_name,
                        'team_id': team_bib,
                        'device_id': code,
                        'device_type': guess_device_type(code, None),
                        'raw_time': time_txt,
                        'status': status_txt or 'OK',
                        'event': 'punch'
                    })

    events.sort(key=lambda e: e['timestamp'])
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
    # Yksinkertainen heuristiikka; muokkaa haluamaksesi
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
class DeviceClient:
    def __init__(self, device_id: str, host: str, port: int):
        self.device_id = device_id
        self.host = host
        self.port = port
        self.ws = None

    async def connect(self):
        try:
            self.ws = await websockets.connect(f"ws://{self.host}:{self.port}/sim")
            print(f"[{self.device_id}] c.")
        except Exception as e:
            print(f"[{self.device_id}] connect error: {e}")
            self.ws = None

    async def send(self, message: str):
        #print(f"[{self.device_id}] sending: {message.strip()}")
        # strip out extra data
        print(f"[{self.device_id}] s.", end='')
        if not self.ws:
            await self.connect()
            if not self.ws:
                return
        try:
            await self.ws.send(message)
        except Exception as e:
            print(f"[{self.device_id}] send error: {e}")
            try:
                await self.ws.close()
            except Exception:
                pass
            self.ws = None

    async def close(self):
        if self.ws:
            try:
                await self.ws.close()
                print(f"[{self.device_id}] connection closed")
            except Exception:
                pass
            self.ws = None

async def load_allowed_controls(file_path: str = None, url: str = None) -> set:
    controls = set()

    # Lue paikallinen tiedosto (JSON-lista odotettuna)
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

async def run_simulator(events: List[Dict[str,Any]],
                        host: str,
                        port: int,
                        speed: float,
                        one_conn_per_device: bool,
                        allowed_controls:set):

    device_clients: Dict[str, DeviceClient] = {}
    all_timeline: List[Tuple[datetime, Dict[str,Any]]] = []

    # prepare timeline
    for ev in events:
        ts = datetime.fromisoformat(ev['timestamp'])
        all_timeline.append((ts, ev))
    if not all_timeline:
        print("No events found.")
        return

    # organize punches per runner (kaikki punchit mukaan heti)
    all_by_runner: Dict[str, List[Tuple[datetime, Dict[str,Any]]]] = {}
    for ts, ev in all_timeline:
        if ev.get('event') != 'punch':
            continue
        runner = ev.get('runner_id') or ev.get('runner_name') or 'unknown'
        all_by_runner.setdefault(runner, []).append((ts, ev))

      #  print(f"Total runners before counter: {len(all_by_runner)}")

    # alusta published_by_runner kaikille heti, vaikka ei olisi yhtään allowed punchia
    published_by_runner: Dict[str, List[Tuple[datetime, Dict[str,Any]]]] = {}
    for runner, punches in all_by_runner.items():
        published_by_runner[runner] = list(punches)

    extra_events: List[Tuple[datetime, Dict[str,Any]]] = []
    start_strings = [
    "2025-06-14T23:00:00+03:00",
    "2025-06-15T09:30:00+03:00",
    "2025-06-15T09:45:00+03:00"
]
    extra_events = []

    for s in start_strings:
        ts = datetime.fromisoformat(s)          # timezone-aware datetime
        event = {
            'timestamp': ts.isoformat(),
            'runner_id': 'mass_start',
            'device_id': 'mass_start',
            'device_type': 'mass_start',
            'event': 'mass_start',
            'note': 'Mass start at known time'
        }
        extra_events.append((ts, event))
        print(f"Mass startline event added at {ts.isoformat()}")

    # --- extra events: login, dump, itkumuuri ---
    #extra_events: List[Tuple[datetime, Dict[str,Any]]] = []
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
            'raw_time': None,
            'status': 'ok',
            'note': f'login {minutes_before}min before first punch'
        }
        extra_events.append((login_ts, login_event))

        # dump
        minutes_after = random.randint(10, 15)
        dump_ts = last_ts + timedelta(minutes=minutes_after)
        punches_dump = []
        for t, e in evs_sorted:
            punches_dump.append({
                'control': e.get('device_id'),
                'time': e.get('timestamp'),
                'status': e.get('status'),
                'device_type': e.get('device_type')
            })
        dump_event = {
            'timestamp': dump_ts.isoformat(),
            'runner_id': runner,
            'device_id': random.choice(DUMP_DEVICES),
            'device_type': 'results_dump',
            'event': 'results_dump',
            'dump_time': dump_ts.isoformat(),
            'punches': punches_dump,
            'note': f'dump {minutes_after}min after last punch'
        }
        extra_events.append((dump_ts, dump_event))

        # itkumuuri jos status ei ok
        runner_status = None
        for _, e in evs_sorted:
            if e.get('status'):
                runner_status = e['status']
                break
        if runner_status and runner_status not in ("OK", "Finished"):
            itkumuuri_delay = random.randint(5, 60)
            itkumuuri_ts = dump_ts + timedelta(minutes=itkumuuri_delay)
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

    # yhdistetään kaikki eventit
    combined = extra_events + [e for lst in published_by_runner.values() for e in lst]
    combined.sort(key=lambda x: x[0])

    # shiftataan nykyhetkeen
    base_time = combined[0][0]
    now = datetime.now(timezone.utc)
    shift = now - base_time

    tasks = []
    for ts, ev in combined:
        delay = (ts - base_time).total_seconds() / speed
        shifted_ts = (ts + shift).astimezone()
        short_runner = ev.get('runner_id') or '-'
        short_control = ev.get('device_id') or ev.get('device_type') or '-'
        ev_type = ev.get('event') or '-'
        print(f"{shifted_ts.strftime('%Y-%m-%d %H:%M:%S')} | +{delay:5.1f}s | {ev_type} | r={short_runner} c={short_control}")

        async def schedule_and_send(delay_sec, event, original_ts):
            # allowed_controls-suodatus vasta tässä
            if event.get('event') == 'punch' and not control_allowed(event.get('device_id'), allowed_controls):
                return  # ohitetaan jos ei ole allowed

            await asyncio.sleep(max(0.0, delay_sec))
            device_id = event.get('device_id') or f"dev_{event.get('device_type')}"
            key = device_id if one_conn_per_device else f"{device_id}_{int(datetime.now(timezone.utc).timestamp()*1000)%1000000}"
            if key not in device_clients:
                device_clients[key] = DeviceClient(key, host, port)

            sent_ts = (original_ts + shift).isoformat()

            # build message
            msg_obj = {
                'device_id': key,
                'device_type': event.get('device_type'),
                'runner_id': event.get('runner_id'),
                'event': event.get('event'),
                'timestamp': sent_ts
            }
            if event.get('event') == 'login':
                msg_obj.update({'login_time': sent_ts, 'note': event.get('note')})
            elif event.get('event') == 'results_dump':
                shifted_punches = []
                for p in event.get('punches', []):
                    try:
                        orig_p_dt = datetime.fromisoformat(p['time'])
                        shifted_p = (orig_p_dt + shift).isoformat()
                    except Exception:
                        shifted_p = p.get('time')
                    shifted_punches.append({**p, 'time': shifted_p})
                msg_obj.update({'dump_time': sent_ts, 'punches': shifted_punches, 'note': event.get('note')})
            elif event.get('event') == 'itkumuuri':
                msg_obj.update({'status': event.get('status'), 'note': event.get('note')})

            msg = make_message(msg_obj)
            await device_clients[key].send(msg)

        tasks.append(asyncio.create_task(schedule_and_send(delay, ev, ts)))

    await asyncio.gather(*tasks)
    await asyncio.gather(*(c.close() for c in device_clients.values()))


# --- CLI ---

def main():
    p = argparse.ArgumentParser(description="IOF3 -> TCP simulator")
    p.add_argument('--iof', required=True, help='path to iof3.xml')
    p.add_argument('--host', default='127.0.0.1')
    p.add_argument('--port', type=int, default=8080)
    p.add_argument('--controls-file', help='Path to file with allowed control codes, one per line')
    p.add_argument('--controls-url', help='URL returning JSON array of allowed control codes')
    p.add_argument('--speed', type=float, default=1.0, help='1.0 realtime, 2.0 twice as fast')
    p.add_argument('--one-conn-per-device', action='store_true', help='If set, use one TCP connection per device id (default: create unique client per event)')
    p.add_argument('--start-offset', type=float, default=0.0, help='Start offset in hours to skip from beginning of simulation (default 0)')

    args = p.parse_args()

    events = parse_iof3_events(args.iof)
#    print(f"Parsed {len(events)} events. Speed={args.speed} Host={args.host}:{args.port}")

    # load allowed controls (async)
    allowed_controls = asyncio.run(load_allowed_controls(args.controls_file, args.controls_url))
    if allowed_controls:
        print(f"Loaded {len(allowed_controls)} allowed controls")
    else:
        print("No controls list provided or failed to load — publishing ALL punches")

    asyncio.run(run_simulator(events, args.host, args.port, args.speed, args.one_conn_per_device, allowed_controls))



if __name__ == '__main__':
    main()
