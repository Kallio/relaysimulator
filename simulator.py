#!/usr/bin/env python3
# simulator.py
import argparse
import asyncio
import xml.etree.ElementTree as ET
import json 
import random
import aiohttp
import os
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Tuple

# --- Configurable message format helpers ---
def make_message(ev: Dict[str, Any]) -> str:
    # yksi JSON-rivi per tapahtuma
    return json.dumps(ev, separators=(',', ':')) + "\n"

# --- IOF XML parsing (simple, adapt to your IOF flavour) ---
def parse_iof3_events(iof_path: str) -> List[Dict[str, Any]]:
    tree = ET.parse(iof_path)
    root = tree.getroot()
    ns = {'iof': root.tag.split('}')[0].strip('{')} if '}' in root.tag else {}

    events = []

    for team in root.findall('.//iof:TeamResult', ns):
        team_bib = team.findtext('iof:BibNumber', namespaces=ns) or team.get('bib') or team.get('id') or None

        members = team.findall('.//iof:TeamMemberResult', ns)
        if not members:
            continue

        for idx, member in enumerate(members):
            # yritä löytää yksilöllinen henkilö-ID
            person_id = None
            # common IOF paths
            person_el = member.find('iof:Person', ns)
            if person_el is not None:
                person_id = person_el.findtext('iof:PersonID', namespaces=ns) or person_el.findtext('iof:Id', namespaces=ns) or person_el.findtext('iof:ID', namespaces=ns)
                # jos nimi löytyy, käytä sitä myös runner_name
                name_el = person_el.find('iof:Name', ns)
                if name_el is not None:
                    given = name_el.findtext('iof:Given', namespaces=ns) or ""
                    family = name_el.findtext('iof:Family', namespaces=ns) or ""
                    runner_name = f"{given} {family}".strip()
                else:
                    runner_name = None
            else:
                # joskus TeamMemberResult:ssä voi olla attribuutteja
                person_id = member.get('id') or member.get('MemberID') or None
                runner_name = None

            # fallback: jos ei löydy, muodosta uniikki id team_bib:index
            if not person_id:
                person_id = f"{team_bib or 'team'}:{idx}"

            result = member.find('iof:Result', ns)
            if result is None:
                continue

            start_time_txt = result.findtext('iof:StartTime', namespaces=ns)
            start_dt = try_parse_time(start_time_txt) if start_time_txt else None

            for split in result.findall('iof:SplitTime', ns):
                code = split.findtext('iof:ControlCode', namespaces=ns)
                time_txt = split.findtext('iof:Time', namespaces=ns)
                if not time_txt or not code:
                    continue

                ts = None
                if ":" in time_txt and len(time_txt.split(":")) == 3:
                    hh, mm, ss = map(int, time_txt.split(":"))
                    ts = start_dt.replace(hour=hh, minute=mm, second=ss) if start_dt else None
                else:
                    offset = int(time_txt)
                    ts = start_dt + timedelta(seconds=offset) if start_dt else None

                if ts:
                    events.append({
                        'timestamp': ts.isoformat(),
                        'runner_id': person_id,
                        'runner_name': runner_name,
                        'team_id': team_bib,
                        'device_id': code,
                        'device_type': guess_device_type(code, None),
                        'raw_time': time_txt,
                        'event': 'punch'
                    })

    events.sort(key=lambda e: e['timestamp'])
    return events

def try_parse_time(t: str) -> datetime:
    # Yritetään useita formaatteja; lisää tarvittaessa
    fmts = ["%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%H:%M:%S"]
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
        self.writer = None

    async def connect(self):
        try:
            reader, writer = await asyncio.open_connection(self.host, self.port)
            self.writer = writer
            print(f"[{self.device_id}] connected to {self.host}:{self.port}")
        except Exception as e:
            print(f"[{self.device_id}] connect error: {e}")
            self.writer = None

    async def send(self, message: str):
        # Konsoliloki: näytä mitä lähetetään
        print(f"[{self.device_id}] sending: {message.strip()}")
        if not self.writer:
            await self.connect()
            if not self.writer:
          #      print(f"[{self.device_id}] cannot send, no connection")
                return
        try:
            self.writer.write(message.encode('utf-8'))
            await self.writer.drain()
           # print(f"[{self.device_id}] sent OK")
        except Exception as e:
           # print(f"[{self.device_id}] send error: {e}")
            try:
                self.writer.close()
            except Exception:
                pass
            self.writer = None

    async def close(self):
        if self.writer:
            try:
                self.writer.close()
                await self.writer.wait_closed()
         #       print(f"[{self.device_id}] connection closed")
            except Exception:
                pass
            self.writer = None
