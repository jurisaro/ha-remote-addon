import asyncio
import json
import base64
import aiohttp
import websockets
import sys
sys.stdout = None
sys.stderr = None
from pathlib import Path

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
            async with websockets.connect(relay_url) as relay:
                await relay.send(device_key)
                print("Connected to relay")

                async with aiohttp.ClientSession() as session:
                    ws_proxies = {}

                    async for msg in relay:
                        data = json.loads(msg)

                        # =====================
                        # HTTP REQUEST
                        # =====================
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
                                body = await resp.read()
                                await relay.send(json.dumps({
                                    "request_id": data["request_id"],
                                    "status": resp.status,
                                    "headers": dict(resp.headers),
                                    "body_b64": base64.b64encode(body).decode(),
                                }))

                        # =====================
                        # WS OPEN
                        # =====================
                        elif data.get("type") == "ws_open":
                            ha_ws = await session.ws_connect(
                                "http://homeassistant:8123/api/websocket"
                            )
                            ws_proxies[data["ws_id"]] = ha_ws

                            async def pump(ws_id, ha_ws):
                                async for msg in ha_ws:
                                    await relay.send(json.dumps({
                                        "type": "ws_recv",
                                        "ws_id": ws_id,
                                        "data": msg.data,
                                    }))

                            asyncio.create_task(pump(data["ws_id"], ha_ws))

                        # =====================
                        # WS SEND
                        # =====================
                        elif data.get("type") == "ws_send":
                            await ws_proxies[data["ws_id"]].send_str(data["data"])

                        # =====================
                        # WS CLOSE
                        # =====================
                        elif data.get("type") == "ws_close":
                            await ws_proxies[data["ws_id"]].close()
                            ws_proxies.pop(data["ws_id"], None)

        except Exception as e:
            print("Disconnected, retrying in 2s:", e)
            await asyncio.sleep(2)


asyncio.run(main())
