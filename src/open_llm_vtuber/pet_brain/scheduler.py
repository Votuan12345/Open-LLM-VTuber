"""Desktop-global behavior scheduler (spec §4, §7, §9).

One `BehaviorScheduler` runs a single background asyncio task while at least
one client is connected. Each tick it samples desktop context once, ticks
every distinct `PetBrain` referenced by the handled clients once, detects
ignored proactive turns and then hands over to the decision/commit path.

Not exported from `pet_brain/__init__.py` to avoid an import cycle with the
websocket handler that constructs it.
"""

import asyncio
import random
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional

from loguru import logger

from .behavior import effective_min_interval_s
from .context import (
    ContextSensor,
    ContextSnapshot,
    classify_process,
    make_default_sensor,
)
from .eligibility import ClientEligibility
from .events import BrainEvent
from .pet_brain import BrainTickInputs, PetBrain
from .presence import ClientPresence, SchedulerState

# `user_returned` fires when the current known idle drops below this (spec §7.3).
RETURNED_IDLE_BELOW_S = 30.0

RunProactiveTurn = Callable[
    [Any, Callable[[str], Awaitable[None]], str, str], Awaitable[None]
]


class BehaviorScheduler:
    def __init__(
        self,
        client_connections: Dict[str, Any],
        client_contexts: Dict[str, Any],
        current_conversation_tasks: Dict[str, Optional[Any]],
        chat_group_manager: Any,
        run_proactive_turn: RunProactiveTurn,
        sensor: Optional[ContextSensor] = None,
        tick_seconds: float = 20.0,
        preempt_warn_seconds: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
        rng: Optional[random.Random] = None,
    ):
        self.client_connections = client_connections
        self.client_contexts = client_contexts
        self.current_conversation_tasks = current_conversation_tasks
        self.chat_group_manager = chat_group_manager
        self.eligibility = ClientEligibility(
            client_connections,
            client_contexts,
            current_conversation_tasks,
            chat_group_manager,
        )
        self._run_proactive_turn = run_proactive_turn
        self.sensor: ContextSensor = (
            sensor if sensor is not None else make_default_sensor()
        )
        self.tick_seconds = tick_seconds
        self.preempt_warn_seconds = preempt_warn_seconds
        self._clock = clock
        self.rng = rng if rng is not None else random.Random()

        self.presences: Dict[str, ClientPresence] = {}
        self.state = SchedulerState()
        self._task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Lifecycle (spec §9)
    # ------------------------------------------------------------------

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def client_connected(self, uid: str) -> None:
        async with self._lock:
            if uid not in self.presences:
                self.presences[uid] = ClientPresence(
                    uid=uid, connected_at=self._clock()
                )
            if self._task is None or self._task.done():
                self._task = asyncio.create_task(self._run())
                logger.debug("[Behavior] scheduler started")

    async def client_disconnected(self, uid: str) -> None:
        async with self._lock:
            self.presences.pop(uid, None)
            if self.presences or self._task is None:
                return
            task = self._task
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            self._task = None
            self.state = SchedulerState()
            logger.debug("[Behavior] scheduler stopped")

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self.tick_seconds)
            try:
                await self.tick_once()
            except Exception:
                logger.exception("[Behavior] tick failed")

    # ------------------------------------------------------------------
    # Tick pipeline (spec §4.4, §7)
    # ------------------------------------------------------------------

    async def tick_once(self) -> None:
        handled = [uid for uid in self.presences if self.eligibility.handles(uid)]
        if not handled:
            return

        now = self._clock()
        ctx = self.sensor.sample()
        self.state.last_context = ctx

        brains_by_id: Dict[int, PetBrain] = {}
        clients_by_brain: Dict[int, List[str]] = {}
        for uid in handled:
            brain = self.eligibility.brain_for(uid)
            if brain is None:
                continue
            brains_by_id[id(brain)] = brain
            clients_by_brain.setdefault(id(brain), []).append(uid)

        self._log_context_label(ctx, handled)

        prev_idle = self.state.previous_user_idle_seconds
        for key, brain in brains_by_id.items():
            uids = clients_by_brain[key]
            self._tick_brain(brain, uids, ctx, prev_idle, now)

        self.state.previous_user_idle_seconds = ctx.user_idle_seconds

        self._detect_ignored(handled, now)

        await self._select_and_dispatch(now, ctx)

    def _log_context_label(self, ctx: ContextSnapshot, handled: List[str]) -> None:
        # Label uses the most relevant handled client's brain table.
        first_uid = self.eligibility.order(handled, self.presences)[0]
        brain = self.eligibility.brain_for(first_uid)
        category = None
        if brain is not None and brain.config.context.enabled:
            category = classify_process(
                ctx.process_name, brain.config.context.process_categories
            )
        label = f"{category}({ctx.process_name})"
        if label != self.state.last_category_label:
            logger.debug(f"[Context] {label}")
            self.state.last_category_label = label

    def _tick_brain(
        self,
        brain: PetBrain,
        uids: List[str],
        ctx: ContextSnapshot,
        prev_idle: Optional[float],
        now: float,
    ) -> None:
        context_cfg = brain.config.context
        brain_ctx = (
            ctx if context_cfg.enabled else ContextSnapshot.unknown(ctx.taken_at_wall)
        )
        category = classify_process(
            brain_ctx.process_name, context_cfg.process_categories
        )
        away_s = context_cfg.away_after_min * 60
        returned = (
            prev_idle is not None
            and prev_idle >= away_s
            and brain_ctx.user_idle_seconds is not None
            and brain_ctx.user_idle_seconds < RETURNED_IDLE_BELOW_S
        )

        presences = [self.presences[uid] for uid in uids]
        timestamps = [
            ts
            for p in presences
            for ts in (p.last_conversation_end, p.last_user_interaction)
            if ts is not None
        ]
        if timestamps:
            quiet_seconds = now - max(timestamps)
        else:
            quiet_seconds = now - brain.lifecycle.since

        conversation_running = any(
            p.reserved or self._task_running(p.uid) for p in presences
        )

        brain.tick(
            BrainTickInputs(
                context=brain_ctx,
                category=category,
                quiet_seconds=quiet_seconds,
                conversation_running=conversation_running,
                user_returned=returned,
            )
        )

        if returned:
            for p in presences:
                p.returned_bonus_pending = True
                p.returned_after_s = prev_idle

    def _task_running(self, uid: str) -> bool:
        task = self.current_conversation_tasks.get(uid)
        return task is not None and not task.done()

    def _detect_ignored(self, handled: List[str], now: float) -> None:
        for uid in handled:
            presence = self.presences[uid]
            awaiting = presence.awaiting_reply_since
            if awaiting is None:
                continue
            brain = self.eligibility.brain_for(uid)
            if brain is None:
                continue
            proactive_cfg = brain.config.proactive
            if now - awaiting < proactive_cfg.ignored_after_min * 60:
                continue
            presence.ignored_count += 1
            presence.awaiting_reply_since = None
            brain.notify(BrainEvent.PROACTIVE_IGNORED)
            backoff_min = (
                effective_min_interval_s(proactive_cfg, presence.ignored_count) / 60
            )
            logger.info(
                f"[Proactive] ignored count={presence.ignored_count} "
                f"backoff={backoff_min:g}min"
            )

    async def _select_and_dispatch(self, now: float, ctx: ContextSnapshot) -> None:
        """Decision and commit path; filled in by Task 9."""
        return
