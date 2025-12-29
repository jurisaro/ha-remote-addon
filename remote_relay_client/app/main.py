import asyncio
import json
import websockets
import aiohttp
from pathlib import Path

OPTIONS_FILE = Path("/data/options.json")

def load_options():
    with OPTIONS_FILE.open() as f:
        return json.load(f)

async def forward_to_ha(req):
    async with aiohttp.ClientSession() as session:
        async with session.request(
            req["method"],
            "http://homeassistant:8123" + req["path"],
            headers=req["headers"],
            data=req["body"].encode()
        ) as resp:
            return {
                "status": resp.status,
                "headers": dict(resp.headers),
                "body": await resp.text()
            }

async def main():
    options = load_options()
    relay_url = options["relay_url"]
    device_key = options["device_key"]

    while True:
        try:
            async with websockets.connect(relay_url) as ws:
                await ws.send(device_key)
                print("Connected to relay")

                async for msg in ws:
                    req = json.loads(msg)
                    resp = await forward_to_ha(req)
                    resp["request_id"] = req["request_id"]  # ⭐ KRITISKI
                    await ws.send(json.dumps(resp))

        except Exception as e:
            print("Disconnected, retrying in 2s:", e)
            await asyncio.sleep(2)

asyncio.run(main())
