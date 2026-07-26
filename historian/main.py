"""事件與歷史資料記錄器。

以 OBSERVER 身分連上 plant-bus：只接收程序影像與事件，不參與 lockstep，
因此 historian 掛掉不會拖垮模擬。
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
from collections import deque

from aiohttp import web

from common.simbus.client import SimBusClient
from common.simbus.protocol import DEFAULT_BUS_PORT, MsgType, Role
from common.util import EventLogger, install_excepthook, wall_time_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    sim_time REAL NOT NULL,
    wall_time REAL NOT NULL,
    signal TEXT NOT NULL,
    value REAL NOT NULL,
    quality TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_samples ON samples(signal, sim_time);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sim_time REAL,
    wall_time TEXT,
    device TEXT,
    event TEXT,
    code INTEGER,
    first_out INTEGER,
    payload TEXT
);
CREATE INDEX IF NOT EXISTS idx_events ON events(device, event);
"""


class Historian:
    def __init__(self) -> None:
        self.state_dir = os.environ.get("STATE_DIR", "/var/lib/historian")
        os.makedirs(self.state_dir, exist_ok=True)
        self.log = EventLogger(device="historian", path=os.path.join(self.state_dir, "events.jsonl"))
        install_excepthook(self.log)
        self.conn = sqlite3.connect(os.path.join(self.state_dir, "history.db"), isolation_level=None)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self.sample_period = float(os.environ.get("SAMPLE_PERIOD", "1.0"))
        self.retention_hours = float(os.environ.get("RETENTION_HOURS", "24"))
        self.latest: dict[str, dict] = {}
        self.events: deque[dict] = deque(maxlen=5000)
        self.first_outs: list[dict] = []
        self.sim_time = 0.0
        self.tick = 0
        self._last_sample = -1e9
        self.sample_count = 0
        self._running = True
        self._tasks: list[asyncio.Task] = []
        self.bus = SimBusClient(
            "historian",
            os.environ.get("SIM_BUS_HOST", "plant-bus"),
            int(os.environ.get("SIM_BUS_PORT", DEFAULT_BUS_PORT)),
            role=Role.OBSERVER,
        )

    async def run(self) -> None:
        self.bus.start()
        runner = await self._start_http()
        self._tasks.append(asyncio.ensure_future(self._retention_loop()))
        try:
            while self._running:
                message = await self.bus.next_message(timeout=5.0)
                if message is None:
                    continue
                kind = message.get("type")
                if kind == MsgType.TICK.value:
                    self._on_tick(message)
                elif kind == MsgType.EVENT.value:
                    self._on_event(message.get("payload") or {})
        finally:
            await runner.cleanup()

    def _on_tick(self, message: dict) -> None:
        sim_time = float(message.get("sim_time", self.sim_time))
        # 快照還原可能把模擬時間拉回較早的時間軸；若不重設取樣基準，
        # historian 會停止取樣直到模擬時間追過舊時間線
        if sim_time < self._last_sample:
            self.log.emit("HISTORIAN_TIMELINE_REWIND", from_sim_time=round(self._last_sample, 3),
                          to_sim_time=round(sim_time, 3))
            self._last_sample = -1e9
        self.sim_time = sim_time
        self.tick = int(message.get("tick", self.tick))
        signals = message.get("inputs") or {}
        self.latest = signals
        if self.sim_time - self._last_sample < self.sample_period:
            return
        self._last_sample = self.sim_time
        now = time.time()
        rows = [
            (self.sim_time, now, name, float(data.get("value", 0.0)), str(data.get("quality", "")))
            for name, data in signals.items()
        ]
        if rows:
            self.conn.executemany(
                "INSERT INTO samples(sim_time,wall_time,signal,value,quality) VALUES(?,?,?,?,?)", rows
            )
            self.sample_count += len(rows)

    def _on_event(self, payload: dict) -> None:
        self.events.append(payload)
        self.conn.execute(
            "INSERT INTO events(sim_time,wall_time,device,event,code,first_out,payload) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                payload.get("sim_time"),
                payload.get("wall_time", wall_time_iso()),
                payload.get("device"),
                payload.get("event"),
                payload.get("code"),
                1 if payload.get("first_out") else 0,
                json.dumps(payload, ensure_ascii=False, default=str),
            ),
        )
        if payload.get("event") == "FIRST_OUT":
            self.first_outs.append(payload)
            self.log.emit("FIRST_OUT_RECORDED", **{k: v for k, v in payload.items()
                                                   if k not in ("wall_time", "sim_time")})

    async def _retention_loop(self) -> None:
        while True:
            await asyncio.sleep(600)
            cutoff = time.time() - self.retention_hours * 3600
            self.conn.execute("DELETE FROM samples WHERE wall_time < ?", (cutoff,))

    # -- HTTP --------------------------------------------------------------
    async def _start_http(self) -> web.AppRunner:
        app = web.Application()
        routes = web.RouteTableDef()

        @routes.get("/health")
        async def health(_: web.Request) -> web.Response:
            return web.json_response({"status": "ok", "sim_time": self.sim_time,
                                      "samples": self.sample_count,
                                      "connected": self.bus.connected.is_set()})

        @routes.get("/latest")
        async def latest(_: web.Request) -> web.Response:
            return web.json_response(self.latest)

        @routes.get("/query")
        async def query(request: web.Request) -> web.Response:
            signal = request.query.get("signal", "")
            limit = int(request.query.get("limit", 600))
            rows = self.conn.execute(
                "SELECT sim_time,value,quality FROM samples WHERE signal=? "
                "ORDER BY sim_time DESC LIMIT ?", (signal, limit)
            ).fetchall()
            return web.json_response(
                {"signal": signal,
                 "samples": [{"sim_time": r[0], "value": r[1], "quality": r[2]}
                             for r in reversed(rows)]}
            )

        @routes.get("/events")
        async def events(request: web.Request) -> web.Response:
            limit = int(request.query.get("limit", 200))
            device = request.query.get("device")
            event = request.query.get("event")
            sql = "SELECT payload FROM events WHERE 1=1"
            params: list = []
            if device:
                sql += " AND device=?"
                params.append(device)
            if event:
                sql += " AND event=?"
                params.append(event)
            sql += " ORDER BY id DESC LIMIT ?"
            params.append(limit)
            rows = self.conn.execute(sql, params).fetchall()
            return web.json_response([json.loads(r[0]) for r in reversed(rows)])

        @routes.get("/first-out")
        async def first_out(_: web.Request) -> web.Response:
            rows = self.conn.execute(
                "SELECT payload FROM events WHERE first_out=1 ORDER BY id DESC LIMIT 50"
            ).fetchall()
            return web.json_response([json.loads(r[0]) for r in rows])

        @routes.get("/metrics")
        async def metrics(_: web.Request) -> web.Response:
            lines = ["# TYPE historian_samples_total counter",
                     f"historian_samples_total {self.sample_count}",
                     "# TYPE historian_sim_time gauge",
                     f"historian_sim_time {self.sim_time}"]
            for name, data in self.latest.items():
                lines.append(f'plant_signal{{signal="{name}"}} {data.get("value", 0.0)}')
            return web.Response(text="\n".join(lines) + "\n", content_type="text/plain")

        app.add_routes(routes)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", int(os.environ.get("HTTP_PORT", "8081")))
        await site.start()
        self.log.emit("HISTORIAN_STARTED", port=int(os.environ.get("HTTP_PORT", "8081")))
        return runner


if __name__ == "__main__":
    asyncio.run(Historian().run())
