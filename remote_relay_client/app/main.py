import asyncio
import json
import base64
import aiohttp
import websockets
import time
import hmac
import hashlib
import os
from pathlib import Path
from websockets.exceptions import ConnectionClosed

OPTIONS_FILE = Path("/data/options.json")
LOG_FILE = Path("/data/client.log")
STATUS_FILE = Path("/data/status.json")

MAX_BODY_SIZE = 20 * 1024 * 1024

def log(msg: str) -> None:
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except Exception:
        pass

def set_status(state: str, extra: dict | None = None):
    data = {"state": state, "ts": int(time.time())}
    if extra:
        data.update(extra)
    try:
        STATUS_FILE.write_text(json.dumps(data), encoding="utf-8")
    except Exception:
        pass
    log(f"STATUS: {state}" + (f" {extra}" if extra else ""))

def load_options():
    with OPTIONS_FILE.open() as f:
        return json.load(f)

def canonical_json(obj) -> bytes:
    return json.dumps(obj, separators=(",", ":"), sort_keys=True).encode("utf-8")

def sign(secret: str, payload: dict) -> str:
    mac = hmac.new(secret.encode("utf-8"), canonical_json(payload), hashlib.sha256).digest()
    return base64.b64encode(mac).decode("ascii")

def verify(secret: str, payload: dict, sig: str) -> bool:
    try:
        return hmac.compare_digest(sign(secret, payload), sig or "")
    except Exception:
        return False

