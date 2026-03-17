# UDP vs TCP Live Stream

Side-by-side comparison of UDP (WebRTC) vs TCP (WebSocket + MSE) live video streaming with real-time latency metrics.

## What it does

Streams the same video source over both protocols simultaneously so you can visually compare latency, quality, and reliability in real time.

## Stack

- **Server:** Python (asyncio + aiohttp)
- **UDP path:** WebRTC via aiortc
- **TCP path:** WebSocket + Media Source Extensions
- **Frontend:** Vanilla HTML/JS with live latency overlay
