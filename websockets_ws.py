# server_ws_v2.py
import json
from aiohttp import web, WSMsgType

HOST = '0.0.0.0'
HTTP_PORT = 8080   # one port only for both HTTP + WS

stats = {
    'connections': 0,
    'messages': 0,
    'by_device': {},
    'by_type': {}
}
dashboards = set()
simulators = set()

async def ws_dashboard_handler(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    dashboards.add(ws)
    try:
        await ws.send_str(json.dumps({'type': 'init', 'stats': stats}))
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                pass
    finally:
        dashboards.remove(ws)
    return ws

async def ws_sim_handler(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    simulators.add(ws)
    stats['connections'] += 1
    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                stats['messages'] += 1
                try:
                    obj = json.loads(msg.data)
                except Exception:
                    obj = {'raw': msg.data}
                device = obj.get('device_id','unknown')
                dtype = obj.get('device_type','unknown')
                stats['by_device'][device] = stats['by_device'].get(device,0) + 1
                stats['by_type'][dtype] = stats['by_type'].get(dtype,0) + 1

                update = json.dumps({'type': 'update','stats':{
                    'connections': stats['connections'],
                    'messages': stats['messages'],
                    'last': obj
                }})
                # broadcast to dashboards
                dead = []
                for d in dashboards:
                    try:
                        await d.send_str(update)
                    except Exception:
                        dead.append(d)
                for d in dead:
                    dashboards.remove(d)
    finally:
        simulators.remove(ws)
        stats['connections'] -= 1
    return ws

async def index(request):
    return web.FileResponse('dashboard2.html')

app = web.Application()
app.router.add_get('/', index)
app.router.add_get('/ws', ws_dashboard_handler)  # dashboards
app.router.add_get('/sim', ws_sim_handler)       # simulator clients

if __name__ == '__main__':
    web.run_app(app, host=HOST, port=HTTP_PORT)
