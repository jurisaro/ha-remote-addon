#!/usr/bin/env python3
import asyncio
import base64
import hmac
import hashlib
import json
import time
import uuid
import threading
from pathlib import Path
from typing import Dict, Any, Optional
from urllib.parse import urlparse

from fastapi import FastAPI, Request, Response, WebSocket, WebSocketDisconnect
import uvicorn
import websockets
from websockets.exceptions import ConnectionClosed

# ================= CONFIG =================
BASE_DOMAIN = "myhalink.com"
DEVICES_FILE = Path("/root/devices.json")  # <- ieliec precīzu ceļu, lai nav WD pārpratumu

USER_HOST = "0.0.0.0"
USER_PORT = 8765

TUNNEL_HOST = "0.0.0.0"
TUNNEL_PORT = 8766

DEVICE_AWAIT_S = 25
HTTP_TIMEOUT_S = 60
DEVICE_SEND_TIMEOUT_S = 10

MAX_MSG_SIZE = 20 * 1024 * 1024
MAX_QUEUE = 128

TUNNEL_PING_INTERVAL_S = 30
TUNNEL_PING_TIMEOUT_S = 20
TUNNEL_CLOSE_TIMEOUT_S = 10

# ================= APP ====================
app = FastAPI()

# ================= STATE ==================
device_ws: Dict[str, websockets.WebSocketServerProtocol] = {}
device_send_q: Dict[str, asyncio.Queue] = {}
device_writer_task: Dict[str, asyncio.Task] = {}

pending_http: Dict[str, asyncio.Future] = {}
pending_ws: Dict[str, WebSocket] = {}

device_connected_evt: Dict[str, asyncio.Event] = {}
device_last_seen: Dict[str, float] = {}

request_owner: Dict[str, str] = {}

metrics = {
    "boot_ts": int(time.time()),
    "device_connected": 0,
    "device_disconnected": 0,
    "device_reconnected": 0,
    "await_device_success": 0,
    "await_device_timeout": 0,
    "http_requests": 0,
    "http_timeouts": 0,
    "http_upstream_errors": 0,
    "ws_open": 0,
    "ws_send": 0,
    "ws_send_blocked": 0,
    "device_send_ok": 0,
    "device_send_fail": 0,
    "bad_sig": 0,
}

# ================= UTILS ==================
def log(msg: str):
    print(msg, flush=True)

def load_devices():
    with DEVICES_FILE.open() as f:
        return {d["device_id"]: d["secret"] for d in json.load(f)}

DEVICES = load_devices()

def canonical(obj: Any) -> bytes:
    return json.dumps(obj, separators=(",", ":"), sort_keys=True).encode()

def sign(secret: str, payload: Any) -> str:
    return base64.b64encode(
        hmac.new(secret.encode(), canonical(payload), hashlib.sha256).digest()
    ).decode()

def verify(secret: str, payload: Any, sig: str) -> bool:
    return hmac.compare_digest(sign(secret, payload), sig or "")

def b64e(b: bytes) -> str:
    return base64.b64encode(b).decode()

def b64d(s: str) -> bytes:
    return base64.b64decode(s) if s else b""

def device_from_host(host: Optional[str]) -> Optional[str]:
    if not host:
        return None
    h = host.split(":")[0].lower()
    if h.endswith("." + BASE_DOMAIN):
        return h[: -(len(BASE_DOMAIN) + 1)]
    return None

def origin_host(origin: Optional[str]) -> Optional[str]:
    if not origin:
        return None
    try:
        return urlparse(origin).hostname
    except Exception:
        return None

def get_or_make_evt(did: str) -> asyncio.Event:
    evt = device_connected_evt.get(did)
    if not evt:
        evt = asyncio.Event()
        device_connected_evt[did] = evt
    return evt

async def await_device(did: str, timeout_s: int) -> bool:
    if device_ws.get(did):
        return True
    evt = get_or_make_evt(did)
    try:
        await asyncio.wait_for(evt.wait(), timeout=timeout_s)
        return bool(device_ws.get(did))
    except asyncio.TimeoutError:
        return False

def fail_pending_for_device(did: str, reason: str):
    doomed = [rid for rid, owner in request_owner.items() if owner == did]
    for rid in doomed:
        fut = pending_http.pop(rid, None)
        request_owner.pop(rid, None)
        if fut and not fut.done():
            fut.set_exception(ConnectionError(reason))

