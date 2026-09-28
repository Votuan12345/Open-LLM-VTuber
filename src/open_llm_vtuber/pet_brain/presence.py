"""Per-client presence and desktop-global scheduler state (spec §4.2-4.3).

Owned and mutated by the BehaviorScheduler (a later task). The selector in
`behavior.py` only reads `ClientPresence`; it never mutates it.
"""

import asyncio
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Optional

from .context import ContextSnapshot


@dataclass
class ClientPresence:
    """Presence and cooldown bookkeeping for one connected client."""

    uid: str
    connected_at: float
    last_user_interaction: Optional[float] = None
    last_conversation_end: Optional[float] = None
    last_proactive: Optional[float] = None
    proactive_timestamps: Deque[float] = field(default_factory=deque)
    ignored_count: int = 0
    awaiting_reply_since: Optional[float] = None
    last_idle_expression: Optional[float] = None
    idle_expression_timestamps: Deque[float] = field(default_factory=deque)
    reserved: bool = False
    proactive_task: Optional["asyncio.Task"] = None
    proactive_preempted: bool = False
    user_turn_pending: bool = False
    returned_bonus_pending: bool = False
    returned_after_s: Optional[float] = None


@dataclass
class SchedulerState:
    """Desktop-global state, shared across all connected clients."""

    last_context: Optional[ContextSnapshot] = None
    previous_user_idle_seconds: Optional[float] = None
    last_category_label: Optional[str] = None


def prune_window(ts: Deque[float], now: float, window_s: float = 3600.0) -> None:
    """Drop timestamps older than `window_s` from the left of `ts`, in place.

    For the scheduler's own bookkeeping. The selector must not mutate
    presence, so it counts timestamps within the window instead of pruning.
    """

    while ts and (now - ts[0]) >= window_s:
        ts.popleft()