async def load_allowed_controls(file_path: str = None, url: str = None) -> set:
    controls = set()
    if file_path and os.path.exists(file_path):
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                for ln in f:
                    code = ln.strip()
                    if code:
                        controls.add(code.lower())
        except Exception as e:
            print(f"Warning: could not read controls file {file_path}: {e}")
    if url:
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.get(url, timeout=10) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if isinstance(data, list):
                            for item in data:
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

async def run_simulator(events: List[Dict[str,Any]], host: str, port: int, speed: float, one_conn_per_device: bool, allowed_controls:set):
    device_clients: Dict[str, DeviceClient] = {}
    all_timeline: List[Tuple[datetime, Dict[str,Any]]] = []
    for ev in events:
        ts = datetime.fromisoformat(ev['timestamp'])
        all_timeline.append((ts, ev))
    if not all_timeline:
        print("No events found.")
        return

    # Kaikki punchit per juoksija (dump)
    all_by_runner: Dict[str, List[Tuple[datetime, Dict[str,Any]]]] = {}
    for ts, ev in all_timeline:
        if ev.get('event') != 'punch':
            continue
        runner = ev.get('runner_id') or ev.get('runner_name') or 'unknown'
        all_by_runner.setdefault(runner, []).append((ts, ev))

    # Julkaistavat punchit per juoksija (suodatettu)
    published_by_runner: Dict[str, List[Tuple[datetime, Dict[str,Any]]]] = {}
    for ts, ev in all_timeline:
        if ev.get('event') != 'punch':
            continue
        if control_allowed(ev.get('device_id'), allowed_controls):
            runner = ev.get('runner_id') or ev.get('runner_name') or 'unknown'
            published_by_runner.setdefault(runner, []).append((ts, ev))

    # Jos allowed_controls annettu mutta ei yksikään punch osunut: fallback -> julkaise kaikki
    if allowed_controls and not any(published_by_runner.values()):
        print("No punches matched allowed controls — falling back to publishing ALL punches")
        published_by_runner = dict(all_by_runner)

    # Luo extra events: login perustuen published_by_runner (jos julkaistavia ei ole, käytä all_by_runner)
    extra_events: List[Tuple[datetime, Dict[str,Any]]] = []
    runners = set(all_by_runner.keys())
    for runner in runners:
        evs_for_login = published_by_runner.get(runner) or all_by_runner.get(runner) or []
        if not evs_for_login:
            continue
        evs_sorted = sorted(evs_for_login, key=lambda x: x[0])
        first_ts = evs_sorted[0][0]
        last_ts = evs_sorted[-1][0]

        minutes_before = random.randint(14, 60)
        login_ts = first_ts - timedelta(minutes=minutes_before)
        login_event = {
            'timestamp': login_ts.isoformat(),
            'runner_id': runner,
            'device_id': f"login_{runner}",
            'device_type': 'login',
            'event': 'login',
            'raw_time': None,
            'status': 'ok',
            'note': f'login {minutes_before}min before first punch'
        }
        extra_events.append((login_ts, login_event))

        minutes_after = random.randint(10, 15)
        dump_ts = last_ts + timedelta(minutes=minutes_after)

        # Käytä ALL punches (all_by_runner) dumpissa
        punches = []
        for t, e in sorted(all_by_runner.get(runner, []), key=lambda x: x[0]):
            punches.append({
                'control': e.get('device_id'),
                'time': e.get('timestamp'),
                'status': e.get('status'),
                'device_type': e.get('device_type')
            })

        dump_event = {
            'timestamp': dump_ts.isoformat(),
            'runner_id': runner,
            'device_id': f"dump_{runner}",
            'device_type': 'results_dump',
            'event': 'results_dump',
            'punches': punches,
            'note': f'dump {minutes_after}min after last punch'
        }
        extra_events.append((dump_ts, dump_event))

    # Published timeline: yhdistä published_by_runner tapahtumat (yksittäiset lähetykset)
    published_timeline: List[Tuple[datetime, Dict[str,Any]]] = []
    for evs in published_by_runner.values():
        published_timeline.extend(evs)

    combined = extra_events + published_timeline
    combined.sort(key=lambda x: x[0])

    # Shift so first event is now
    base_time = combined[0][0]
    now = datetime.now(timezone.utc)
    shift = now - base_time

    tasks = []
    for ts, ev in combined:
        shifted_ts = (ts + shift).astimezone()
        delay = (ts - base_time).total_seconds() / speed

        short_runner = ev.get('runner_id') or ev.get('runner_name') or '-'
        short_control = ev.get('device_id') or ev.get('raw_control') or ev.get('device_type') or '-'
        ev_type = ev.get('event') or ev.get('device_type') or '-'
        print(f"{shifted_ts.strftime('%Y-%m-%d %H:%M:%S')} | +{delay:5.1f}s | {ev_type} | r={short_runner} c={short_control}")

        async def schedule_and_send(delay_sec, event, original_ts):
            await asyncio.sleep(max(0.0, delay_sec))
            device_id = event.get('device_id') or f"dev_{event.get('device_type')}"
            if one_conn_per_device:
                key = device_id
            else:
                key = f"{device_id}_{int(datetime.now(timezone.utc).timestamp()*1000)%1000000}"
            if key not in device_clients:
                device_clients[key] = DeviceClient(key, host, port)

            sent_ts = (original_ts + shift).isoformat()
            if event.get('event') == 'login':
                msg_obj = {
                    'device_id': key,
                    'device_type': 'login',
                    'runner_id': event.get('runner_id'),
                    'event': 'login',
                    'login_time': sent_ts,
                    'note': event.get('note'),
                }
            elif event.get('event') == 'results_dump':
                shifted_punches = []
                for p in event.get('punches', []):
                    try:
                        orig_p_dt = datetime.fromisoformat(p['time'])
                        shifted_p = (orig_p_dt + shift).isoformat()
                    except Exception:
                        shifted_p = p.get('time')
                    shifted_punches.append({
                        'control': p.get('control'),
                        'time': shifted_p,
                        'status': p.get('status'),
                        'device_type': p.get('device_type')
                    })
                msg_obj = {
                    'device_id': key,
                    'device_type': 'results_dump',
                    'runner_id': event.get('runner_id'),
                    'event': 'results_dump',
                    'dump_time': sent_ts,
                    'punches': shifted_punches,
                    'note': event.get('note'),
                }
            else:
                msg_obj = {
                    'device_id': key,
                    'device_type': event.get('device_type'),
                    'runner_id': event.get('runner_id'),
                    'event': event.get('event'),
                    'raw_control': event.get('device_id'),
                    'status': event.get('status'),
                    'timestamp': sent_ts
                }

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
    p.add_argument('--port', type=int, default=9000)
    p.add_argument('--controls-file', help='Path to file with allowed control codes, one per line')
    p.add_argument('--controls-url', help='URL returning JSON array of allowed control codes')
    p.add_argument('--speed', type=float, default=1.0, help='1.0 realtime, 2.0 twice as fast')
    p.add_argument('--one-conn-per-device', action='store_true',
                   help='If set, use one TCP connection per device id (default: create unique client per event)')
    args = p.parse_args()

    events = parse_iof3_events(args.iof)
    print(f"Parsed {len(events)} events. Speed={args.speed} Host={args.host}:{args.port}")

    # load allowed controls (async)
    allowed_controls = asyncio.run(load_allowed_controls(args.controls_file, args.controls_url))
    if allowed_controls:
        print(f"Loaded {len(allowed_controls)} allowed controls")
    else:
        print("No controls list provided or failed to load — publishing ALL punches")

    asyncio.run(run_simulator(events, args.host, args.port, args.speed, args.one_conn_per_device, allowed_controls))

if __name__ == '__main__':
    main()
