#!/usr/bin/env python3
# simulator.py
import argparse
import asyncio
import xml.etree.ElementTree as ET
import json
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Tuple

# --- Configurable message format helpers ---
def make_message(ev: Dict[str, Any]) -> str:
    # yksi JSON-rivi per tapahtuma
    return json.dumps(ev, separators=(',', ':')) + "\n"

# --- IOF XML parsing (simple, adapt to your IOF flavour) ---
    tree = ET.parse(iof_path)
    root = tree.getroot()
    ns = {'iof': root.tag.split('}')[0].strip('{')} if '}' in root.tag else {}
    events = []
    # Tämä malli olettaa, että iof-tiedosto sisältää Entry/Checkpoint-tyyppisiä tietoja
    # Muokkaa tarvittaessa, jos tiedoston rakenne on erilainen.
    for person in root.findall('.//person', ns) or root.findall('.//Person', ns):
        person_id = person.get('id') or person.findtext('id') or person.findtext('PersonID') or None
        # etsi kaikki leimat (siis checkpoints / punches)
        for result in person.findall('.//result', ns) or person.findall('.//Result', ns):
            for lap in result.findall('.//lap', ns) or result.findall('.//Lap', ns):
                # etsi punch / controlcode ja time
                time_text = lap.findtext('time') or lap.findtext('Time') or lap.findtext('timestamp')
                control = lap.findtext('controlcode') or lap.findtext('ControlCode') or lap.findtext('control')
                status = lap.findtext('status') or lap.findtext('Status') or None
                if not time_text:
                    continue
                # oletetaan iso muoto ISO8601 tai HH:MM:SS, yritetään parse
                ts = try_parse_time(time_text)
                events.append({
                    'timestamp': ts.isoformat(),
                    'runner_id': person_id,
                    'device_id': control,
                    'device_type': guess_device_type(control, status),
                    'status': status,
                    'raw_time': time_text,
                    'event': 'punch'
                })
        # sisäänkirjautuminen ja tulosten purku voidaan etsiä erillisistä tageista
        # Erittäin riippuvainen tiedoston rakenteesta: muokkaa tarvittaessa.
    # sort by timestamp
    events.sort(key=lambda e: e['timestamp'])
    return events
def parse_iof3_events(iof_path: str) -> List[Dict[str, Any]]:
    tree = ET.parse(iof_path)
    root = tree.getroot()
    ns = {'iof': root.tag.split('}')[0].strip('{')} if '}' in root.tag else {}

    events = []

    for team in root.findall('.//iof:TeamResult', ns):
        team_id = team.findtext('iof:BibNumber', namespaces=ns)

        for member in team.findall('.//iof:TeamMemberResult', ns):
            person_el = member.find('iof:Person/iof:Name', ns)
            runner_name = None
            if person_el is not None:
                given = person_el.findtext('iof:Given', namespaces=ns) or ""
                family = person_el.findtext('iof:Family', namespaces=ns) or ""
                runner_name = f"{given} {family}".strip()

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

                # tulkitaan Time: joko kellonaika (HH:MM:SS) tai offset
                ts = None
                if ":" in time_txt and len(time_txt.split(":")) == 3:
                    # Kellonaika muodossa HH:MM:SS
                    hh, mm, ss = map(int, time_txt.split(":"))
                    ts = start_dt.replace(hour=hh, minute=mm, second=ss) if start_dt else None
                else:
                    # Offset sekunteina lähdöstä
                    offset = int(time_txt)
                    ts = start_dt + timedelta(seconds=offset) if start_dt else None

                if ts:
                    events.append({
                        'timestamp': ts.isoformat(),
                        'runner_id': team_id,
                        'runner_name': runner_name,
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
        except Exception as e:
            print(f"[{self.device_id}] connect error: {e}")
            self.writer = None

    async def send(self, message: str):
        if not self.writer:
            await self.connect()
            if not self.writer:
                print(f"[{self.device_id}] cannot send, no connection")
                return
        try:
            self.writer.write(message.encode('utf-8'))
            await self.writer.drain()
        except Exception as e:
            print(f"[{self.device_id}] send error: {e}")
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
            except Exception:
                pass
            self.writer = None

async def run_simulator(events: List[Dict[str,Any]], host: str, port: int, speed: float, one_conn_per_device: bool):
    # Map devices to clients
    device_clients: Dict[str, DeviceClient] = {}
    # Build timeline: convert timestamps to datetime
    timeline: List[Tuple[datetime, Dict[str,Any]]] = []
    for ev in events:
        ts = datetime.fromisoformat(ev['timestamp'])
        timeline.append((ts, ev))
    if not timeline:
        print("No events found.")
        return
    # base = first event time
    base_time = timeline[0][0]
    sim_start = datetime.now(timezone.utc)
    tasks = []
    for ts, ev in timeline:
        # compute simulated delay from now: (ts - base_time)/speed
        delta = (ts - base_time).total_seconds() / speed
        # schedule
        async def schedule_and_send(delay, event):
            await asyncio.sleep(max(0.0, delay))
            device_id = event.get('device_id') or f"dev_{event.get('device_type')}"
            # optionally namespace same device ids to create multiple clients if needed
            if one_conn_per_device:
                key = device_id
            else:
                # create unique per event client to simulate many devices
                key = f"{device_id}_{int(event['timestamp'].split(':')[-1])}"
            if key not in device_clients:
                device_clients[key] = DeviceClient(key, host, port)
            msg_obj = {
                'device_id': key,
                'device_type': event.get('device_type'),
                'runner_id': event.get('runner_id'),
                'event': event.get('event'),
                'raw_control': event.get('device_id'),
                'status': event.get('status'),
                'timestamp': event.get('timestamp')
            }
            msg = make_message(msg_obj)
            await device_clients[key].send(msg)
        tasks.append(asyncio.create_task(schedule_and_send(delta, ev)))
    await asyncio.gather(*tasks)
    # close clients
    await asyncio.gather(*(c.close() for c in device_clients.values()))

# --- CLI ---
def main():
    p = argparse.ArgumentParser(description="IOF3 -> TCP simulator")
    p.add_argument('--iof', required=True, help='path to iof3.xml')
    p.add_argument('--host', default='127.0.0.1')
    p.add_argument('--port', type=int, default=9000)
    p.add_argument('--speed', type=float, default=1.0, help='1.0 realtime, 2.0 twice as fast')
    p.add_argument('--one-conn-per-device', action='store_true',
                   help='If set, use one TCP connection per device id (default: create unique client per event)')
    args = p.parse_args()

    events = parse_iof3_events(args.iof)
    print(f"Parsed {len(events)} events. Speed={args.speed} Host={args.host}:{args.port}")
    asyncio.run(run_simulator(events, args.host, args.port, args.speed, args.one_conn_per_device))

if __name__ == '__main__':
    main()

