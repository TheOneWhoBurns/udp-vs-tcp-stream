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
import socket
import ssl
import subprocess
import tempfile
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
        async for msg in ws:
            targets = viewers_tcp if role == "broadcaster" else broadcasters_tcp
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


app = web.Application()
app.router.add_get("/", lambda r: web.FileResponse(STATIC / "index.html"))
app.router.add_get("/broadcast", lambda r: web.FileResponse(STATIC / "broadcast.html"))
app.router.add_get("/qr", handle_qr)
app.router.add_get("/broadcast-url", handle_broadcast_url)
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
