"""plant-bus 管理 API（management_net）。

快速恢復狀態功能的對外介面：不需要重啟 docker 即可把整套模擬環境
（含物理量、狀態機、跳機鎖存、累積值、暫存器內容）回到指定快照。
"""
from __future__ import annotations

import json
from typing import Any

from aiohttp import web

from .bus import PlantBus


def _json(data: Any, status: int = 200) -> web.Response:
    return web.json_response(data, status=status, dumps=lambda d: json.dumps(d, ensure_ascii=False, default=str))


def build_app(bus: PlantBus) -> web.Application:
    app = web.Application()
    routes = web.RouteTableDef()

    # -- 健康與狀態 ----------------------------------------------------
    @routes.get("/health")
    async def health(_: web.Request) -> web.Response:
        online = [n for n in bus.expected_devices if n in bus.participants]
        healthy = len(online) == len(bus.expected_devices) or not bus.expected_devices
        return _json(
            {
                "status": "ok" if healthy else "degraded",
                "tick": bus.tick,
                "sim_time": round(bus.sim_time, 3),
                "paused": bus.paused,
                "online": online,
                "offline": [n for n in bus.expected_devices if n not in bus.participants],
            },
            200 if healthy else 200,
        )

    @routes.get("/state")
    async def state(_: web.Request) -> web.Response:
        return _json(bus.state())

    @routes.get("/signals")
    async def signals(_: web.Request) -> web.Response:
        return _json(bus.state()["signals"])

    @routes.get("/events")
    async def events(request: web.Request) -> web.Response:
        limit = int(request.query.get("limit", 100))
        event_filter = request.query.get("event")
        device = request.query.get("device")
        items = list(bus.events)
        if event_filter:
            items = [e for e in items if e.get("event") == event_filter]
        if device:
            items = [e for e in items if e.get("device") == device]
        return _json(items[-limit:])

    @routes.get("/metrics")
    async def metrics(_: web.Request) -> web.Response:
        return web.Response(text=bus.metrics(), content_type="text/plain")

    # -- 模擬控制 ------------------------------------------------------
    @routes.post("/sim/pause")
    async def sim_pause(_: web.Request) -> web.Response:
        await bus.pause()
        return _json({"paused": True, "tick": bus.tick, "sim_time": bus.sim_time})

    @routes.post("/sim/resume")
    async def sim_resume(_: web.Request) -> web.Response:
        await bus.resume()
        return _json({"paused": False, "tick": bus.tick, "sim_time": bus.sim_time})

    @routes.post("/sim/step")
    async def sim_step(request: web.Request) -> web.Response:
        body = await _body(request)
        await bus.step(int(body.get("ticks", 1)))
        return _json({"stepping": int(body.get("ticks", 1)), "tick": bus.tick})

    @routes.post("/sim/speed")
    async def sim_speed(request: web.Request) -> web.Response:
        body = await _body(request)
        bus.set_speed(float(body.get("speed", 1.0)))
        return _json({"speed": bus.speed})

    # -- 快照 ----------------------------------------------------------
    @routes.get("/snapshot")
    async def snapshot_list(_: web.Request) -> web.Response:
        return _json({"snapshots": bus.store.list(), "last": bus.last_snapshot})

    @routes.post("/snapshot/save")
    async def snapshot_save(request: web.Request) -> web.Response:
        body = await _body(request)
        name = str(body.get("name") or "manual")
        try:
            meta = await bus.save_snapshot(
                name,
                description=str(body.get("description", "")),
                tags=list(body.get("tags") or []),
                timeout=float(body.get("timeout", 5.0)),
            )
        except Exception as exc:
            return _json({"error": repr(exc)}, status=500)
        return _json(meta)

    @routes.post("/snapshot/restore")
    async def snapshot_restore(request: web.Request) -> web.Response:
        body = await _body(request)
        name = str(body.get("name") or bus.last_snapshot or "")
        if not name or not bus.store.exists(name):
            return _json({"error": f"找不到快照 {name}"}, status=404)
        options = {
            "clear_latches": bool(body.get("clear_latches", False)),
            "keep_faults": bool(body.get("keep_faults", False)),
            "preserve_totalizers": bool(body.get("preserve_totalizers", False)),
            "resume": bool(body.get("resume", True)),
        }
        try:
            summary = await bus.restore_snapshot(name, options,
                                                 timeout=float(body.get("timeout", 5.0)))
        except FileNotFoundError:
            return _json({"error": f"找不到快照 {name}"}, status=404)
        except Exception as exc:
            return _json({"error": repr(exc)}, status=500)
        return _json(summary)

    @routes.get("/snapshot/{name}")
    async def snapshot_get(request: web.Request) -> web.Response:
        name = request.match_info["name"]
        if not bus.store.exists(name):
            return _json({"error": "not found"}, status=404)
        document = bus.store.load(name)
        if request.query.get("full") == "1":
            return _json(document)
        return _json({"meta": document.get("meta"), "verified": bus.store.verify(name)})

    @routes.delete("/snapshot/{name}")
    async def snapshot_delete(request: web.Request) -> web.Response:
        name = request.match_info["name"]
        return _json({"deleted": bus.store.delete(name), "name": name})

    # -- 故障注入 ------------------------------------------------------
    @routes.post("/fault")
    async def fault(request: web.Request) -> web.Response:
        body = await _body(request)
        target = str(body.get("target", "*"))
        payload = {
            "action": body.get("action", "set"),
            "category": body.get("category"),
            "name": body.get("name"),
            "spec": body.get("spec"),
        }
        return _json(await bus.inject_fault(target, payload))

    @routes.post("/fault/clear")
    async def fault_clear(request: web.Request) -> web.Response:
        body = await _body(request)
        target = str(body.get("target", "*"))
        payload = {"action": "clear", "category": body.get("category"), "name": body.get("name")}
        return _json(await bus.inject_fault(target, payload))

    @routes.post("/signal/force")
    async def signal_force(request: web.Request) -> web.Response:
        body = await _body(request)
        value = body.get("value")
        return _json(bus.force_signal(str(body["name"]), None if value is None else float(value)))

    app.add_routes(routes)
    return app


async def _body(request: web.Request) -> dict:
    if not request.can_read_body:
        return {}
    try:
        return await request.json()
    except Exception:
        return {}


async def start_http(bus: PlantBus, host: str = "0.0.0.0", port: int = 8080) -> web.AppRunner:
    app = build_app(bus)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    return runner
