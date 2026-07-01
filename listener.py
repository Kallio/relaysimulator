#!/usr/bin/env python3
"""
Combined WebSocket + Socket.IO server for simulator testing.

Serves two protocols on the same port:

  1. WebSocket at /sim   — receives JSON messages from simulator.py DeviceClient
                           (login events, punches, purkus).  Used with:
                             simulator.py -P <PORT> --speed 10 --login-only

  2. Socket.IO at /      — mimics the Navisport desktop app API
                           (Event/Select, Result/Update, Passing/Update).
                           Used with:
                             simulator.py --navisport http://127.0.0.1:<PORT>
                                          --navisport-event-id <uuid>

Usage:
  python3 listener.py [--port PORT]
"""
import argparse
import json
import signal
import uuid
from datetime import datetime, timezone

import socketio
from aiohttp import web

# ---------------------------------------------------------------------------
# Checkpoint data — from real Navisport desktop app list-checkpoints output
# ---------------------------------------------------------------------------
CHECKPOINTS = [
    {'code': '100', 'name': '100', 'type': 'Checkpoint',
     'id': '8a38a363-2b50-4001-b2c8-7176959f15fa',
     'devices': ['2a61b6d5-875b-4f25-be44-7c4ccc1bb765']},
    {'code': '133', 'name': '133', 'type': 'Checkpoint',
     'id': '398cd4d8-6582-41d7-9326-09c8b22a1a82',
     'devices': ['2a61b6d5-875b-4f25-be44-7c4ccc1bb765']},
    {'code': '266', 'name': '266', 'type': 'Checkpoint',
     'id': '7fb98933-d565-4daa-9bdb-09816386f8e7',
     'devices': ['a781b320-a6aa-44c5-97c7-915f8d94bd58']},
    {'code': '42',  'name': '42', 'type': 'Checkpoint',
     'id': '86ff53b5-973a-413e-ab2c-7ba1dccb4b97',
     'devices': ['2a61b6d5-875b-4f25-be44-7c4ccc1bb765']},
    {'code': '73',  'name': '73', 'type': 'Checkpoint',
     'id': '4fd1aa2c-667a-409e-8649-c4f88dc08832',
     'devices': ['2a61b6d5-875b-4f25-be44-7c4ccc1bb765']},
    {'code': '93',  'name': '93', 'type': 'Checkpoint',
     'id': '22cab0b9-da17-419d-9adb-877eec9de007',
     'devices': ['2a61b6d5-875b-4f25-be44-7c4ccc1bb765']},
    {'code': '300', 'name': '300', 'type': 'Finish',
     'id': '51d574ca-e311-438b-a04b-6a8d811796d1',
     'devices': []},
]

CP_NAME_BY_ID = {cp['id']: f"{cp.get('name','?')} ({cp.get('type','?')})" for cp in CHECKPOINTS}

# ---------------------------------------------------------------------------
# Global state (shared across both protocols)
# ---------------------------------------------------------------------------
results_store: list = []
passing_count = 0
ws_message_count = 0
_chips_seen: set = set()
_current_event_id: str = ''

# ---------------------------------------------------------------------------
# Socket.IO + aiohttp
# ---------------------------------------------------------------------------

sio = socketio.AsyncServer(async_mode='aiohttp', cors_allowed_origins='*')
app = web.Application()
sio.attach(app)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ensure_result(chip: str, event_id: str):
    """Create a minimal Individual result for *chip* if none exists."""
    global results_store, _chips_seen
    if chip in _chips_seen:
        return
    for r in results_store:
        if r.get('chip') == chip or r.get('secondaryChip') == chip:
            _chips_seen.add(chip)
            return
    result = {
        'id': str(uuid.uuid4()),
        'eventId': event_id,
        'chip': chip, 'name': chip, 'status': 'Registered',
        'resultType': 'Individual', 'leg': 1, 'registered': True,
        'updated': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.') + '000Z',
    }
    results_store.append(result)
    _chips_seen.add(chip)
    print(f"  [auto] Registered result for chip={chip}")


def _handle_result_update(payload: dict) -> dict:
    global results_store
    single = payload.get('result')
    batch = payload.get('results')

    if batch:
        added = updated = 0
        for new_r in batch:
            rid = new_r.get('id')
            for i, existing in enumerate(results_store):
                if existing.get('id') == rid:
                    results_store[i] = new_r
                    updated += 1
                    break
            else:
                results_store.append(new_r)
                added += 1
        print(f"  [navisport] Batch Result/Update: {added} added, {updated} updated "
              f"(total: {len(results_store)})")
        return {'status': 'ok'}

    if not single:
        return {'status': 'error', 'message': 'No result or results in payload'}

    for i, r in enumerate(results_store):
        if r.get('id') == single.get('id'):
            results_store[i] = single
            break
    else:
        results_store.append(single)

    name = single.get('name') or single.get('chip', '?')
    status = single.get('status', '?')
    chip = single.get('chip', '?')
    leg = single.get('leg', '?')
    rtype = single.get('resultType', '?')
    finish = single.get('finishTime', '')
    elapsed = single.get('time')
    ctimes = single.get('controlTimes', [])
    read_time = single.get('readTime', '')

    parts = [f"  [navisport] Result/Update: {name}",
             f"type={rtype} chip={chip} leg={leg} status={status}"]
    if elapsed is not None:
        parts.append(f"time={elapsed}s")
    if finish:
        parts.append(f"finish={finish}")
    if ctimes:
        parts.append(f"{len(ctimes)} controls")
    if read_time:
        parts.append(f"readTime={read_time}")
    print(" | ".join(parts))
    return {'status': 'ok'}


