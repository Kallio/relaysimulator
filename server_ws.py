# server_ws.py
import asyncio, json
from aiohttp import web, WSMsgType

HOST = '0.0.0.0'
TCP_PORT = 9000
HTTP_PORT = 8080

stats = {
    'connections': 0,
    'messages': 0,
    'by_device': {},
    'by_type': {}
}
websockets = set()

async def tcp_handler(reader, writer):
    stats['connections'] += 1
    addr = writer.get_extra_info('peername')
    try:
        while not reader.at_eof():
            line = await reader.readline()
            if not line:
                break
            stats['messages'] += 1
            text = line.decode().strip()
            # try parse JSON
            try:
                obj = json.loads(text)
            except Exception:
                obj = {'raw': text}
            device = obj.get('device_id','unknown')
            dtype = obj.get('device_type','unknown')
            stats['by_device'][device] = stats['by_device'].get(device,0) + 1
            stats['by_type'][dtype] = stats['by_type'].get(dtype,0) + 1
            # broadcast small update to websockets
            msg = json.dumps({'type':'update','stats':{
                'connections': stats['connections'],
                'messages': stats['messages'],
                'last': obj
            }})
            await broadcast(msg)
    except Exception as e:
        print("tcp error:", e)
    finally:
        writer.close()
        await writer.wait_closed()
        stats['connections'] -= 1

async def broadcast(msg):
    to_remove = []
    for ws in websockets:
        try:
            await ws.send_str(msg)
        except Exception:
            to_remove.append(ws)
    for r in to_remove:
        websockets.remove(r)

async def ws_handler(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    websockets.add(ws)
    try:
        await ws.send_str(json.dumps({'type':'init','stats':stats}))
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                pass
    finally:
        websockets.remove(ws)
    return ws

async def index(request):
    return web.FileResponse('dashboard.html')

async def start_tcp_server(app):
    loop = asyncio.get_event_loop()
    server = await asyncio.start_server(tcp_handler, HOST, TCP_PORT)
    app['tcp_server'] = server

async def cleanup_tcp_server(app):
    server = app.get('tcp_server')
    if server:
        server.close()
        await server.wait_closed()

app = web.Application()
app.router.add_get('/', index)
app.router.add_get('/ws', ws_handler)
app.on_startup.append(start_tcp_server)
app.on_cleanup.append(cleanup_tcp_server)

if __name__ == '__main__':
    web.run_app(app, host=HOST, port=HTTP_PORT)
