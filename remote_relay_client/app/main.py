import asyncio
import websockets
import os

RELAY_URL = os.environ.get("RELAY_URL")

async def main():
    if not RELAY_URL:
        raise RuntimeError("RELAY_URL not set")
    async with websockets.connect(RELAY_URL) as ws:
        await ws.send("hello from Home Assistant add-on")
        while True:
            msg = await ws.recv()
            print(msg)

asyncio.run(main())
