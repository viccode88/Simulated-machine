"""plant-bus 進入點。"""
from __future__ import annotations

import asyncio
import os

from common.util import EventLogger, cfg_get, env_int, install_excepthook, load_config

from .bus import PlantBus
from .http_api import start_http
from .snapshot_store import SnapshotStore


async def main() -> None:
    config_dir = os.environ.get("CONFIG_DIR", "/app/configs")
    cfg = load_config(os.path.join(config_dir, "plant.yaml"))
    state_dir = os.environ.get("STATE_DIR", "/var/lib/plant-bus")
    os.makedirs(state_dir, exist_ok=True)

    log = EventLogger(device="plant-bus", path=os.path.join(state_dir, "events.jsonl"))
    install_excepthook(log)

    store = SnapshotStore(os.path.join(state_dir, "snapshots"))
    bus = PlantBus(cfg, store, log)
    bus.log.sim_time_fn = lambda: bus.sim_time

    await bus.start()
    runner = await start_http(bus, port=env_int("HTTP_PORT", int(cfg_get(cfg, "bus.http_port", 8080))))
    log.emit("BUS_HTTP_LISTENING", port=env_int("HTTP_PORT", int(cfg_get(cfg, "bus.http_port", 8080))))

    boot_snapshot = os.environ.get("RESTORE_ON_BOOT", "").strip()
    if boot_snapshot:
        await asyncio.sleep(float(os.environ.get("RESTORE_ON_BOOT_DELAY", "5")))
        try:
            summary = await bus.restore_snapshot(boot_snapshot, {"resume": True})
            log.emit("BOOT_SNAPSHOT_RESTORED", **summary)
        except Exception as exc:
            log.emit("BOOT_SNAPSHOT_FAILED", name=boot_snapshot, error=repr(exc))

    try:
        while True:
            await asyncio.sleep(3600)
    finally:
        await runner.cleanup()
        log.close()


if __name__ == "__main__":
    asyncio.run(main())
