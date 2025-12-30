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

OPTIONS_FILE = Path("/data/options.json")
LOG_FILE = Path("/data/client.log")

MAX_BODY_SIZE = 20 * 1024 * 1024  # saskaņo ar serveri (20MB)

def log(msg: str) -> None:
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except Exception:
        pass

def load_options():
    with OPTIONS_FILE.open() as f:
        return json.load(f)

def canonical_json(obj) -> bytes:
    return json.dumps(obj, separators=(",", ":"), sort_keys=True).encode("utf-8")

def sign(secret: str, payload_obj) -> str:
    mac = hmac.new(secret.encode("utf-8"), canonical_json(payload_obj), hashlib.sha256).digest()
    return base64.b64encode(mac).decode("ascii")

def verify(secret: str, payload_obj, sig_b64: str) -> bool:
    try:
        expected = sign(secret, payload_obj)
        return hmac.compare_digest(expected, sig_b64 or "")
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

    # Home Assistant base URL inside addon network
    ha_base = os.environ.get("HA_BASE", "http://homeassistant:8123")

    while True:
        try:
            log(f"Connecting to relay {relay_url} as {device_id}")

            async with websockets.connect(
                relay_url,
                ping_interval=None,   # IMPORTANT: disable to avoid CF/nginx desync
                ping_timeout=None,
                max_size=MAX_BODY_SIZE,
                max_queue=64,
            ) as relay:

                # -------------------------
                # HANDSHAKE: {"device_id","ts","sig"}
                # -------------------------
                ts = int(time.time())
                hello_payload = {"device_id": device_id, "ts": ts}
                hello = {"device_id": device_id, "ts": ts, "sig": sign(secret, hello_payload)}
                await relay.send(json.dumps(hello))
                log("Handshake sent")

                async with aiohttp.ClientSession() as session:
                    ws_proxies = {}  # ws_id -> aiohttp websocket

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

                        # Verify every payload (same as server expects)
                        if not verify(secret, payload, sig):
                            log(f"Bad signature for message type {mtype}")
                            continue

                        # =========================
                        # HTTP REQUEST: http_req
                        # =========================
                        if mtype == "http_req":
                            req_id = payload.get("request_id")
                            method = payload.get("method", "GET")
                            path = payload.get("path", "/")
                            headers = payload.get("headers", {}) or {}
                            body = b64d(payload.get("body_b64", ""))

                            # Avoid compression issues
                            headers = {k: v for k, v in headers.items() if k.lower() != "accept-encoding"}

                            try:
                                async with session.request(
                                    method,
                                    ha_base + path,
                                    headers=headers,
                                    data=body,
                                    timeout=aiohttp.ClientTimeout(total=60),
                                ) as resp:
                                    # Stream read with cap
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
                                    await relay.send(json.dumps({
                                        "type": "http_resp",
                                        "payload": resp_payload,
                                        "sig": sign(secret, resp_payload),
                                    }))
                            except Exception as e:
                                # respond with 502 so browser doesn't hang
                                resp_payload = {
                                    "request_id": req_id,
                                    "status": 502,
                                    "headers": {"content-type": "text/plain"},
                                    "body_b64": b64e(f"HA request failed: {e}".encode("utf-8")),
                                }
                                try:
                                    await relay.send(json.dumps({
                                        "type": "http_resp",
                                        "payload": resp_payload,
                                        "sig": sign(secret, resp_payload),
                                    }))
                                except Exception:
                                    pass

                        # =========================
                        # WS OPEN: ws_open
                        # =========================
                        elif mtype == "ws_open":
                            ws_id = payload.get("ws_id")
                            if not ws_id:
                                continue

                            try:
                                ha_ws = await session.ws_connect(
                                    ha_base + "/api/websocket",
                                    heartbeat=30,
                                    max_msg_size=MAX_BODY_SIZE,
                                )
                                ws_proxies[ws_id] = ha_ws
                            except Exception as e:
                                log(f"Failed to open HA WS: {e}")
                                continue

                            async def pump_from_ha(ws_id_inner: str, ha_ws_inner: aiohttp.ClientWebSocketResponse):
                                try:
                                    async for m in ha_ws_inner:
                                        if m.type == aiohttp.WSMsgType.TEXT:
                                            out_payload = {"ws_id": ws_id_inner, "data": m.data}
                                            await relay.send(json.dumps({
                                                "type": "ws_recv",
                                                "payload": out_payload,
                                                "sig": sign(secret, out_payload),
                                            }))
                                        elif m.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                                            break
                                except Exception:
                                    pass
                                finally:
                                    ws_proxies.pop(ws_id_inner, None)
                                    closed_payload = {"ws_id": ws_id_inner}
                                    try:
                                        await relay.send(json.dumps({
                                            "type": "ws_closed",
                                            "payload": closed_payload,
                                            "sig": sign(secret, closed_payload),
                                        }))
                                    except Exception:
                                        pass

                            asyncio.create_task(pump_from_ha(ws_id, ha_ws))

                        # =========================
                        # WS SEND: ws_send
                        # =========================
                        elif mtype == "ws_send":
                            ws_id = payload.get("ws_id")
                            data = payload.get("data", "")
                            ha_ws = ws_proxies.get(ws_id)
                            if ha_ws:
                                try:
                                    await ha_ws.send_str(data)
                                except Exception:
                                    ws_proxies.pop(ws_id, None)

                        # =========================
                        # WS CLOSE: ws_close
                        # =========================
                        elif mtype == "ws_close":
                            ws_id = payload.get("ws_id")
                            ha_ws = ws_proxies.pop(ws_id, None)
                            if ha_ws:
                                try:
                                    await ha_ws.close()
                                except Exception:
                                    pass

        except Exception as e:
            log(f"Relay connection error: {e}; reconnecting in 2s")
            await asyncio.sleep(2)

asyncio.run(main())
