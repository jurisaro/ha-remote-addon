import asyncio
import json
import websockets
from pathlib import Path

OPTIONS_FILE = Path("/data/options.json")

def load_options():
    if not OPTIONS_FILE.exists():
        raise RuntimeError("options.json not found")
    with OPTIONS_FILE.open() as f:
        return json.load(f)

async def main():
    options = load_options()
    relay_url = options.get("relay_url")

    if not relay_url:
        raise RuntimeError("relay_url not set in add-on configuration")

    async with websockets.connect(relay_url) as ws:
        await ws.send("hello from Home Assistant add-on")
        while True:
            msg = await ws.recv()
            print(msg)

asyncio.run(main())