def b64e(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")

def b64d(s: str) -> bytes:
    return base64.b64decode(s.encode("ascii")) if s else b""

async def main():
    options = load_options()

    relay_url = options["relay_url"].strip()
    device_id = options["device_id"].strip()
    secret = options["secret"].strip()

    ha_base = os.environ.get("HA_BASE", "http://homeassistant:8123")

    backoff = 2

    while True:
        try:
            set_status("connecting", {"relay": relay_url})
            log(f"Connecting to relay {relay_url} as {device_id}")

            async with websockets.connect(
                relay_url,
                ping_interval=30,
                ping_timeout=20,
                close_timeout=10,
                max_size=MAX_BODY_SIZE,
                max_queue=128,
            ) as relay:

                out_q: asyncio.Queue[str] = asyncio.Queue(maxsize=256)
                stop_evt = asyncio.Event()

                async def sender():
                    try:
                        while not stop_evt.is_set():
                            data = await out_q.get()
                            try:
                                await asyncio.wait_for(relay.send(data), timeout=10)
                            finally:
                                out_q.task_done()
                    except ConnectionClosed:
                        pass
                    except Exception as e:
                        log(f"sender error: {e}")

                async def send_json(obj: dict):
                    data = json.dumps(obj)
                    try:
                        out_q.put_nowait(data)
                    except asyncio.QueueFull:
                        # ja rinda pilna, labāk atvienoties un reconnect nekā iestrēgt
                        raise RuntimeError("relay outbound queue full")

                sender_task = asyncio.create_task(sender())

                # ---------- HANDSHAKE ----------
                ts = int(time.time())
                hello_payload = {"device_id": device_id, "ts": ts}
                await send_json({
                    "device_id": device_id,
                    "ts": ts,
                    "sig": sign(secret, hello_payload),
                })

                set_status("connected", {"device_id": device_id})
                log("Relay handshake OK")
                backoff = 2

                timeout = aiohttp.ClientTimeout(total=60)

                async with aiohttp.ClientSession(timeout=timeout) as session:
                    ws_map: dict[str, aiohttp.ClientWebSocketResponse] = {}

                    async def pump(ws_id_i: str, ha_ws_i: aiohttp.ClientWebSocketResponse):
                        try:
                            async for m in ha_ws_i:
                                if m.type == aiohttp.WSMsgType.TEXT:
                                    out = {"ws_id": ws_id_i, "data": m.data}
                                    await send_json({
                                        "type": "ws_recv",
                                        "payload": out,
                                        "sig": sign(secret, out),
                                    })
                        except Exception as e:
                            log(f"HA WS pump error {ws_id_i}: {e}")
                        finally:
                            ws_map.pop(ws_id_i, None)
                            try:
                                await send_json({
                                    "type": "ws_closed",
                                    "payload": {"ws_id": ws_id_i},
                                    "sig": sign(secret, {"ws_id": ws_id_i}),
                                })
                            except Exception:
                                pass
                            try:
                                await ha_ws_i.close()
                            except Exception:
                                pass

                    async for raw in relay:
                        if isinstance(raw, bytes):
                            raw = raw.decode("utf-8", errors="ignore")

                        try:
                            msg = json.loads(raw)
                        except Exception:
                            continue

                        mtype = msg.get("type")
                        payload = msg.get("payload")
                        sig = msg.get("sig")

                        if not isinstance(payload, dict):
                            continue

                        if not verify(secret, payload, sig):
                            log(f"BAD SIG: {mtype}")
                            continue

                        if mtype == "http_req":
                            req_id = payload.get("request_id")
                            method = payload.get("method", "GET")
                            path = payload.get("path", "/")
                            body = b64d(payload.get("body_b64", ""))

                            headers = {
                                k: v for k, v in (payload.get("headers") or {}).items()
                                if k.lower() not in ("host", "accept-encoding", "content-length", "connection")
                            }

                            try:
                                async with session.request(
                                    method,
                                    ha_base + path,
                                    headers=headers,
                                    data=body,
                                    timeout=aiohttp.ClientTimeout(total=60),
                                ) as resp:

                                    buf = bytearray()
                                    async for chunk in resp.content.iter_chunked(64 * 1024):
                                        buf += chunk
                                        if len(buf) > MAX_BODY_SIZE:
                                            break

                                    resp_payload = {
                                        "request_id": req_id,
                                        "status": resp.status,
                                        "headers": dict(resp.headers),
                                        "body_b64": b64e(bytes(buf)),
                                    }

                                    await send_json({
                                        "type": "http_resp",
                                        "payload": resp_payload,
                                        "sig": sign(secret, resp_payload),
                                    })

                            except Exception as e:
                                err = {
                                    "request_id": req_id,
                                    "status": 502,
                                    "headers": {"content-type": "text/plain"},
                                    "body_b64": b64e(str(e).encode()),
                                }
                                try:
                                    await send_json({
                                        "type": "http_resp",
                                        "payload": err,
                                        "sig": sign(secret, err),
                                    })
                                except Exception:
                                    pass

                        elif mtype == "ws_open":
                            ws_id = payload.get("ws_id")
                            if not ws_id:
                                continue
                            try:
                                ha_ws = await session.ws_connect(
                                    ha_base + "/api/websocket",
                                    heartbeat=30,
                                    max_msg_size=MAX_BODY_SIZE,
                                    headers={"Origin": ha_base},
                                )
                                ws_map[ws_id] = ha_ws
                                asyncio.create_task(pump(ws_id, ha_ws))
                            except Exception as e:
                                log(f"WS OPEN FAIL {ws_id}: {e}")
                                continue

                        elif mtype == "ws_send":
                            ws_id = payload.get("ws_id")
                            ws = ws_map.get(ws_id)
                            if ws:
                                try:
                                    await asyncio.wait_for(ws.send_str(payload.get("data", "")), timeout=10)
                                except Exception:
                                    ws_map.pop(ws_id, None)

                        elif mtype == "ws_close":
                            ws_id = payload.get("ws_id")
                            ws = ws_map.pop(ws_id, None)
                            if ws:
                                try:
                                    await ws.close()
                                except Exception:
                                    pass

                # if we exit relay loop, cleanup
                stop_evt.set()
                try:
                    sender_task.cancel()
                except Exception:
                    pass

        except Exception as e:
            set_status("disconnected", {"error": str(e)})
            log(f"Relay error: {e}")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

if __name__ == "__main__":
    asyncio.run(main())
