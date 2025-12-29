import asyncio
import json
import base64
import aiohttp
import websockets
import sys
from pathlib import Path

# -------------------------
# KILL STDOUT (KRITISKI)
# -------------------------
sys.stdout = None
sys.stderr = None

OPTIONS_FILE = Path("/data/options.json")


def load_options():
    with OPTIONS_FILE.open() as f:
        return json.load(f)


async def main():
    options = load_options()
    relay_url = options["relay_url"]
    device_key = options["device_key"]

    while True:
        try:
            async with websockets.connect(
                relay_url,
                ping_interval=30,
                ping_timeout=60,
                max_size=10 * 1024 * 1024,
                max_queue=32,
            ) as relay:

                await relay.send(device_key)

                async with aiohttp.ClientSession() as session:
                    ws_proxies = {}

                    async for msg in relay:
                        data = json.loads(msg)

                        # -----------------
                        # HTTP
                        # -----------------
                        if "request_id" in data:
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
                                    if len(body) > 10 * 1024 * 1024:
                                        break

                                await relay.send(json.dumps({
                                    "request_id": data["request_id"],
                                    "status": resp.status,
                                    "headers": dict(resp.headers),
                                    "body_b64": base64.b64encode(body).decode(),
                                }))

                        # -----------------
                        # WS OPEN
                        # -----------------
                        elif data.get("type") == "ws_open":
                            ha_ws = await session.ws_connect(
                                "http://homeassistant:8123/api/websocket"
                            )
                            ws_proxies[data["ws_id"]] = ha_ws

                            async def pump(ws_id, ha_ws):
                                try:
                                    async for msg in ha_ws:
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

                            asyncio.create_task(pump(data["ws_id"], ha_ws))

                        # -----------------
                        # WS SEND
                        # -----------------
                        elif data.get("type") == "ws_send":
                            try:
                                await ws_proxies[data["ws_id"]].send_str(data["data"])
                            except Exception:
                                pass

                        # -----------------
                        # WS CLOSE
                        # -----------------
                        elif data.get("type") == "ws_close":
                            ws = ws_proxies.pop(data["ws_id"], None)
                            if ws:
                                try:
                                    await ws.close()
                                except Exception:
                                    pass

        except Exception:
            await asyncio.sleep(2)


asyncio.run(main())
