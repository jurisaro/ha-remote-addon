import asyncio
import json
import base64
import websockets
import aiohttp
from pathlib import Path

OPTIONS_FILE = Path("/data/options.json")

def load_options():
    with OPTIONS_FILE.open() as f:
        return json.load(f)


async def forward_to_ha(req):
    body_bytes = base64.b64decode(req.get("body_b64", ""))

    async with aiohttp.ClientSession() as session:
        async with session.request(
            method=req["method"],
            url="http://homeassistant:8123" + req["path"]
                + (("?" + req["query_string"]) if req.get("query_string") else ""),
            headers=req["headers"],
            data=body_bytes,
            allow_redirects=False,
        ) as resp:

            resp_body_bytes = await resp.read()

            return {
                "status": resp.status,
                "headers": dict(resp.headers),
                "body_b64": base64.b64encode(resp_body_bytes).decode("ascii"),
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

                    # ⭐ KRITISKI: request_id OBLIGĀTS
                    resp["request_id"] = req["request_id"]

                    await ws.send(json.dumps(resp))

        except Exception as e:
            print("Disconnected, retrying in 2s:", e)
            await asyncio.sleep(2)


asyncio.run(main())
