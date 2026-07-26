"""plant-bus 用戶端：自動重連、訊息佇列、品質追蹤。"""
from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from .protocol import PROTOCOL_VERSION, MsgType, Role, decode, encode


class SimBusClient:
    def __init__(
        self,
        name: str,
        host: str,
        port: int,
        *,
        role: Role = Role.DEVICE,
        publishes: list[str] | None = None,
        subscribes: list[str] | None = None,
        reconnect_delay: float = 1.0,
    ) -> None:
        self.name = name
        self.host = host
        self.port = port
        self.role = role
        self.publishes = publishes or []
        self.subscribes = subscribes or []
        self.reconnect_delay = reconnect_delay
        self.connected = asyncio.Event()
        self.messages: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=2000)
        self.connect_count = 0
        self.last_error: str | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._task: asyncio.Task | None = None
        self._closing = False

    # -- 生命週期 ----------------------------------------------------------
    def start(self) -> None:
        self._task = asyncio.ensure_future(self._run())

    async def close(self) -> None:
        self._closing = True
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        if self._writer:
            self._writer.close()

    async def _run(self) -> None:
        while not self._closing:
            try:
                reader, writer = await asyncio.open_connection(self.host, self.port)
                self._writer = writer
                self.connect_count += 1
                await self.send(
                    {
                        "type": MsgType.HELLO.value,
                        "device": self.name,
                        "role": self.role.value,
                        "protocol": PROTOCOL_VERSION,
                        "publishes": self.publishes,
                        "subscribes": self.subscribes,
                    }
                )
                self.connected.set()
                self.last_error = None
                while True:
                    line = await reader.readline()
                    if not line:
                        raise ConnectionError("plant-bus 關閉連線")
                    try:
                        message = decode(line)
                    except Exception as exc:
                        self.last_error = f"decode: {exc}"
                        continue
                    if message.get("type") == MsgType.PING.value:
                        await self.send({"type": MsgType.PONG.value, "device": self.name})
                        continue
                    if self.messages.full():
                        with contextlib.suppress(asyncio.QueueEmpty):
                            self.messages.get_nowait()
                    await self.messages.put(message)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = repr(exc)
            finally:
                self.connected.clear()
                if self._writer:
                    with contextlib.suppress(Exception):
                        self._writer.close()
                    self._writer = None
            if not self._closing:
                await asyncio.sleep(self.reconnect_delay)

    # -- 收送 --------------------------------------------------------------
    async def send(self, message: dict[str, Any]) -> bool:
        writer = self._writer
        if writer is None:
            return False
        try:
            writer.write(encode(message))
            await writer.drain()
            return True
        except Exception as exc:
            self.last_error = repr(exc)
            return False

    async def next_message(self, timeout: float | None = None) -> dict[str, Any] | None:
        """取下一則訊息；逾時回傳 None。

        刻意不使用 asyncio.wait_for：Python 3.10 的 wait_for 在「取消與內部
        future 同時完成」的競態下會吞掉 CancelledError，導致設備主迴圈無法停止。
        asyncio.wait 沒有這個問題。
        """
        if timeout is None:
            return await self.messages.get()
        getter = asyncio.ensure_future(self.messages.get())
        try:
            done, _ = await asyncio.wait({getter}, timeout=timeout)
        except asyncio.CancelledError:
            getter.cancel()
            raise
        if getter in done:
            return getter.result()
        getter.cancel()
        return None

    async def send_event(self, payload: dict[str, Any]) -> None:
        await self.send({"type": MsgType.EVENT.value, "device": self.name, "payload": payload})
