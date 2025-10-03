# server_ws_v3.py
import json
import asyncio    
import resource
import sys
from aiohttp import web, WSMsgType

HOST = '0.0.0.0'
HTTP_PORT = 8080   # yksi portti HTTP + WS

stats = {
    'connections': 0,
    'messages': 0,
    'by_device': {},
    'by_type': {},
    'last': None
}

dashboards = set()
simulators = set()

# ----------------------
# Dashboard websocket
# ----------------------
async def ws_dashboard_handler(request):
    ws = web.WebSocketResponse(max_msg_size=10*1024*1024)
    
    await ws.prepare(request)
    dashboards.add(ws)
    try:
        # Lähetetään init snapshot
        await ws.send_str(json.dumps({'type': 'init', 'stats': stats}))
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                # Dashboard ei lähetä mitään → voidaan ignoroida
                pass
    finally:
        dashboards.discard(ws)
    return ws

# ----------------------
# Simulaattori websocket
# ----------------------
async def ws_sim_handler(request):
    ws = web.WebSocketResponse(max_msg_size=10*1024*1024)
    await ws.prepare(request)
    simulators.add(ws)
    stats['connections'] += 1
    #print(f"[DEBUG] Connections: {stats['connections']}")
    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                stats['messages'] += 1
                try:
                    obj = json.loads(msg.data)
                except Exception:
                    obj = {'raw': msg.data}

                device = obj.get('device_id', 'unknown')
                dtype = obj.get('device_type', 'unknown')

                stats['by_device'][device] = stats['by_device'].get(device, 0) + 1
                stats['by_type'][dtype]   = stats['by_type'].get(dtype, 0) + 1
                stats['last'] = obj
            elif msg.type == WSMsgType.ERROR:
                print(f"Simulator WS error: {ws.exception()}")
    finally:
        simulators.discard(ws)
        stats['connections'] -= 1
    return ws

# ----------------------
# Static index (dashboard page)
# ----------------------
async def index(request):
    return web.FileResponse('dashboard.html')

# ----------------------
# Broadcast loop
# ----------------------
async def broadcast_loop():
    """Lähetä kaikille dashboardeille tilastot 1s välein."""
    while True:
        if dashboards:  # vain jos joku kuuntelee
            update = json.dumps({'type': 'update', 'stats': stats})
            dead = []
            for d in dashboards:
                try:
                    await d.send_str(update)
                except Exception:
                    dead.append(d)
            for d in dead:
                dashboards.discard(d)
        await asyncio.sleep(0.1)  # 1 päivitys / s

# ----------------------
# App setup
# ----------------------
app = web.Application()
app.router.add_get('/', index)
app.router.add_get('/ws', ws_dashboard_handler)  # dashboard clients
app.router.add_get('/sim', ws_sim_handler)       # simulator clients

async def on_startup(app):
           app['broadcast_task'] = asyncio.create_task(broadcast_loop())

async def on_cleanup(app):
    app['broadcast_task'].cancel()
    await asyncio.gather(app['broadcast_task'], return_exceptions=True)

app.on_startup.append(on_startup)
app.on_cleanup.append(on_cleanup)

if __name__ == '__main__':


# Tarkistetaan max open files
    soft_limit, hard_limit = resource.getrlimit(resource.RLIMIT_NOFILE)
    MIN_FILES_REQUIRED = 4096

    if soft_limit < MIN_FILES_REQUIRED:
        print(f"ERROR: Max open files (ulimit -n) too low: {soft_limit}")
        print(f"Please increase it to at least {MIN_FILES_REQUIRED} before running the server.")
        sys.exit(1)

    print(f"Max open files ok: {soft_limit}")
    web.run_app(app, host=HOST, port=HTTP_PORT,backlog=4096)
