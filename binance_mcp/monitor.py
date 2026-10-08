"""Small operational monitor driven by User Data Streams plus bounded REST recovery."""
from __future__ import annotations

import asyncio
import contextlib
import json
import time
from urllib.parse import urlencode

import websockets

from .client import BinanceClientError


class Monitor:
    def __init__(self, execution, interval: float = 30):
        if not 5 <= interval <= 60:
            raise ValueError("monitor interval must be 5..60 seconds")
        self.execution = execution
        self.interval = interval
        self.wake = asyncio.Event()
        self.tasks = []

    async def start(self):
        ledger = self.execution.ledger
        ledger.set_meta("monitorAt", None)
        ledger.set_meta("streamConnected", False)
        # Paper simulation never opens an authenticated event stream.
        has_live = ledger.db.execute("SELECT 1 FROM strategies WHERE mode='live'").fetchone()
        if has_live:
            ledger.pause("monitor starting; account reconciliation required")
        self.tasks = [asyncio.create_task(self.run())]
        if has_live and self.execution.client.auth_status()["account_access_ready"]:
            self.tasks.append(asyncio.create_task(self.stream()))

    async def stop(self):
        for task in self.tasks:
            task.cancel()
        for task in self.tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self.tasks = []
        self.execution.ledger.set_meta("streamConnected", False)
        self.execution.ledger.set_meta("monitorAt", None)

    async def tick(self):
        svc, ledger = self.execution, self.execution.ledger
        async with svc.lock:
            for intent in ledger.outstanding():
                try:
                    await svc.reconcile(intent["id"])
                except Exception:
                    if ledger.strategy(intent["strategy"])["mode"] == "paper":
                        ledger.set_meta("paperPause:" + intent["strategy"], "paper reconciliation incomplete for " + intent["id"])
                    else:
                        ledger.pause("reconciliation incomplete for " + intent["id"])
            if ledger.db.execute("SELECT 1 FROM strategies WHERE mode='live'").fetchone():
                await svc.client.sync_time()
                await svc.check_account()
                await svc.check_protection()
                if not ledger.meta("streamConnected"):
                    ledger.pause("user stream disconnected; new entries paused")
                    raise BinanceClientError("user stream disconnected; new entries paused")
                if not any(i["state"] in ("PREPARED", "OUTCOME_UNKNOWN") or (i["result"] or {}).get("protectionUncertain")
                           for i in ledger.outstanding(live_only=True)):
                    reason = ledger.meta("pause") or ""
                    # Only transient operational pauses may clear automatically.
                    if reason.startswith(("monitor starting", "user stream", "missing or inactive protection", "unprotected owned", "reconciliation incomplete", "uncertain execution", "account coverage incomplete")):
                        ledger.set_meta("pause", None)
            ledger.set_meta("monitorAt", int(time.time() * 1000))

    async def run(self):
        while True:
            self.wake.clear()
            try:
                await self.tick()
            except Exception:
                self.execution.ledger.pause(self.execution.ledger.meta("pause") or "reconciliation incomplete for operational monitor")
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=self.interval)
            except TimeoutError:
                pass

    async def handle_event(self, message: dict):
        event = message.get("event", message)
        kind = event.get("e")
        if kind in ("executionReport", "listStatus", "outboundAccountPosition", "balanceUpdate"):
            self.execution.ledger.set_meta("lastUserEventAt", int(time.time() * 1000))
            self.wake.set()
        if kind == "eventStreamTerminated":
            raise BinanceClientError("user stream terminated")

    async def stream(self):
        svc, ledger = self.execution, self.execution.ledger
        delay = 1
        while True:
            try:
                await svc.client.sync_time()
                async with websockets.connect("wss://ws-api.binance.com:443/ws-api/v3", max_size=2**20,
                                              open_timeout=15, ping_interval=20, ping_timeout=20) as ws:
                    params = {"apiKey": svc.client.config.api_key,
                              "timestamp": int(time.time() * 1000) + svc.client._offsets.get("spot", 0)}
                    params["signature"] = svc.client._sign(urlencode(sorted(params.items())).encode("ascii"))
                    await ws.send(json.dumps({"id": "monitor-subscribe", "method": "userDataStream.subscribe.signature", "params": params}))
                    response = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
                    if response.get("status") != 200 or "subscriptionId" not in response.get("result", {}):
                        raise BinanceClientError("user stream authentication failed")
                    ledger.set_meta("streamConnected", True)
                    delay = 1
                    self.wake.set()
                    async for raw in ws:
                        await self.handle_event(json.loads(raw))
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            finally:
                ledger.set_meta("streamConnected", False)
            ledger.pause("user stream disconnected; reconciliation required")
            self.wake.set()
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)
