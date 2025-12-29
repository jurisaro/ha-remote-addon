import asyncio
import json
import base64
import aiohttp
import websockets
import sys
from pathlib import Path

# =====================================================
# KRITISKI: NEKĀDA STDOUT / STDERR HA ADD-ONĀ
# =====================================================
sys.stdout = None
sys.stderr = None

OPTIONS_FILE = Path("/data/options.json")

MAX_BODY_SIZE = 10 * 1024 * 1024  # 10MB hard limit


def load_options():
    with OPTIONS_FILE.open() as f:
        return json.load(f)


async def main():
    options = load_options()

    relay_url = options["relay_url"]
    device_id = options["device_id"]
    secret = options["secret"]

    while True:
        try:
            async with websockets.connect(
                relay_url,
                ping_interval=30,
                ping_timeout=60,
                max_size=MAX_BODY_SIZE,
                max_queue=32,
            ) as relay:

                # -------------------------
                # HANDSHAKE (MULTI-DEVICE)
                # -------------------------
                await relay.send(json.dumps({
                    "device_id": device_id,
                    "secret": secret,
                }))

                async with aiohttp.ClientSession() as session:
                    ws_proxies = {}

                    async for msg in relay:
                        data = json.loads(msg)

                        # =================================================
                        # HTTP REQUEST FROM EC2 → HA
                        # =================================================
                        if "request_id" in data:
                            try:
                                async with session.request(
                                    data["method"],
                                    "http://homeassistant:8123" + data["path"],
                                    headers={
                                        k: v for k, v in data["headers"].items()
                                        if k.lower() != "accept-encoding"
                                    },
                                    data=base64.b64decode(data["body_b64"]),
                                ) as resp:

                                    body = b""
                                    async for chunk in resp.content.iter_chunked(64 * 1024):
                                        body += chunk
                                        if len(body) > MAX_BODY_SIZE:
                                            break

                                    await relay.send(json.dumps({
                                        "request_id": data["request_id"],
                                        "status": resp.status,
                                        "headers": dict(resp.headers),
                                        "body_b64": base64.b64encode(body).decode(),
                                    }))

                            except Exception:
                                # neatbildējam, ja HA request saplīst
                                pass

                        # =================================================
                        # WS OPEN (browser → HA)
                        # =================================================
                        elif data.get("type") == "ws_open":
                            try:
                                ha_ws = await session.ws_connect(
                                    "http://homeassistant:8123/api/websocket",
                                    heartbeat=30,
                                    max_msg_size=MAX_BODY_SIZE,
                                )
                            except Exception:
                                continue

                            ws_proxies[data["ws_id"]] = ha_ws

                            async def pump(ws_id, ha_ws):
                                try:
                                    async for msg in ha_ws:
                                        if msg.type == aiohttp.WSMsgType.TEXT:
                                            try:
                                                await relay.send(json.dumps({
                                                    "type": "ws_recv",
                                                    "ws_id": ws_id,
                                                    "data": msg.data,
                                                }))
                                            except Exception:
                                                break
                                except Exception:
                                    pass
                                finally:
                                    ws_proxies.pop(ws_id, None)

                            asyncio.create_task(pump(data["ws_id"], ha_ws))

                        # =================================================
                        # WS SEND (browser → HA)
                        # =================================================
                        elif data.get("type") == "ws_send":
                            ws = ws_proxies.get(data["ws_id"])
                            if ws:
                                try:
                                    await ws.send_str(data["data"])
                                except Exception:
                                    ws_proxies.pop(data["ws_id"], None)

                        # =================================================
                        # WS CLOSE
                        # =================================================
                        elif data.get("type") == "ws_close":
                            ws = ws_proxies.pop(data["ws_id"], None)
                            if ws:
                                try:
                                    await ws.close()
                                except Exception:
                                    pass

        except Exception:
            # soft reconnect loop
            await asyncio.sleep(2)


asyncio.run(main())
