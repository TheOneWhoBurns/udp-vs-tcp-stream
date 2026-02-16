# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "aiohttp",
#     "qrcode",
#     "Pillow",
# ]
# ///

import asyncio
import io
import json
import random
import socket
import ssl
import subprocess
import tempfile
import time
from pathlib import Path

import qrcode
from aiohttp import web

STATIC = Path(__file__).parent / "static"


def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    finally:
        s.close()


def generate_self_signed_cert(tmp_dir):
    cert_path = Path(tmp_dir) / "cert.pem"
    key_path = Path(tmp_dir) / "key.pem"
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048",
            "-keyout", str(key_path), "-out", str(cert_path),
            "-days", "1", "-nodes",
            "-subj", "/CN=localhost",
            "-addext", f"subjectAltName=IP:{get_local_ip()},DNS:localhost",
        ],
        capture_output=True,
        check=True,
    )
    return str(cert_path), str(key_path)


viewers_signal = set()
broadcasters_signal = set()
viewers_tcp = set()
broadcasters_tcp = set()

sim_config = {"loss_percent": 0, "latency_ms": 0}


async def handle_signal_ws(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    role = request.query.get("role")

    if role == "broadcaster":
        broadcasters_signal.add(ws)
        for v in viewers_signal:
            await v.send_json({"type": "peer_joined"})
        await notify_broadcaster_if_viewer_present(ws, viewers_signal)
    else:
        viewers_signal.add(ws)
        for b in broadcasters_signal:
            await b.send_json({"type": "peer_joined"})
        await notify_broadcaster_if_viewer_present(ws, broadcasters_signal)

    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                data = json.loads(msg.data)
                targets = viewers_signal if role == "broadcaster" else broadcasters_signal
                for t in list(targets):
                    try:
                        await t.send_json(data)
                    except Exception:
                        targets.discard(t)
    finally:
        if role == "broadcaster":
            broadcasters_signal.discard(ws)
            for v in viewers_signal:
                try:
                    await v.send_json({"type": "peer_left"})
                except Exception:
                    pass
        else:
            viewers_signal.discard(ws)
            for b in broadcasters_signal:
                try:
                    await b.send_json({"type": "peer_left"})
                except Exception:
                    pass
    return ws


async def notify_broadcaster_if_viewer_present(ws, others):
    if others:
        await ws.send_json({"type": "peer_joined"})


async def handle_tcp_ws(request):
    ws = web.WebSocketResponse(max_msg_size=10 * 1024 * 1024)
    await ws.prepare(request)
    role = request.query.get("role")

    if role == "broadcaster":
        broadcasters_tcp.add(ws)
        for v in viewers_tcp:
            await v.send_json({"type": "peer_joined"})
        await notify_broadcaster_if_viewer_present(ws, viewers_tcp)
    else:
        viewers_tcp.add(ws)
        for b in broadcasters_tcp:
            await b.send_json({"type": "peer_joined"})
        await notify_broadcaster_if_viewer_present(ws, broadcasters_tcp)

    try:
        pending_ts = None
        burst_remaining = 0
        async for msg in ws:
            targets = viewers_tcp if role == "broadcaster" else broadcasters_tcp

            if role == "broadcaster":
                if msg.type == web.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                    except Exception:
                        data = {}
                    if data.get("type") == "ts":
                        pending_ts = msg.data
                        continue
                    for t in list(targets):
                        try:
                            await t.send_str(msg.data)
                        except Exception:
                            targets.discard(t)
                    continue

                if msg.type == web.WSMsgType.BINARY:
                    cfg = sim_config
                    if burst_remaining > 0:
                        burst_remaining -= 1
                        pending_ts = None
                        for t in list(targets):
                            try:
                                await t.send_json({"type": "sim_drop"})
                            except Exception:
                                targets.discard(t)
                        continue
                    if cfg["loss_percent"] > 0 and random.random() * 100 < cfg["loss_percent"]:
                        burst_remaining = random.randint(1, 2)
                        pending_ts = None
                        for t in list(targets):
                            try:
                                await t.send_json({"type": "sim_drop"})
                            except Exception:
                                targets.discard(t)
                        continue

                    ts_data = pending_ts
                    bin_data = msg.data
                    pending_ts = None
                    delay = cfg["latency_ms"] / 1000.0 if cfg["latency_ms"] > 0 else 0

                    async def relay(ts_d, bin_d, tgts, d):
                        if d > 0:
                            await asyncio.sleep(d)
                        for t in list(tgts):
                            try:
                                if ts_d:
                                    await t.send_str(ts_d)
                                await t.send_bytes(bin_d)
                            except Exception:
                                tgts.discard(t)

                    if delay > 0:
                        asyncio.create_task(relay(ts_data, bin_data, targets, delay))
                    else:
                        await relay(ts_data, bin_data, targets, 0)
                    continue

            for t in list(targets):
                try:
                    if msg.type == web.WSMsgType.BINARY:
                        await t.send_bytes(msg.data)
                    elif msg.type == web.WSMsgType.TEXT:
                        await t.send_str(msg.data)
                except Exception:
                    targets.discard(t)
    finally:
        if role == "broadcaster":
            broadcasters_tcp.discard(ws)
            for v in viewers_tcp:
                try:
                    await v.send_json({"type": "peer_left"})
                except Exception:
                    pass
        else:
            viewers_tcp.discard(ws)
            for b in broadcasters_tcp:
                try:
                    await b.send_json({"type": "peer_left"})
                except Exception:
                    pass
    return ws


async def handle_qr(request):
    host = get_local_ip()
    port = request.app["port"]
    url = f"https://{host}:{port}/broadcast"
    img = qrcode.make(url, box_size=8, border=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return web.Response(body=buf.getvalue(), content_type="image/png")


async def handle_broadcast_url(request):
    host = get_local_ip()
    port = request.app["port"]
    url = f"https://{host}:{port}/broadcast"
    return web.json_response({"url": url})


async def handle_simulate_get(request):
    return web.json_response(sim_config)


async def handle_simulate_post(request):
    data = await request.json()
    for k in ("loss_percent", "latency_ms"):
        if k in data:
            sim_config[k] = max(0, int(data[k]))
    for v in list(viewers_tcp):
        try:
            await v.send_json({"type": "sim_config", **sim_config})
        except Exception:
            pass
    return web.json_response(sim_config)


app = web.Application()
app.router.add_get("/", lambda r: web.FileResponse(STATIC / "index.html"))
app.router.add_get("/broadcast", lambda r: web.FileResponse(STATIC / "broadcast.html"))
app.router.add_get("/qr", handle_qr)
app.router.add_get("/broadcast-url", handle_broadcast_url)
app.router.add_get("/api/simulate", handle_simulate_get)
app.router.add_post("/api/simulate", handle_simulate_post)
app.router.add_get("/ws/signal", handle_signal_ws)
app.router.add_get("/ws/tcp", handle_tcp_ws)
app.router.add_static("/static/", STATIC)


def main():
    port = 8443
    app["port"] = port
    tmp_dir = tempfile.mkdtemp()
    cert, key = generate_self_signed_cert(tmp_dir)
    ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ssl_ctx.load_cert_chain(cert, key)
    host = get_local_ip()
    print(f"\n  Viewer:      https://{host}:{port}/")
    print(f"  Broadcaster: https://{host}:{port}/broadcast\n")
    web.run_app(app, host="0.0.0.0", port=port, ssl_context=ssl_ctx, print=None)


if __name__ == "__main__":
    main()