async def device_writer(did: str):
    q = device_send_q[did]
    while True:
        item = await q.get()
        if item is None:
            q.task_done()
            return
        ws = device_ws.get(did)
        if not ws:
            metrics["device_send_fail"] += 1
            q.task_done()
            continue
        try:
            await asyncio.wait_for(ws.send(item), timeout=DEVICE_SEND_TIMEOUT_S)
            metrics["device_send_ok"] += 1
        except Exception:
            metrics["device_send_fail"] += 1
        finally:
            q.task_done()

async def device_send(did: str, data: str) -> bool:
    q = device_send_q.get(did)
    if not q:
        q = asyncio.Queue(maxsize=MAX_QUEUE)
        device_send_q[did] = q
    if did not in device_writer_task or device_writer_task[did].done():
        device_writer_task[did] = asyncio.create_task(device_writer(did))
    try:
        q.put_nowait(data)
        return True
    except asyncio.QueueFull:
        return False

# ================= TUNNEL =================
async def handle_device(ws: websockets.WebSocketServerProtocol):
    did = None
    log("[TUNNEL] incoming connection")
    try:
        # v21 stils: GAIDA hello bez timeout
        hello_raw = await ws.recv()
        log("[TUNNEL] hello received")
        hello = json.loads(hello_raw)

        did = hello.get("device_id")
        ts = hello.get("ts")
        sig = hello.get("sig")

        if not did or did not in DEVICES:
            log("[TUNNEL] unknown device in hello")
            return

        if not verify(DEVICES[did], {"device_id": did, "ts": ts}, sig):
            log(f"[TUNNEL] bad hello sig {did}")
            metrics["bad_sig"] += 1
            return

        prev = device_ws.get(did)
        device_ws[did] = ws
        device_last_seen[did] = time.time()

        evt = get_or_make_evt(did)
        evt.set()

        if prev:
            metrics["device_reconnected"] += 1
            log(f"[DEVICE RECONNECTED] {did}")
        else:
            metrics["device_connected"] += 1
            log(f"[DEVICE CONNECTED] {did}")

        async for msg in ws:
            device_last_seen[did] = time.time()
            try:
                data = json.loads(msg)
            except Exception:
                continue

            mtype = data.get("type")
            payload = data.get("payload")
            sig = data.get("sig")

            if not isinstance(payload, dict):
                continue

            if not verify(DEVICES[did], payload, sig):
                metrics["bad_sig"] += 1
                continue

            if mtype == "http_resp":
                rid = payload.get("request_id")
                fut = pending_http.pop(rid, None)
                request_owner.pop(rid, None)
                if fut and not fut.done():
                    fut.set_result(payload)

            elif mtype == "ws_recv":
                ws_id = payload.get("ws_id")
                bw = pending_ws.get(ws_id)
                if bw:
                    try:
                        await bw.send_text(payload.get("data", ""))
                    except Exception:
                        pending_ws.pop(ws_id, None)

            elif mtype == "ws_closed":
                ws_id = payload.get("ws_id")
                pending_ws.pop(ws_id, None)

    except ConnectionClosed:
        # normāls tīkla pārtraukums
        pass
    except Exception as e:
        log(f"[TUNNEL] handler error: {e}")
    finally:
        if did:
            if device_ws.get(did) is ws:
                device_ws.pop(did, None)

            metrics["device_disconnected"] += 1
            log(f"[DEVICE DISCONNECTED] {did}")

            evt = get_or_make_evt(did)
            evt.clear()

            fail_pending_for_device(did, "device disconnected")

            q = device_send_q.get(did)
            if q:
                try:
                    q.put_nowait(None)
                except Exception:
                    pass

async def tunnel_main():
    async with websockets.serve(
        handle_device,
        TUNNEL_HOST,
        TUNNEL_PORT,
        ping_interval=TUNNEL_PING_INTERVAL_S,
        ping_timeout=TUNNEL_PING_TIMEOUT_S,
        close_timeout=TUNNEL_CLOSE_TIMEOUT_S,
        max_size=MAX_MSG_SIZE,
        max_queue=MAX_QUEUE,
    ):
        log(f"[TUNNEL] listening on {TUNNEL_PORT}")
        await asyncio.Future()

def start_tunnel_thread():
    asyncio.run(tunnel_main())

# ================= HTTP ===================
@app.get("/_metrics")
async def metrics_endpoint():
    data = dict(metrics)
    data["devices_connected_now"] = len(device_ws)
    data["pending_http"] = len(pending_http)
    data["pending_ws"] = len(pending_ws)
    now = time.time()
    data["device_idle_s"] = {k: int(now - v) for k, v in device_last_seen.items()}
    return data