# ---------------------------------------------------------------------------
# WebSocket endpoint (/sim)
# ---------------------------------------------------------------------------

async def websocket_handler(request):
    global ws_message_count
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    print(f"[ws] Client connected: {request.remote}")

    async for msg in ws:
        if msg.type == web.WSMsgType.TEXT:
            try:
                data = json.loads(msg.data)
            except json.JSONDecodeError:
                print(f"[ws] Raw: {msg.data}")
                continue

            ws_message_count += 1
            ev = data.get('event', '?')
            rid = data.get('runner_id', '?')
            dev = data.get('device_id', '?')
            ts = data.get('timestamp', '?')
            note = data.get('note', '')
            print(f"[ws] #{ws_message_count} [{ev:15s}] runner={rid:25s}  "
                  f"device={dev:10s}  ts={ts}")
            if note:
                print(f"     note: {note}")
        elif msg.type == web.WSMsgType.ERROR:
            break

    print(f"[ws] Client disconnected: {request.remote}")
    return ws


app.router.add_get('/sim', websocket_handler)

# ---------------------------------------------------------------------------
# HTTP endpoint
# ---------------------------------------------------------------------------

async def health(request):
    return web.json_response({
        'status': 'ok', 'passings': passing_count,
        'results': len(results_store),
        'checkpoints': len(CHECKPOINTS),
        'ws_messages': ws_message_count,
    })

app.router.add_get('/health', health)


# ---------------------------------------------------------------------------
# Socket.IO event handlers
# ---------------------------------------------------------------------------

@sio.event
async def connect(sid, environ):
    print(f"[navisport] Client connected (sid={sid})")


@sio.event
async def disconnect(sid):
    print(f"[navisport] Client disconnected (sid={sid})")


@sio.on('message')
async def handle_message(sid, data):
    global passing_count, _current_event_id

    subject = data.get('subject', '?')
    operation = data.get('operation', '?')
    payload = data.get('payload', {})

    if subject == 'Event' and operation == 'Select':
        event_id = payload.get('eventId', '?')
        _current_event_id = event_id
        print(f"[navisport] Event/Select: eventId={event_id} "
              f"({len(results_store)} results, {len(CHECKPOINTS)} checkpoints)")
        return {
            'payload': {
                'event': {
                    'id': event_id,
                    'name': 'Simulated Event (listener)',
                    'checkpoints': CHECKPOINTS,
                    'results': list(results_store),
                },
            },
            'status': 'ok',
        }

    if subject == 'Event' and operation == 'List':
        print("[navisport] Event/List: no events stored")
        return {'payload': {'events': []}, 'status': 'ok'}

    if subject == 'Result' and operation == 'Update':
        return _handle_result_update(payload)

    if subject == 'Passing' and operation == 'Update':
        passing = payload.get('passing', {})
        passing_count += 1

        chip = str(passing.get('chip', '?'))
        cp_id = passing.get('checkpointId', '')
        dev_id = passing.get('deviceId', '')
        ts = passing.get('timestamp', '?')
        elapsed = passing.get('time')

        if _current_event_id:
            _ensure_result(chip, _current_event_id)

        cp_label = CP_NAME_BY_ID.get(cp_id, cp_id or '-')
        elapsed_str = f"  time={elapsed}s" if elapsed is not None else ""
        print(f"[navisport] Passing #{passing_count}: "
              f"chip={chip:15s} cp={cp_label:30s} "
              f"device={dev_id:10s} ts={ts}{elapsed_str}")
        return {'status': 'ok'}

    print(f"[navisport] Unknown: {subject}/{operation}")
    return {'status': 'error', 'message': f'Unknown {subject}/{operation}'}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(
        description='Combined WebSocket + Socket.IO server for simulator testing')
    p.add_argument('-P', '--port', type=int, default=8080,
                   help='Listen port (default 8080)')
    args = p.parse_args()

    print(f"[listener] Starting combined server on 0.0.0.0:{args.port}")
    print(f"[listener]   WebSocket /sim     — for simulator DeviceClient")
    print(f"[listener]   Socket.IO /        — Navisport-mock protocol")
    print(f"[listener]   HTTP     /health   — health check")
    print(f"[listener] Checkpoints loaded: {len(CHECKPOINTS)}")
    for cp in CHECKPOINTS:
        devs = ', '.join(cp['devices']) if cp['devices'] else '-'
        print(f"  {cp['type']:12s} {cp['name']:6s}  id={cp['id']}  devices={devs}")
    print()

    web.run_app(app, host='0.0.0.0', port=args.port, print=lambda *a: None)


if __name__ == '__main__':
    main()
