"""Background automation: inbox watcher + rule engine + event dispatcher.

Wired into the FastAPI app through the lifespan (see kanban_ui/main.py):
background tasks are started as asyncio tasks and stopped on shutdown.
"""
from .event_dispatcher import EventDispatcher, events_status
from .inbox import InboxWatcher, inbox_status
from .rules import RuleEngine, rules_status, emit_rule_event
from .webhooks import (
    init_dispatcher,
    shutdown_dispatcher,
    emit_event,
    webhooks_status,
)
from . import plan_md

__all__ = [
    "EventDispatcher",
    "InboxWatcher",
    "RuleEngine",
    "events_status",
    "inbox_status",
    "rules_status",
    "emit_rule_event",
    "init_dispatcher",
    "shutdown_dispatcher",
    "emit_event",
    "webhooks_status",
    "plan_md",
]