@app.api_route("/{path:path}", methods=["GET", "POST", "OPTIONS"])
async def http_entry(path: str, request: Request):
    metrics["http_requests"] += 1
    host = request.headers.get("host")
    origin = request.headers.get("origin")
    did = device_from_host(host)

    if request.method == "POST":
        ohost = origin_host(origin)
        if ohost and host and ohost != host.split(":")[0]:
            return Response(status_code=303, headers={"Location": "/"})

    if not did or did not in DEVICES:
        return Response("Unknown device", status_code=404)

    if not device_ws.get(did):
        ok = await await_device(did, DEVICE_AWAIT_S)
        if ok:
            metrics["await_device_success"] += 1
        else:
            metrics["await_device_timeout"] += 1
            return Response("Device not connected", status_code=503)

    body = await request.body()
    req_id = str(uuid.uuid4())

    fut = asyncio.get_running_loop().create_future()
    pending_http[req_id] = fut
    request_owner[req_id] = did

    headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in (
            "host", "origin", "referer",
            "content-length", "connection", "accept-encoding",
            "x-forwarded-host", "x-forwarded-proto", "x-forwarded-for",
        )
    }

    payload = {
        "request_id": req_id,
        "method": request.method,
        "path": "/" + path,
        "headers": headers,
        "body_b64": b64e(body),
    }

    msg = json.dumps({
        "type": "http_req",
        "payload": payload,
        "sig": sign(DEVICES[did], payload),
    })

    ok = await device_send(did, msg)
    if not ok:
        metrics["ws_send_blocked"] += 1
        pending_http.pop(req_id, None)
        request_owner.pop(req_id, None)
        return Response("Device send queue full", status_code=503)

    try:
        resp = await asyncio.wait_for(fut, timeout=HTTP_TIMEOUT_S)
    except asyncio.TimeoutError:
        metrics["http_timeouts"] += 1
        pending_http.pop(req_id, None)
        request_owner.pop(req_id, None)
        return Response("Upstream timeout", status_code=504)
    except Exception:
        metrics["http_upstream_errors"] += 1
        pending_http.pop(req_id, None)
        request_owner.pop(req_id, None)
        return Response("Upstream error", status_code=502)

    return Response(
        content=b64d(resp.get("body_b64", "")),
        status_code=int(resp.get("status", 502)),
        headers={
            k: v for k, v in (resp.get("headers") or {}).items()
            if k.lower() not in ("content-length", "transfer-encoding", "connection")
        },
    )

@app.websocket("/api/websocket")
async def ws_entry(browser_ws: WebSocket):
    await browser_ws.accept()
    metrics["ws_open"] += 1

    did = device_from_host(browser_ws.headers.get("host"))
    if not did or did not in DEVICES:
        await browser_ws.close(code=1008)
        return

    if not device_ws.get(did):
        ok = await await_device(did, DEVICE_AWAIT_S)
        if not ok:
            await browser_ws.close(code=1013)
            return

    ws_id = str(uuid.uuid4())
    pending_ws[ws_id] = browser_ws

    payload_open = {"ws_id": ws_id}
    msg_open = json.dumps({
        "type": "ws_open",
        "payload": payload_open,
        "sig": sign(DEVICES[did], payload_open),
    })

    ok = await device_send(did, msg_open)
    if not ok:
        metrics["ws_send_blocked"] += 1
        pending_ws.pop(ws_id, None)
        await browser_ws.close(code=1013)
        return

    try:
        while True:
            msg = await browser_ws.receive_text()
            metrics["ws_send"] += 1

            payload_send = {"ws_id": ws_id, "data": msg}
            out = json.dumps({
                "type": "ws_send",
                "payload": payload_send,
                "sig": sign(DEVICES[did], payload_send),
            })

            ok = await device_send(did, out)
            if not ok:
                metrics["ws_send_blocked"] += 1
                await browser_ws.close(code=1013)
                break

    except WebSocketDisconnect:
        pass
    finally:
        pending_ws.pop(ws_id, None)
        payload_close = {"ws_id": ws_id}
        msg_close = json.dumps({
            "type": "ws_close",
            "payload": payload_close,
            "sig": sign(DEVICES[did], payload_close),
        })
        try:
            await device_send(did, msg_close)
        except Exception:
            pass

# ================= START ==================
if __name__ == "__main__":
    log("[BOOT] relay starting")
    log(f"[INFO] devices: {list(DEVICES.keys())}")

    threading.Thread(target=start_tunnel_thread, daemon=True).start()

    uvicorn.run(app, host=USER_HOST, port=USER_PORT, log_level="warning")
