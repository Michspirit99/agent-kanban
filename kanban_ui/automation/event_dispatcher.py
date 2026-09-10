"""Event dispatcher: delivers durable outbox events (Phase 3.1).

Polls the ``issue_events`` outbox written by the Store and delivers each
pending event to the webhook dispatcher and the reactive rule engine, then
marks it delivered. This runs in the UI process's lifespan, so mutations
from ANY client (REST, HTTP MCP, stdio MCP writing to the same database,
inbox watcher) produce the same webhook and automation behaviour.

Delivery semantics (this slice): single attempt per event — failures are
logged and the event is still marked delivered (at-most-once). Consumers
that need dedup can key on the outbox event id. Retries/backoff are a
future slice.

Recursion safety: the Store does not record events for mutations performed
by actor "automation", so rule-triggered moves/comments cannot re-enter
the dispatcher.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from kanban_store import Store

from . import rules, webhooks

log = logging.getLogger("kanban.automation.events")

DEFAULT_INTERVAL = float(os.environ.get("KANBAN_EVENT_POLL_INTERVAL", "1.0"))
BATCH_SIZE = 100

Emitter = Callable[[str, dict[str, Any]], Any]

_status: dict[str, Any] = {
    "running": False,
    "interval_sec": DEFAULT_INTERVAL,
    "processed_total": 0,
    "last_processed_at": None,
}


def events_status() -> dict[str, Any]:
    return dict(_status)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


async def _maybe_await(fn: Emitter, event: str, payload: dict[str, Any]) -> None:
    result = fn(event, payload)
    if asyncio.iscoroutine(result) or isinstance(result, Awaitable):
        await result


class EventDispatcher:
    """Polls the outbox and delivers pending events exactly once."""

    def __init__(
        self,
        store: Store,
        *,
        interval: float = DEFAULT_INTERVAL,
        batch_size: int = BATCH_SIZE,
        emit_webhook: Emitter | None = None,
        apply_reactive: Emitter | None = None,
    ):
        self.store = store
        self.interval = interval
        self.batch_size = batch_size
        self._emit_webhook: Emitter = emit_webhook or webhooks.emit_event
        self._apply_reactive: Emitter = apply_reactive or rules.emit_rule_event
        self._stop = asyncio.Event()

    async def _deliver(self, event: dict[str, Any]) -> None:
        event_type = event["event_type"]
        payload = event["payload"]
        if event_type in webhooks.VALID_EVENTS:
            await _maybe_await(self._emit_webhook, event_type, payload)
        if event_type in rules.REACTIVE_TRIGGERS:
            await _maybe_await(self._apply_reactive, event_type, payload)

    async def process_pending(self) -> int:
        """Deliver all pending events; returns the number processed."""
        processed = 0
        while True:
            batch = self.store.list_pending_events(limit=self.batch_size)
            if not batch:
                break
            for event in batch:
                try:
                    await self._deliver(event)
                except Exception:
                    # Single-attempt delivery: log and move on so one bad
                    # consumer cannot wedge the dispatcher.
                    log.exception(
                        "event delivery failed: #%s %s",
                        event["id"], event["event_type"],
                    )
                self.store.mark_event_delivered(event["id"])
                processed += 1
                _status["processed_total"] += 1
            if len(batch) < self.batch_size:
                break
        if processed:
            _status["last_processed_at"] = _now()
        return processed

    async def run(self) -> None:
        _status["running"] = True
        _status["interval_sec"] = self.interval
        log.info(
            "event dispatcher started (interval=%ss)", self.interval
        )
        try:
            while not self._stop.is_set():
                try:
                    await self.process_pending()
                except Exception:
                    log.exception("event dispatcher: poll failed")
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self.interval)
                except asyncio.TimeoutError:
                    pass
        finally:
            _status["running"] = False
            log.info("event dispatcher stopped")

    def stop(self) -> None:
        self._stop.set()


__all__ = ["EventDispatcher", "events_status"]
